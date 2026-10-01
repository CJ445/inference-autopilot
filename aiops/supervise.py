"""Run an experimental child (the bounded GPU stressor) under watchdog limits.

This LAUNCHES a caller-supplied argv and kills its process group; it is deliberately separate
from aiops/watchdog.py, the control-plane guard, which can run no command at all.
"""
import os
import signal
import subprocess
import time

from aiops.watchdog import check


def supervise(argv, limits, sampler, interval, max_seconds, on_start=None):
    """Run argv in its own process group; kill the whole group on any violation."""
    proc = subprocess.Popen(argv, start_new_session=True)
    if on_start:
        on_start(proc.pid)
    deadline = time.monotonic() + max_seconds
    try:
        while True:
            if proc.poll() is not None:
                return _result("COMPLETED", [], proc)
            try:
                reason = check(sampler(), limits)
            except Exception:  # unreadable sensors mean we cannot vouch for safety
                reason = ["sampler_failed"]
            if not reason and time.monotonic() >= deadline:
                reason = ["max_duration"]
            if reason:
                _kill_group(proc)
                return _result("ABORTED", reason, proc)
            time.sleep(interval)
    finally:
        _kill_group(proc)  # never leave the workload behind, even if we are interrupted


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _result(outcome, reason, proc):
    return {"outcome": outcome, "reason": reason, "returncode": proc.returncode,
            "pid": proc.pid}
