"""The lease, the allowlisted container runner, and identity-verified recovery.

Everything the injector, the reaper process and the control-plane monitor share lives here, so
there is exactly one implementation of "unpause only the workload this fault paused".
"""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from aiops.docker import MANAGED_LABEL, WORKLOAD_LABEL, DockerProvider, WorkloadAbsent, inspect_workload
from aiops.kubectl import ClusterError

FAULT_PAUSE = "pause_workload"
FAULT_TYPES = {FAULT_PAUSE}
MIN_DURATION, MAX_DURATION, DEFAULT_DURATION = 15, 300, 120
COMMAND_TIMEOUT_SECONDS = 30
FULL_ID = re.compile(r"[0-9a-f]{64}")

ACTIVE_STATES = ("ARMING", "ACTIVE")
# Terminal: RECOVERED (unpaused, or nothing to undo), IDENTITY_CHANGED (the workload was restarted
# or replaced, so it was not touched), RECOVERY_REFUSED (could not be identified safely),
# FAILED (the pause did not happen), CANCELLED (stood down before pausing).
TERMINAL_STATES = ("RECOVERED", "IDENTITY_CHANGED", "RECOVERY_REFUSED", "FAILED", "CANCELLED")


class FaultError(Exception):
    """A refused or failed fault operation. `code` maps to the API's error contract."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# -- the only commands this package may run ---------------------------------------------------------

class AllowlistedDocker:
    """Runs exactly: ps (both project labels), inspect <id>, pause <id>, unpause <id>.

    Anything else raises before a process is started: no exec, rm, run, kill, restart, no other
    verb, no other argument shape, and the mutating verbs only accept a full 64-hex container id.
    """

    def __init__(self, workload, run=subprocess.run):
        self._workload, self._run = workload, run

    def allowed(self, argv):
        a = list(argv)
        if a[:1] != ["docker"] or len(a) < 2:
            return False
        verb = a[1]
        if verb == "ps":
            return a == ["docker", "ps", "-a", "--filter", f"label={MANAGED_LABEL}=true",
                         "--filter", f"label={WORKLOAD_LABEL}={self._workload}",
                         "--format", "{{.ID}}"]
        if verb == "inspect":
            return len(a) == 3 and bool(re.fullmatch(r"[0-9a-f]{12,64}", a[2]))
        if verb in ("pause", "unpause"):
            return len(a) == 3 and bool(FULL_ID.fullmatch(a[2]))
        return False

    def __call__(self, argv, **kw):
        if not self.allowed(argv):
            raise FaultError("REFUSED_OPERATION", f"not an allowed fault operation: {list(argv)!r}")
        kw.setdefault("timeout", COMMAND_TIMEOUT_SECONDS)
        return self._run(list(argv), **kw)


def provider_for(workload, run):
    return DockerProvider(workload, run=run)


def identity_of(container):
    return f"{container['Id']}:{container['State']['StartedAt']}"


# -- the lease -----------------------------------------------------------------------------------------

def now_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def read_lease(path):
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    try:
        lease = json.loads(text)
        if not isinstance(lease, dict) or not isinstance(lease.get("status"), str):
            raise ValueError("not a lease")
    except ValueError as e:
        raise FaultError("LEASE_UNREADABLE", f"fault lease {path} is unreadable: {e}") from e
    return lease


@contextlib.contextmanager
def locked(path):
    """An exclusive lock shared by every process that reads-modifies-writes this lease."""
    lock = open(f"{path}.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def write_lease(path, lease):
    """Atomic replace: a reader never sees half a lease."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(lease, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_lease(path, fn):
    """Read-modify-write under the lock; `fn(lease)` returns the new lease (or None for no change)."""
    with locked(path):
        lease = read_lease(path)
        if lease is None:
            return None
        new = fn(dict(lease))
        if new is not None:
            write_lease(path, new)
            return new
        return lease


def lease_path_for(profile):
    return Path(profile["control_plane"]["state_file"] + ".fault.json")


# -- recovery ---------------------------------------------------------------------------------------------

def recover(path, docker_run, by, clock=time.time):
    """End a fault: unpause the SAME workload it paused, or touch nothing. Idempotent; safe to run
    concurrently from the reaper, the monitor and the operator. Returns the lease's final state."""
    def finish(lease, status, action, detail):
        if lease["status"] not in ACTIVE_STATES:
            return None                                  # someone else already finished it
        lease.update(status=status, recovery={"at": now_iso(clock()), "by": by,
                                              "action": action, "detail": detail})
        return lease

    def decide(lease):
        if lease["status"] not in ACTIVE_STATES:
            return None
        provider = provider_for(lease["workload"], docker_run)
        try:
            container = inspect_workload(provider, lease["workload"])
        except WorkloadAbsent:
            return finish(lease, "RECOVERED", "nothing_to_undo", "the workload no longer exists")
        except ClusterError as e:
            return finish(lease, "RECOVERY_REFUSED", "none", f"could not identify the workload: {e}")
        if identity_of(container) != lease["expected_identity"]:
            return finish(lease, "IDENTITY_CHANGED", "none",
                          "the workload was restarted or replaced since the fault began; not touched")
        if not container["State"].get("Paused"):
            return finish(lease, "RECOVERED", "already_running", "the workload was not paused")
        try:
            docker_run(["docker", "unpause", container["Id"]], capture_output=True, text=True,
                       check=True)
            after = inspect_workload(provider, lease["workload"])
        except (OSError, subprocess.SubprocessError, ClusterError, FaultError) as e:
            return finish(lease, "RECOVERY_REFUSED", "unpause_failed", str(e)[:200])
        if after["State"].get("Paused"):
            return finish(lease, "RECOVERY_REFUSED", "unpause_failed", "still paused after unpause")
        return finish(lease, "RECOVERED", "unpaused", "the workload was resumed")

    final = update_lease(path, decide)
    return final["status"] if final else None


def expired(lease, clock=time.time):
    return lease["status"] in ACTIVE_STATES and clock() >= lease["expires_at"]
