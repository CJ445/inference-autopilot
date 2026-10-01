"""Recovery verification for the real vLLM path.

The real inference probe is the primary application-recovery signal. Docker health is
deliberately not consulted: it lags actual recovery (it still read "starting" when inference
already worked), and a restart's API success proves nothing.
"""
import time

from aiops.kubectl import ClusterError
from aiops.prometheus import TelemetryError


def real_verifier(provider, telemetry, gpu_threshold, check_memory, stable_probes=3,
                  probe_interval=1.0, sleep=time.sleep):
    """Returns verify(workload, id_before) -> {check name: bool}; every check must be True."""

    def verify(workload, id_before):
        try:
            state = provider.get_workload(workload)
        except ClusterError:
            return {"workload_available": False}
        checks = {"workload_restarted": state["id"] != id_before}  # lifecycle (StartedAt)
        try:
            gpu = telemetry.gpu()
            checks["gpu_observable"] = True
            if check_memory:
                checks["gpu_memory_below_threshold"] = (
                    gpu["gpu_memory_used_bytes"] < gpu_threshold)
        except TelemetryError:
            checks["gpu_observable"] = False
        try:
            telemetry.vllm.metrics()
            checks["vllm_metrics_readable"] = True
        except TelemetryError:
            checks["vllm_metrics_readable"] = False
        checks["inference_probe_stable"] = _stable(telemetry.vllm, stable_probes,
                                                   probe_interval, sleep)
        return checks

    return verify


def _stable(vllm, count, interval, sleep):
    """A short bounded window: `count` consecutive real completions must all succeed."""
    for n in range(count):
        if not vllm.probe()["ok"]:
            return False
        if n < count - 1:
            sleep(interval)
    return True
