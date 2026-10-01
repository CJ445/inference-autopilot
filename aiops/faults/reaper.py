"""The lease reaper: a separate, detached process that ends a fault at its deadline.

`python -m aiops.faults.reaper --lease PATH`. It imports only `aiops.faults.core` (no engine, no
API, no control plane), so it keeps working if the control plane crashes, and it can do exactly one
thing: recover THIS lease's workload, only if its lifecycle identity still matches. It exits as soon
as the lease is no longer active.
"""
import argparse
import os
import sys
import time

from aiops.faults.core import (ACTIVE_STATES, AllowlistedDocker, FaultError, now_iso, read_lease,
                               recover, update_lease)
from aiops.watchdog import proc_start

POLL_SECONDS = 0.5


def arm(path, clock=time.time):
    """Publish this process's identity so the injector can verify the safety net exists."""
    def mark(lease):
        lease["reaper"] = {"pid": os.getpid(), "proc_start": proc_start(os.getpid()),
                           "armed_at": now_iso(clock())}
        return lease
    return update_lease(path, mark)


def run(path, docker_run=None, clock=time.time, sleep=time.sleep, poll=POLL_SECONDS):
    """Wait for the deadline, then recover. Returns what happened (testable without a process)."""
    lease = arm(path, clock)
    if lease is None:
        return "no_lease"
    docker_run = docker_run or AllowlistedDocker(lease["workload"])
    while True:
        try:
            lease = read_lease(path)
        except FaultError:
            return "unreadable_lease"
        if lease is None or lease["status"] not in ACTIVE_STATES:
            return "stood_down"
        if clock() >= lease["expires_at"]:
            return recover(path, docker_run, by="reaper", clock=clock) or "stood_down"
        sleep(poll)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aiops.faults.reaper")
    parser.add_argument("--lease", required=True)
    args = parser.parse_args(argv)
    outcome = run(args.lease)
    print(f"reaper: {outcome}", flush=True)
    return 0 if outcome != "unreadable_lease" else 1


if __name__ == "__main__":
    sys.exit(main())
