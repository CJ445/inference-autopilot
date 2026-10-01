"""Docker workload provider: inspect and restart project-owned containers, nothing else.

The Docker socket is a high-trust, root-equivalent boundary. This class is the only thing
that may touch it, and it exposes no generic command, exec, run, pull or argument
passthrough. A container is acted on only if it carries this project's labels, both when
it is resolved and again right before acting.
"""
import json
import re
import subprocess

from aiops.kubectl import ClusterError

MANAGED_LABEL = "com.inference-autopilot.managed"
WORKLOAD_LABEL = "com.inference-autopilot.workload"
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SHORT_ID = re.compile(r"[0-9a-f]{12}")   # `docker ps --format {{.ID}}`
FULL_ID = re.compile(r"[0-9a-f]{64}")    # `docker inspect` .Id
RESTART_GRACE_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 60


class WorkloadAbsent(ClusterError):
    """No container carries this workload's labels. The operator provisions it; nothing here does."""


class DockerProvider:
    """Bound at construction to ONE explicitly configured workload; any other name is refused."""

    def __init__(self, workload, run=subprocess.run):
        if not isinstance(workload, str) or not NAME.fullmatch(workload):
            raise ClusterError(f"invalid workload name: {workload!r}")
        self._workload, self._run = workload, run

    def get_workload(self, name):
        container = self._inspect(self._resolve(name), name)
        state = container["State"]
        health = state.get("Health", {}).get("Status")  # absent when no healthcheck is defined
        # docker restart keeps the container ID; StartedAt is what proves a new run
        return {"id": f"{container['Id']}:{state['StartedAt']}",
                "ready": bool(state["Running"]) and health in (None, "healthy")}

    def restart_workload(self, name):
        container = self._inspect(self._resolve(name), name)  # re-check right before acting
        if not container["State"]["Running"]:
            # `docker restart` would START a stopped container. Starting the workload is the
            # operator's decision, never a remediation's.
            raise ClusterError("the workload is not running; it is not started automatically")
        self._docker("restart", "-t", str(RESTART_GRACE_SECONDS), container["Id"])
        return "ok"

    def _resolve(self, name):
        if not NAME.fullmatch(name):
            raise ClusterError(f"invalid workload name: {name!r}")
        if name != self._workload:
            raise ClusterError(f"{name!r} is not the configured workload")
        ids = self._docker("ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
                           "--filter", f"label={WORKLOAD_LABEL}={name}",
                           "--format", "{{.ID}}").split()
        if not all(SHORT_ID.fullmatch(i) for i in ids):
            raise ClusterError("unexpected container id from docker")
        if not ids:
            raise WorkloadAbsent(f"no managed workload named {name!r}")
        if len(ids) > 1:
            raise ClusterError(f"ambiguous: {len(ids)} managed containers named {name!r}")
        return ids[0]

    def _inspect(self, container_id, name):
        try:
            container = json.loads(self._docker("inspect", container_id))[0]
            labels, state = container["Config"]["Labels"] or {}, container["State"]
            container["Id"], state["Running"], state["StartedAt"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ClusterError(f"unexpected docker state: {e!r}") from e
        if not FULL_ID.fullmatch(container["Id"]) or not container["Id"].startswith(container_id):
            raise ClusterError("container identity mismatch between ps and inspect")
        if labels.get(MANAGED_LABEL) != "true" or labels.get(WORKLOAD_LABEL) != name:
            raise ClusterError("container labels do not identify a managed workload")
        return container

    def _docker(self, *args):
        try:
            return self._run(["docker", *args], capture_output=True, text=True, check=True,
                             timeout=COMMAND_TIMEOUT_SECONDS).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise ClusterError(f"docker unavailable or failed: {getattr(e, 'stderr', None) or e}") from e


def workload_presence(provider, name):
    """'absent', 'stopped', 'starting' or 'running' (a paused container is running). Read-only: the provider's
    own label and identity checks, and no new capability on the provider itself. Ambiguity or an
    identity mismatch still raises ClusterError."""
    try:
        container = provider._inspect(provider._resolve(name), name)
    except WorkloadAbsent:
        return "absent"
    state = container["State"]
    if not state["Running"]:
        return "stopped"
    # Docker's own health check says the container is still inside its start period (the model is
    # loading): it is running but cannot be expected to answer yet.
    return "starting" if state.get("Health", {}).get("Status") == "starting" else "running"


def inspect_workload(provider, name):
    """The managed workload's `docker inspect` record, or raise (WorkloadAbsent, or ClusterError for
    ambiguity or an identity mismatch). One definition of "this is the managed workload" for
    everything that must act on it: exactly one container with BOTH project labels, the `ps` id
    equal to the `inspect` id, and the labels re-checked. Like `workload_presence`, a module
    function so `DockerProvider` gains no public method."""
    return provider._inspect(provider._resolve(name), name)
