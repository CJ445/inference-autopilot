import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from aiops.prometheus import PrometheusAdapter, TelemetryError

QUERIES = {
    "gpu_memory_used_bytes": "sum(DCGM_FI_DEV_FB_USED)",
    "error_rate": "rate(errors[1m])",
}


@pytest.fixture
def prom():
    """Real HTTP server speaking the Prometheus instant-query API."""
    data = {}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)["query"][0]
            seen.append((url.path, q))
            result = [] if q not in data else [{"metric": {}, "value": [1.0, str(data[q])]}]
            body = json.dumps({"status": "success",
                               "data": {"resultType": "vector", "result": result}})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", data, seen
    server.shutdown()


def test_metrics_returns_observed_values_via_instant_query_api(prom):
    url, data, seen = prom
    data["sum(DCGM_FI_DEV_FB_USED)"] = 4_000_000_000
    data["rate(errors[1m])"] = 0.01
    m = PrometheusAdapter(url, QUERIES).metrics()
    assert m == {"gpu_memory_used_bytes": 4_000_000_000.0, "error_rate": 0.01}
    assert {p for p, _ in seen} == {"/api/v1/query"}


def test_missing_metric_raises_instead_of_inventing_a_value(prom):
    url, data, _ = prom
    data["sum(DCGM_FI_DEV_FB_USED)"] = 1
    with pytest.raises(TelemetryError, match="error_rate"):
        PrometheusAdapter(url, QUERIES).metrics()


def test_unreachable_prometheus_raises_telemetry_error():
    with pytest.raises(TelemetryError):
        PrometheusAdapter("http://127.0.0.1:1", QUERIES, timeout=0.5).metrics()
