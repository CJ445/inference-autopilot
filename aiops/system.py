"""What the API tells an operator about the control plane itself, plus its one lifecycle hook.

Everything here is read-only except `request_stop`, which is exactly the `SIGTERM` that
`aiops stop` sends: it sets the stop event `lifecycle.start` is already waiting on, so the same
graceful shutdown runs (service stopped, watchdog disarmed, state released). Nothing here starts
anything or touches the managed workload: workload remediation remains incident -> policy ->
approval -> executor -> verification.
"""
import os
import platform
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from aiops import __version__
from aiops.doctor import run_doctor

SECRET_KEY = re.compile(r"token|secret|password|passwd|credential|api_?key", re.IGNORECASE)
ROOT = Path(__file__).resolve().parent.parent


def redact(value):
    """Defence in depth: the profile schema has no secrets today; a future one must not leak."""
    if isinstance(value, dict):
        return {k: "<redacted>" if SECRET_KEY.search(k) else redact(v) for k, v in value.items()}
    return value


def git_revision(root=ROOT, run=subprocess.run):
    """The checkout's short revision, or None. Never required: a release has no .git."""
    if not (Path(root) / ".git").exists():
        return None
    try:
        r = run(["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}", out) else None


class System:
    def __init__(self, profile, request_stop, doctor=run_doctor, revision=git_revision):
        self._profile, self._request_stop = profile, request_stop
        self._doctor, self._revision = doctor, revision
        self._diag_lock = threading.Lock()
        self.pid = os.getpid()

    def version(self):
        return {"version": __version__, "git_revision": self._revision(),
                "python": platform.python_version(),
                "platform": f"{platform.system()} {platform.machine()}".strip()}

    def config(self):
        p = self._profile
        sections = {k: v for k, v in p.items()
                    if isinstance(v, dict)}                     # every validated [section]
        return {"source": p["config_path"], "profile": p["name"], "provider": p["provider"],
                "sections": redact(sections)}

    def diagnostics(self):
        """The doctor's own results (read-only). None if a run is already in progress."""
        if not self._diag_lock.acquire(blocking=False):
            return None
        try:
            t0 = time.monotonic()
            results = self._doctor(self._profile)
            return {"results": results, "ran_at": datetime.now(timezone.utc).isoformat(),
                    "duration_seconds": round(time.monotonic() - t0, 1)}
        finally:
            self._diag_lock.release()

    def request_stop(self):
        self._request_stop()
