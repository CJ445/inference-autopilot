"""Golden slice on a real kind cluster. Opt in: AIOPS_INTEGRATION=1 pytest tests/integration

Creates a dedicated cluster (never reuses an existing one) and always deletes it.
Every kubectl call is pinned to that cluster's context.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from aiops.kubectl import KubernetesProvider
from aiops.prometheus import PrometheusAdapter, TelemetryError

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_INTEGRATION") != "1", reason="set AIOPS_INTEGRATION=1 to run")

CLUSTER = "aiops-test"
CONTEXT = f"kind-{CLUSTER}"
DEPLOY = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "kubernetes")
IMAGES = ["python:3.12-slim", "prom/prometheus:v2.53.0"]  # pinned in deploy/kubernetes
THRESHOLD = 7_500_000_000
QUERIES = {
    "gpu_memory_used_bytes": 'gpu_memory_used_bytes{job="vllm"}',
    "error_rate": 'error_rate{job="vllm"}',
    "allocation_failures_total": 'allocation_failures_total{job="vllm"}',
}


def sh(*argv, **kw):
    r = subprocess.run(argv, capture_output=True, text=True, **kw)
    if r.returncode:
        raise AssertionError(f"{' '.join(argv)} failed:\n{r.stdout}\n{r.stderr}")
    return r


def kubectl(*args):
    return sh("kubectl", "--context", CONTEXT, *args).stdout


def wait_for(fn, timeout, what):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            if fn():
                return
        except (subprocess.CalledProcessError, TelemetryError) as e:
            last = e
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what} (last error: {last})")


@pytest.fixture(scope="module")
def prom_url():
    assert CLUSTER not in sh("kind", "get", "clusters").stdout.split(), \
        f"cluster {CLUSTER} already exists; refusing to reuse or delete it"
    forward = None
    try:
        for image in IMAGES:  # cache on the host once; avoids re-pulling into every new cluster
            if subprocess.run(["docker", "image", "inspect", image],
                              capture_output=True).returncode:
                sh("docker", "pull", image)
        sh("kind", "create", "cluster", "--name", CLUSTER, "--wait", "120s")
        for image in IMAGES:
            sh("kind", "load", "docker-image", image, "--name", CLUSTER)
        for f in ("workload.yaml", "prometheus.yaml"):
            kubectl("apply", "-f", os.path.join(DEPLOY, f))
        try:
            kubectl("rollout", "status", "statefulset/vllm", "--timeout=240s")
            kubectl("rollout", "status", "deployment/prometheus", "--timeout=240s")
        except AssertionError as e:
            diag = subprocess.run(["kubectl", "--context", CONTEXT, "describe", "pods"],
                                  capture_output=True, text=True).stdout[-3000:]
            raise AssertionError(f"{e}\n--- pod events ---\n{diag}") from e
        forward = subprocess.Popen(
            ["kubectl", "--context", CONTEXT, "port-forward", "svc/prometheus", "19090:9090"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        yield "http://127.0.0.1:19090"
    finally:
        if forward:
            forward.terminate()
        subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER])


def http(base, path, method="GET", timeout=10):
    req = urllib.request.Request(base + path, method=method,
                                 data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def test_fault_to_verified_recovery_over_http_against_a_real_serve_process(prom_url, tmp_path):
    prom = PrometheusAdapter(prom_url, QUERIES)
    cluster = KubernetesProvider("default", context=CONTEXT, metrics_source=prom)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    serve = subprocess.Popen(
        [sys.executable, "-m", "aiops", "serve", "--context", CONTEXT,
         "--prometheus-url", prom_url, "--port", str(port), "--db", str(tmp_path / "aiops.db"),
         "--interval", "1", "--gpu-threshold", str(THRESHOLD)])
    try:
        # healthy baseline: the loop runs on its own and opens nothing
        wait_for(lambda: prom.metrics()["gpu_memory_used_bytes"] > 0, 60, "first scrape")
        wait_for(lambda: http(base, "/health")["status"] == "HEALTHY", 30, "serve up")
        time.sleep(3)
        assert http(base, "/api/v1/incidents")["incidents"] == []
        uid_before = cluster.get_workload("vllm-0")["id"]

        # controlled fault (actor: FAULT_INJECTOR), bounded: lives only in the pod process
        kubectl("exec", "vllm-0", "--", "python", "-c",
                "import urllib.request as u;"
                "u.urlopen(u.Request('http://localhost:8000/fault', method='POST'))")

        # nobody calls tick(): the running process detects, diagnoses, and waits for approval
        wait_for(lambda: http(base, "/api/v1/incidents")["incidents"], 60, "incident")
        (incident,) = http(base, "/api/v1/incidents")["incidents"]
        assert incident["status"] == "POLICY_CHECK"
        assert incident["category"] == "GPU_MEMORY_PRESSURE"
        assert len(incident["evidence"]) >= 2
        assert incident["proposal"] == {"action": "restart_workload",
                                        "parameters": {"workload": "vllm-0"}}
        assert cluster.get_workload("vllm-0")["id"] == uid_before  # nothing changed pre-approval

        done = http(base, f"/api/v1/incidents/{incident['incident_id']}/remediation/approve",
                    "POST", timeout=150)

        # judged from independently observed state, not from the API's own claim
        assert done["status"] == "RESOLVED", done
        assert cluster.get_workload("vllm-0")["id"] != uid_before
        assert prom.metrics()["gpu_memory_used_bytes"] < THRESHOLD
        assert len(http(base, "/api/v1/incidents")["incidents"]) == 1  # no duplicates after

        serve.send_signal(signal.SIGTERM)
        assert serve.wait(timeout=15) == 0
        with pytest.raises(urllib.error.URLError):  # nothing left running
            http(base, "/health")
    finally:
        if serve.poll() is None:
            serve.kill()
