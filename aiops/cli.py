"""`aiops <command>`: start | stop | status | doctor (and the older `serve`)."""
import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from aiops import lifecycle
from aiops.doctor import FAIL, format_results, run_doctor
from aiops.profile import ProfileError, default_config_path, load_profile

USAGE = ("usage: aiops [--config PATH]   (no command: open the operator UI, starting the control\n"
         "                                  plane first if it is not running)\n"
         "       aiops {start|stop|status|doctor} [--config PATH] [--json]\n"
         "       aiops tui [--config PATH] [--url URL] [-- TUI ARGS]   (the operator terminal UI)\n"
         "       aiops demo [--scenario NAME] [--approve | --reject]   (SIMULATION: no infrastructure)\n"
         "       aiops serve ...   (unmanaged legacy)")


def build_parser():
    parser = argparse.ArgumentParser(prog="aiops", usage=USAGE.removeprefix("usage: "))
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_ in (("start", "run the control plane in the foreground (explicit; no daemon)"),
                        ("stop", "stop the running control plane (safe if already stopped)"),
                        ("status", "report OBSERVED state of the control plane and workload"),
                        ("doctor", "read-only diagnosis of prerequisites")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--config", default=default_config_path(),
                       help="runtime profile (TOML); default ./aiops.toml, else ~/.config/aiops/aiops.toml")
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


TUI_BUILD = "cargo build --release --manifest-path tui/Cargo.toml"


def _tui_binary():
    """The Rust operator TUI: $AIOPS_TUI_BIN, else the release or debug build in this checkout."""
    explicit = os.environ.get("AIOPS_TUI_BIN")
    if explicit:
        return Path(explicit)
    root = Path(__file__).resolve().parent.parent / "tui" / "target"
    return next((p for p in (root / "release" / "aiops-tui", root / "debug" / "aiops-tui")
                 if p.exists()), root / "release" / "aiops-tui")


def _tui(argv):
    """Hand the terminal to the Rust TUI. It is a client of the HTTP API only: this command
    starts no control plane, reads no state and touches no database."""
    parser = argparse.ArgumentParser(prog="aiops tui")
    parser.add_argument("--config", default=default_config_path())
    parser.add_argument("--url")
    args, extra = parser.parse_known_args(argv)
    if args.url:
        url = args.url
    else:
        try:
            url = f"http://127.0.0.1:{load_profile(args.config)['control_plane']['port']}"
        except ProfileError as e:
            print(f"configuration error: {e}")
            return 2
    binary = _tui_binary()
    if not binary.exists():
        print(f"the operator TUI is not built ({binary}).\nBuild it once with: {TUI_BUILD}")
        return 1
    if not os.access(binary, os.X_OK):
        print(f"{binary} is not executable")
        return 1
    sys.stdout.flush()
    os.execv(str(binary), [str(binary), "--url", url, *extra])   # replaces this process


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["tui"]:
        return _tui(argv[1:])
    if argv[:1] == ["demo"]:
        from aiops.sim.demo import main as demo_main
        return demo_main(argv[1:])
    if argv[:1] == ["serve"]:
        from aiops.serve import main as serve_main
        return serve_main(argv[1:])
    if not argv or argv[0] == "--config":
        from aiops.launcher import main as launcher_main
        return launcher_main(argv)
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
