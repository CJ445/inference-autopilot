import json
import urllib.parse
import urllib.request


class TelemetryError(Exception):
    pass


class PrometheusAdapter:
    """Reads named signals via the Prometheus instant-query API. Never invents values."""

    def __init__(self, base_url, queries, timeout=5):
        self.base_url, self.queries, self.timeout = base_url, queries, timeout

    def metrics(self):
        return {name: self._query(name, promql) for name, promql in self.queries.items()}

    def _query(self, name, promql):
        url = f"{self.base_url}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                body = json.load(resp)
        except (OSError, ValueError) as e:
            raise TelemetryError(f"prometheus unavailable: {e}") from e
        result = body.get("data", {}).get("result", [])
        if body.get("status") != "success" or not result:
            raise TelemetryError(f"no data for {name}")
        return float(result[0]["value"][1])
