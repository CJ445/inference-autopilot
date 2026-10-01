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
RESTART_GRACE_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 60


class DockerProvider:
    def __init__(self, run=subprocess.run):
        self._run = run

    def get_workload(self, name):
        container = self._inspect(self._resolve(name), name)
        state = container["State"]
        health = state.get("Health", {}).get("Status")  # absent when no healthcheck is defined
        # docker restart keeps the container ID; StartedAt is what proves a new run
        return {"id": f"{container['Id']}:{state['StartedAt']}",
                "ready": bool(state["Running"]) and health in (None, "healthy")}

    def restart_workload(self, name):
        container_id = self._resolve(name)
        self._inspect(container_id, name)  # re-check identity immediately before acting
        self._docker("restart", "-t", str(RESTART_GRACE_SECONDS), container_id)
        return "ok"

    def _resolve(self, name):
        if not NAME.fullmatch(name):
            raise ClusterError(f"invalid workload name: {name!r}")
        ids = self._docker("ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
                           "--filter", f"label={WORKLOAD_LABEL}={name}",
                           "--format", "{{.ID}}").split()
        if not ids:
            raise ClusterError(f"no managed workload named {name!r}")
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
        if labels.get(MANAGED_LABEL) != "true" or labels.get(WORKLOAD_LABEL) != name:
            raise ClusterError("container labels do not identify a managed workload")
        return container

    def _docker(self, *args):
        try:
            return self._run(["docker", *args], capture_output=True, text=True, check=True,
                             timeout=COMMAND_TIMEOUT_SECONDS).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise ClusterError(f"docker unavailable or failed: {getattr(e, 'stderr', None) or e}") from e
