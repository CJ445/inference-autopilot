"""`aiops` with no command: attach to the control plane (starting it first if needed), then hand
the terminal to the operator TUI.

This adds no lifecycle of its own. Detecting a running control plane uses the lifecycle's state
file and process-identity check; starting one runs the existing `aiops start` (doctor,
single-instance claim, watchdog arming and all) as a detached process; readiness is the real API
answering. The control plane is deliberately NOT a child of the TUI: it lives in its own session
and keeps running when the TUI exits. `aiops stop` (or the TUI's confirmed stop) ends it.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from aiops import lifecycle
from aiops.profile import ProfileError, load_profile

STARTUP_TIMEOUT_SECONDS = 90      # doctor + watchdog arming (15s) + API bind, with headroom
POLL_SECONDS = 0.2
LOG_TAIL_LINES = 12


def log_path(profile):
    return Path(profile["control_plane"]["state_file"] + ".log")


def api_ready(port, timeout=2):
    """True only if the control plane's API answers with a real status document."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/status", timeout=timeout) as r:
            return r.status == 200 and "health" in json.load(r)
    except (OSError, ValueError):
        return False


def spawn_start(config_path, log, argv=None):
    """The existing `aiops start`, detached: own session, no stdin, output to the log file."""
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "wb")
    try:
        return subprocess.Popen(
            argv or [sys.executable, "-m", "aiops", "start", "--config", str(config_path)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=os.getcwd(),
            env={**os.environ, "PYTHONUNBUFFERED": "1"})   # the log shows steps as they happen
    finally:
        out.close()                       # the child holds its own descriptor


def _tail(path, n=LOG_TAIL_LINES):
    try:
        return Path(path).read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _fail(out, reason, log, hint="aiops doctor"):
    out("AIOPS\n\nControl plane could not be started.\n\nReason:")
    for line in reason.splitlines() or ["unknown"]:
        out(f"  {line}")
    lines = _tail(log)
    if lines:
        out(f"\nLast output ({log}):")
        for line in lines:
            out(f"  {line}")
    out(f"\nRun diagnostics with:\n  {hint}")
    return 1


class _Follower:
    """Echoes the lifecycle's own output lines as they appear: the real startup steps."""

    def __init__(self, path, out):
        self.path, self.out, self.seen = path, out, 0

    def pump(self):
        lines = Path(self.path).read_text(errors="replace").splitlines() if Path(
            self.path).exists() else []
        for line in lines[self.seen:]:
            self.out(f"  {line}")
        self.seen = len(lines)


def ensure_control_plane(config_path, out=print, spawn=spawn_start, ready=api_ready,
                         sleep=time.sleep, clock=time.monotonic,
                         timeout=STARTUP_TIMEOUT_SECONDS):
    """Returns (exit_code, api_url). exit_code 0 means a control plane is answering at api_url."""
    try:
        profile = load_profile(config_path)
    except ProfileError as e:
        out(f"configuration error: {e}")
        return 2, None
    state_path = Path(profile["control_plane"]["state_file"])
    log = log_path(profile)
    cp, state = lifecycle._control_plane(state_path)

    if cp["state"] == "unknown":
        return _fail(out, cp["error"], log, "aiops status"), None
    if cp["state"] == "running":                    # attach: never a second control plane
        port = state["port"]
        deadline = clock() + timeout
        while not ready(port):
            if clock() >= deadline:
                out(f"AIOPS\n\nThe control plane is running (pid {cp['pid']}) but its API on "
                    f"127.0.0.1:{port} is not answering.\n\nRun diagnostics with:\n"
                    "  aiops status")
                return 1, None
            sleep(POLL_SECONDS)
        return 0, f"http://127.0.0.1:{port}"

    out("AIOPS\n\nStarting control plane…")
    child = spawn(profile["config_path"], log)
    follower = _Follower(log, out)
    deadline = clock() + timeout
    while True:
        follower.pump()
        cp, state = lifecycle._control_plane(state_path)
        if cp["state"] == "running" and ready(state["port"]):
            follower.pump()
            out(f"● CONTROL ONLINE (pid {cp['pid']}, API http://127.0.0.1:{state['port']})")
            return 0, f"http://127.0.0.1:{state['port']}"
        rc = child.poll()
        if rc is not None and not (rc == 0 and cp["state"] == "running"):
            follower.pump()
            if rc == 0 and cp["state"] != "running":
                reason = "`aiops start` exited without leaving a running control plane"
            else:
                reason = f"`aiops start` exited with code {rc} (its output is below)"
            return _fail(out, reason, log), None
        if clock() >= deadline:
            return _fail(out, f"the API did not answer within {timeout}s; the control plane "
                              "may still be starting (it is left running; `aiops stop` ends it)",
                         log, "aiops status"), None
        sleep(POLL_SECONDS)


def main(argv, out=print, tui_exec=None, ensure=None, isatty=None):
    import argparse
    from aiops.cli import TUI_BUILD, _tui_binary
    parser = argparse.ArgumentParser(prog="aiops")
    parser.add_argument("--config", default="aiops.toml", help="runtime profile (TOML)")
    args = parser.parse_args(argv)

    interactive = isatty() if isatty else sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        out("aiops opens an interactive terminal UI; this is not a terminal.\n"
            "For scripts use: aiops start | stop | status | doctor")
        return 2
    binary = _tui_binary()
    if not binary.exists():                     # before starting anything: no point otherwise
        out(f"the operator TUI is not built ({binary}).\nBuild it once with: {TUI_BUILD}")
        return 1
    code, url = (ensure or ensure_control_plane)(args.config, out=out)
    if code != 0:
        return code
    sys.stdout.flush()
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)   # inherited across exec: nothing lingers as a
    (tui_exec or os.execv)(str(binary), [str(binary), "--url", url])   # zombie when it stops
