import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from aiops.prometheus import TelemetryError
from aiops.vllm import VllmClient, parse_metrics

FIXTURE = Path(__file__).parent / "fixtures" / "vllm_v0.10.0_metrics_after_one_request.txt"
REAL = FIXTURE.read_text()  # a genuine scrape from vLLM v0.10.0 on the RTX 4060


def test_real_scrape_parses_labeled_samples():
    samples = parse_metrics(REAL)
    success = {labels["finished_reason"]: v for n, labels, v in samples
               if n == "vllm:request_success_total"}
    # one series per finished_reason, and vLLM files aborts under "success" too
    assert success == {"stop": 0.0, "length": 1.0, "abort": 0.0}


def test_malformed_exposition_is_rejected():
    with pytest.raises(ValueError):
        parse_metrics("vllm:num_requests_waiting\n")


class Server:
    """A real HTTP server shaped like vLLM's: /metrics, /v1/completions. Behaviour is mutable."""

    def __init__(self):
        self.metrics_text, self.completion = REAL, (200, {"choices": [{"text": " Paris"}]})
        self.completion_delay, self.requests = 0, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(("GET", self.path))
                body = outer.metrics_text.encode()
                self.send_response(200 if self.path == "/metrics" else 404)
                self.end_headers()
                self.wfile.write(body if self.path == "/metrics" else b"")

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append(("POST", self.path, json.loads(self.rfile.read(n))))
                time.sleep(outer.completion_delay)
                status, body = outer.completion
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"


@pytest.fixture
def server():
    s = Server()
    yield s
    s.httpd.shutdown()


def client(server, **kw):
    return VllmClient(server.url, "facebook/opt-125m", timeout=kw.pop("timeout", 2), **kw)


def test_metrics_are_read_from_the_real_scrape(server):
    m = client(server).metrics()
    assert m["vllm_requests_succeeded_total"] == 1.0           # stop + length, NOT abort
    assert m["vllm_requests_aborted_total"] == 0.0
    assert m["vllm_requests_running"] == 0.0 and m["vllm_requests_waiting"] == 0.0
    assert m["vllm_kv_cache_usage"] == pytest.approx(0.0002458814851241664)
    assert m["vllm_e2e_latency_seconds_count"] == 1.0
    assert m["vllm_e2e_latency_seconds_sum"] > 0


def test_a_required_metric_missing_raises_instead_of_inventing_a_value(server):
    server.metrics_text = "\n".join(l for l in REAL.splitlines()
                                    if "num_requests_waiting" not in l)
    with pytest.raises(TelemetryError, match="num_requests_waiting"):
        client(server).metrics()


def test_unreachable_vllm_metrics_raise_telemetry_error():
    with pytest.raises(TelemetryError):
        VllmClient("http://127.0.0.1:1", "m", timeout=0.5).metrics()


def test_unparseable_metrics_raise_telemetry_error(server):
    server.metrics_text = "this is not prometheus text {{{"
    with pytest.raises(TelemetryError):
        client(server).metrics()


def test_probe_succeeds_only_on_a_real_completion_and_sends_a_fixed_small_request(server):
    p = client(server).probe()
    assert p["ok"] is True and p["error"] is None and p["latency_ms"] >= 0
    method, path, body = server.requests[-1]
    assert (method, path) == ("POST", "/v1/completions")
    assert body == {"model": "facebook/opt-125m", "prompt": "Hello", "max_tokens": 4,
                    "temperature": 0}


@pytest.mark.parametrize("completion, why", [
    ((500, {"error": "boom"}), "http 500"),
    ((200, b"not json"), "unparseable body"),
    ((200, {"choices": []}), "no choices: metrics up but inference broken"),
    ((200, {"object": "list"}), "no choices key"),
    ((200, {"choices": [{"index": 0}]}), "choice without text"),
])
def test_probe_fails_when_the_response_is_not_a_real_completion(server, completion, why):
    server.completion = completion
    p = client(server).probe()
    assert p["ok"] is False and p["error"], why


def test_probe_fails_on_timeout_instead_of_hanging(server):
    server.completion_delay = 1.0
    start = time.monotonic()
    p = client(server, timeout=0.2).probe()
    assert p["ok"] is False and "timeout" in p["error"].lower()
    assert time.monotonic() - start < 0.9


def test_probe_fails_when_nothing_is_listening():
    p = VllmClient("http://127.0.0.1:1", "m", timeout=0.5).probe()
    assert p["ok"] is False and p["error"]


def test_aborted_requests_are_not_counted_as_successes(server):
    server.metrics_text = REAL.replace(
        'finished_reason="abort",model_name="facebook/opt-125m"} 0.0',
        'finished_reason="abort",model_name="facebook/opt-125m"} 3.0')
    m = client(server).metrics()
    assert m["vllm_requests_succeeded_total"] == 1.0 and m["vllm_requests_aborted_total"] == 3.0


# --- malformed HTTP must be a failed probe / telemetry error, never an exception ----------

import socket  # noqa: E402


class RawServer:
    """Answers every connection with fixed bytes, then closes: a broken or truncated server."""

    def __init__(self, payload):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.payload = payload
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(self.payload)
            finally:
                conn.close()

    def close(self):
        self.sock.close()


BROKEN = {
    "not http at all": b"garbage that is not http\r\n\r\n",
    "truncated body": b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\nshort",
    "closed without answering": b"",
}


@pytest.mark.parametrize("name", list(BROKEN))
def test_probe_treats_a_malformed_http_response_as_a_failed_probe(name):
    s = RawServer(BROKEN[name])
    try:
        p = VllmClient(s.url, "m", timeout=2).probe()
    finally:
        s.close()
    assert p["ok"] is False and p["error"]


@pytest.mark.parametrize("name", list(BROKEN))
def test_metrics_treats_a_malformed_http_response_as_a_telemetry_error(name):
    s = RawServer(BROKEN[name])
    try:
        with pytest.raises(TelemetryError):
            VllmClient(s.url, "m", timeout=2).metrics()
    finally:
        s.close()
