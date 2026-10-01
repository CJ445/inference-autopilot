"""`python -m aiops serve`: the tick loop and the local API in one process."""
import argparse
import signal
import threading
import traceback

from aiops.api import make_server
from aiops.engine import Engine
from aiops.kubectl import KubectlCluster
from aiops.prometheus import PrometheusAdapter
from aiops.store import Store

# PromQL for the signals the engine reads (the CPU stand-in's labels; see deploy/kubernetes).
QUERIES = {
    "gpu_memory_used_bytes": 'gpu_memory_used_bytes{job="vllm"}',
    "error_rate": 'error_rate{job="vllm"}',
    "allocation_failures_total": 'allocation_failures_total{job="vllm"}',
}


class Service:
    def __init__(self, engine, port, interval):
        self.engine, self.interval = engine, interval
        self._lock, self._stop = threading.Lock(), threading.Event()
        self.server = make_server(engine, port=port, lock=self._lock)  # loopback only
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
    p.add_argument("--pod", default="vllm-0")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--gpu-threshold", type=int, default=7_500_000_000)
    p.add_argument("--error-rate-limit", type=float, default=0.05)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    prom = PrometheusAdapter(args.prometheus_url, QUERIES)
    cluster = KubectlCluster(args.namespace, context=args.context, metrics_source=prom)
    config = {"service": "vllm", "pod": args.pod, "gpu_threshold": args.gpu_threshold,
              "error_rate_limit": args.error_rate_limit}
    service = Service(Engine(prom, cluster, config, store=Store(args.db)),
                      port=args.port, interval=args.interval)

    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    service.start()
    done.wait()
    service.stop()
    return 0
