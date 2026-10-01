"""Read vLLM's real /metrics and probe real inference. Never invents a value."""
import json
import re
import time
import urllib.error
import urllib.request

from aiops.prometheus import TelemetryError

SAMPLE = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

# engine key -> vLLM v0.10.0 metric family, summed over labels. request_success_total is
# handled separately: it has one series per finished_reason and vLLM counts "abort" in it.
METRICS = {
    "vllm_requests_running": "vllm:num_requests_running",
    "vllm_requests_waiting": "vllm:num_requests_waiting",
    "vllm_kv_cache_usage": "vllm:kv_cache_usage_perc",  # a 0..1 fraction in v0.10.0
    "vllm_e2e_latency_seconds_sum": "vllm:e2e_request_latency_seconds_sum",
    "vllm_e2e_latency_seconds_count": "vllm:e2e_request_latency_seconds_count",
}


def parse_metrics(text):
    """Prometheus text exposition -> [(name, labels, value)]. Raises ValueError if malformed."""
    samples = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        m = SAMPLE.fullmatch(line.strip())
        if not m:
            raise ValueError(f"malformed metrics line: {line[:80]!r}")
        samples.append((m.group(1), dict(LABEL.findall(m.group(2) or "")), float(m.group(3))))
    return samples


class VllmClient:
    def __init__(self, base_url, model, timeout=5):
        self.base_url, self.model, self.timeout = base_url, model, timeout

    def metrics(self):
        try:
            with urllib.request.urlopen(self.base_url + "/metrics", timeout=self.timeout) as r:
                samples = parse_metrics(r.read().decode())
        except (OSError, ValueError) as e:
            raise TelemetryError(f"vllm metrics unavailable: {e}") from e
        out = {}
        for key, family in METRICS.items():
            values = [v for n, _, v in samples if n == family]
            if not values:
                raise TelemetryError(f"vllm metric missing: {family}")
            out[key] = sum(values)
        finished = [(l.get("finished_reason"), v) for n, l, v in samples
                    if n == "vllm:request_success_total"]
        if not finished:
            raise TelemetryError("vllm metric missing: vllm:request_success_total")
        out["vllm_requests_succeeded_total"] = sum(v for r, v in finished if r != "abort")
        out["vllm_requests_aborted_total"] = sum(v for r, v in finished if r == "abort")
        return out

    def probe(self):
        """One small real completion. ok only if the response contains a real completion."""
        body = json.dumps({"model": self.model, "prompt": "Hello", "max_tokens": 4,
                           "temperature": 0}).encode()
        request = urllib.request.Request(
            self.base_url + "/v1/completions", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        start = time.monotonic()
        error = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                text = json.load(r)["choices"][0]["text"]
            if not isinstance(text, str):
                error = "bad response: completion text is not a string"
        except urllib.error.HTTPError as e:
            error = f"http {e.code}"
        except urllib.error.URLError as e:
            error = "timeout" if isinstance(e.reason, TimeoutError) else f"unreachable: {e.reason}"
        except TimeoutError:
            error = "timeout"
        except (ValueError, KeyError, IndexError, TypeError):
            error = "bad response: no completion in body"
        except OSError as e:
            error = f"unreachable: {e}"
        return {"ok": error is None, "latency_ms": (time.monotonic() - start) * 1000,
                "error": error}
