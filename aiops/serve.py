"""`python -m aiops serve`: the tick loop and the local API in one process."""
import argparse
import signal
import sys
import threading
import traceback

from aiops.api import make_server
from aiops.runtime import QUERIES, build_engine  # noqa: F401  (QUERIES re-exported)

class Service:
    def __init__(self, engine, port, interval, info=None, status_extra=None, system=None,
                 practice=None):
        self.engine, self.interval, self.practice = engine, interval, practice
        self._lock, self._stop = threading.Lock(), threading.Event()
        self.server = make_server(engine, port=port, lock=self._lock, info=info,  # loopback only
                                  status_extra=status_extra, system=system, practice=practice)
        self.port = self.server.server_address[1]
        self._threads = []

    def start(self):
        self._threads = [
            threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                             name="aiops-api"),
            threading.Thread(target=self._loop, name="aiops-tick"),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        if self.practice is not None:
            self.practice.shutdown()
        self.server.shutdown()
        self.server.server_close()
        for t in self._threads:
            t.join()

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                try:
                    self.engine.tick()
                except Exception:  # one bad tick must not kill the loop
                    traceback.print_exc()
            self._stop.wait(self.interval)


def build_parser():
    p = argparse.ArgumentParser(prog="aiops serve")
    p.add_argument("--context", required=True, help="kubectl context; never defaults")
    p.add_argument("--prometheus-url", required=True)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--db", default="aiops.db")
    p.add_argument("--namespace", default="default")
    p.add_argument("--workload", default="vllm-0")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--gpu-threshold", type=int, default=7_500_000_000)
    p.add_argument("--error-rate-limit", type=float, default=0.05)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    print("aiops serve: UNMANAGED legacy command: no watchdog, no preflight, no lifecycle state. "
          "Use `aiops start` for a protected control plane.", file=sys.stderr, flush=True)
    profile = {  # the same builder `aiops start` uses, fed from flags instead of a file
        "name": "kubernetes", "provider": "kubernetes",
        "control_plane": {"db": args.db},
        "workload": {"name": args.workload, "namespace": args.namespace,
                     "context": args.context, "prometheus_url": args.prometheus_url},
        "safety": {"gpu_memory_threshold_bytes": args.gpu_threshold,
                   "error_rate_limit": args.error_rate_limit},
        "verification": {"timeout": 60, "interval": 2},
    }
    service = Service(build_engine(profile), port=args.port, interval=args.interval)

    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    service.start()
    done.wait()
    service.stop()
    return 0
