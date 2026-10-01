import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
from test_engine import CONFIG, World

from aiops.api import make_server
from aiops.engine import Engine
from aiops.serve import Service, build_parser


def http(base, path, method="GET"):
    req = urllib.request.Request(base + path, method=method,
                                 data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def wait_for(fn, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_service_detects_on_its_own_then_approval_resolves_and_stop_leaves_nothing():
    w = World()
    w.fault, w.failures = True, 5
    svc = Service(Engine(w, w, CONFIG), port=0, interval=0.01)
    svc.start()
    base = f"http://127.0.0.1:{svc.port}"
    try:
        inc = wait_for(lambda: http(base, "/api/v1/incidents")["incidents"])[0]  # no manual tick
        assert inc["status"] == "POLICY_CHECK" and w.restarts == 0
        http(base, f"/api/v1/incidents/{inc['incident_id']}/remediation/approve", "POST")
        assert http(base, f"/api/v1/incidents/{inc['incident_id']}")["status"] == "RESOLVED"
    finally:
        svc.stop()
    with pytest.raises(urllib.error.URLError):  # port closed: nothing left running
        http(base, "/health")
    assert not [t for t in threading.enumerate() if t.name.startswith("aiops-")]


def test_tick_loop_survives_unexpected_errors():
    class Flaky(World):
        failures_left = 3

        def metrics(self):
            if self.failures_left:
                self.failures_left -= 1
                raise RuntimeError("boom")
            return super().metrics()

    w = Flaky()
    w.fault, w.failures = True, 5
    svc = Service(Engine(w, w, CONFIG), port=0, interval=0.01)
    svc.start()
    try:
        base = f"http://127.0.0.1:{svc.port}"
        assert wait_for(lambda: http(base, "/api/v1/incidents")["incidents"])
    finally:
        svc.stop()


def test_api_requests_use_the_lock_supplied_by_the_caller():
    lock = threading.Lock()
    server = make_server(Engine(World(), World(), CONFIG), port=0, lock=lock)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    done = threading.Event()
    try:
        with lock:  # e.g. the tick loop is mid-tick
            threading.Thread(target=lambda: (http(base, "/health"), done.set()),
                             daemon=True).start()
            assert not done.wait(0.3)  # request waits for the engine
        assert done.wait(3)
    finally:
        server.shutdown()


def test_cli_has_safe_defaults_and_requires_an_explicit_cluster_context():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["--prometheus-url", "http://x"])       # no --context
    with pytest.raises(SystemExit):
        p.parse_args(["--context", "kind-x"])                # no --prometheus-url
    a = p.parse_args(["--context", "kind-x", "--prometheus-url", "http://x"])
    assert (a.port, a.namespace, a.workload, a.interval) == (8080, "default", "vllm-0", 5)
    assert not hasattr(a, "host")  # loopback only; no way to expose the API


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_process_degrades_without_prometheus_and_stops_cleanly_on_sigterm(tmp_path):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "aiops", "serve", "--context", "kind-does-not-exist",
         "--prometheus-url", "http://127.0.0.1:1", "--port", str(port),
         "--db", str(tmp_path / "aiops.db"), "--interval", "0.2"],
        stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        def degraded():
            assert proc.poll() is None, proc.stderr.read()
            try:
                return http(base, "/health")["status"] == "DEGRADED"
            except urllib.error.URLError:
                return False  # still starting up

        wait_for(degraded, timeout=15)  # a real Prometheus outage -> DEGRADED
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    with pytest.raises(urllib.error.URLError):
        http(base, "/health")
    assert os.path.exists(tmp_path / "aiops.db")
