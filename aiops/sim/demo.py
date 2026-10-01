"""`aiops demo`: the golden simulation scenario from the command line. No config, no infrastructure."""
import argparse
import os
import sys
import tempfile

from aiops.sim.scenario import GOLDEN, SCENARIOS
from aiops.sim.session import SimSession

BANNER = ("SIMULATION: nothing here touches your GPU, Docker or any real workload.\n"
          "Every event below is produced by the real control loop over a synthetic world.")


def main(argv, out=print, ask=input, isatty=None):
    parser = argparse.ArgumentParser(prog="aiops demo")
    parser.add_argument("--scenario", default=GOLDEN, choices=sorted(SCENARIOS))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true", help="approve the proposed restart")
    group.add_argument("--reject", action="store_true", help="reject the proposed restart")
    args = parser.parse_args(argv)

    interactive = isatty() if isatty else sys.stdin.isatty() and sys.stdout.isatty()
    if not (args.approve or args.reject or interactive):
        out("aiops demo needs a decision when it is not run in a terminal: pass --approve or "
            "--reject.")
        return 2

    def decide(incident):
        if args.approve:
            return "approve"
        if args.reject:
            return "reject"
        answer = ask(f"\nApprove the simulated restart for {incident.incident_id}? [y/N] ")
        return "approve" if answer.strip().lower() in ("y", "yes") else "reject"

    out(BANNER + f"\nScenario: {args.scenario}\n")
    with tempfile.TemporaryDirectory(prefix="aiops-sim-") as scratch:
        session = SimSession(os.path.join(scratch, "simulation.db"))
        result = SCENARIOS[args.scenario](
            session, decide, emit=lambda s: out(f"[{s['n']:>2}] {s['title']}"
                                                + (f": {s['detail']}" if s["detail"] else "")))
    out(f"\nSIMULATION complete: incident {result.incident.incident_id} {result.outcome} "
        "(simulated; nothing real changed).")
    if args.approve:
        return 0 if result.outcome == "RESOLVED" else 1
    return 0 if result.outcome in ("REJECTED", "RESOLVED") else 1
