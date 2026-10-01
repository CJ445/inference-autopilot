import json
import subprocess


class ClusterError(Exception):
    pass


class KubernetesProvider:
    """Real Kubernetes access via fixed kubectl argv; never a shell string."""

    def __init__(self, namespace, run=subprocess.run, metrics_source=None, context=None):
        self.namespace, self._run, self._metrics = namespace, run, metrics_source
        self.context = context

    def metrics(self):
        if self._metrics is None:
            raise RuntimeError("no telemetry source configured")
        return self._metrics.metrics()

    def _kubectl(self, *args):
        ctx = ["--context", self.context] if self.context else []
        argv = ["kubectl", *ctx, "-n", self.namespace, *args]
        try:
            result = self._run(argv, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise ClusterError(e.stderr or str(e)) from e
        return result.stdout

    def get_workload(self, name):
        pod = json.loads(self._kubectl("get", "pod", name, "-o", "json"))
        conditions = pod["status"].get("conditions", [])
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)
        return {"id": pod["metadata"]["uid"], "ready": ready}

    def restart_workload(self, name):
        self._kubectl("delete", "pod", name, "--wait=true")
        return "ok"
