"""`aiops <command>`: start | stop | status | doctor (and the older `serve`)."""
import argparse
import json
import signal
import sys
import threading

from aiops import lifecycle
from aiops.doctor import FAIL, format_results, run_doctor
from aiops.profile import ProfileError, load_profile

USAGE = "usage: aiops {start|stop|status|doctor} [--config PATH] [--json]   (or: aiops serve ...)"


def build_parser():
    parser = argparse.ArgumentParser(prog="aiops", usage=USAGE)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_ in (("start", "run the control plane in the foreground (explicit; no daemon)"),
                        ("stop", "stop the running control plane (safe if already stopped)"),
                        ("status", "report OBSERVED state of the control plane and workload"),
                        ("doctor", "read-only diagnosis of prerequisites")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--config", default="aiops.toml", help="runtime profile (TOML)")
        if name in ("status", "doctor"):
            p.add_argument("--json", action="store_true")
    return parser


def _doctor(args):
    try:
        profile = load_profile(args.config)
    except ProfileError as e:
        results = [{"check": "config", "status": FAIL, "detail": str(e), "blocking": True}]
        print(json.dumps(results, indent=2) if args.json else format_results(results))
        return 2
    results = run_doctor(profile)
    print(json.dumps(results, indent=2) if args.json else format_results(results))
    return 1 if any(r["status"] == FAIL for r in results) else 0


def _status(args):
    try:
        s = lifecycle.status(args.config)
    except ProfileError as e:
        print(f"configuration error: {e}")
        return 2
    print(json.dumps(s, indent=2) if args.json else lifecycle.format_status(s))
    return lifecycle.EXIT_RUNNING if s["control_plane"]["state"] == "running" else lifecycle.EXIT_STOPPED


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["serve"]:
        from aiops.serve import main as serve_main
        return serve_main(argv[1:])
    if not argv:
        print(USAGE)
        return 2
    args = build_parser().parse_args(argv)
    if args.command == "start":
        stop_event = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # graceful, never abrupt
            signal.signal(sig, lambda *_: stop_event.set())
        return lifecycle.start(args.config, stop_event)
    if args.command == "stop":
        return lifecycle.stop(args.config)
    if args.command == "status":
        return _status(args)
    return _doctor(args)
