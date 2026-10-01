from aiops.prometheus import TelemetryError


class RealTelemetry:
    """One observation of the real GPU and the real vLLM workload.

    A GPU read failure is a telemetry outage (it raises). vLLM being down is DATA: the probe
    fails and its metrics are marked unavailable; nothing is invented.
    """

    sources = {"gpu_memory_used_bytes": "nvidia-smi", "inference_probe": "vllm-probe",
               "vllm_metrics": "vllm-metrics"}

    def __init__(self, gpu, vllm):
        self.gpu, self.vllm = gpu, vllm

    def metrics(self):
        observation = dict(self.gpu())
        probe = self.vllm.probe()
        observation.update(inference_probe_ok=probe["ok"],
                           inference_probe_latency_ms=probe["latency_ms"],
                           inference_probe_error=probe["error"])
        try:
            observation.update(self.vllm.metrics())
            observation["vllm_metrics_available"] = True
        except TelemetryError:
            observation["vllm_metrics_available"] = False
        return observation
