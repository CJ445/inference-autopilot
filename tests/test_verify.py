import pytest

from aiops.kubectl import ClusterError
from aiops.prometheus import TelemetryError
from aiops.verify import real_verifier

THRESHOLD = 4_500_000_000


class Provider:
    def __init__(self, id_="new", ready=True, error=None):
        self.id_, self.ready, self.error = id_, ready, error

    def get_workload(self, name):
        if self.error:
            raise self.error
        return {"id": self.id_, "ready": self.ready}


class Vllm:
    """probe() answers from a script; metrics() works or raises."""

    def __init__(self, probes=(True,), metrics_ok=True):
        self.script, self.metrics_ok, self.probe_calls = list(probes), metrics_ok, 0

    def probe(self):
        ok = self.script[min(self.probe_calls, len(self.script) - 1)]
        self.probe_calls += 1
        return {"ok": ok, "latency_ms": 5.0, "error": None if ok else "timeout"}

    def metrics(self):
        if not self.metrics_ok:
            raise TelemetryError("vllm metrics unavailable")
        return {"vllm_requests_waiting": 0.0}


class Telemetry:
    def __init__(self, vllm, memory=2.9e9, gpu_ok=True):
        self.vllm, self.memory, self.gpu_ok = vllm, memory, gpu_ok

    def gpu(self):
        if not self.gpu_ok:
            raise TelemetryError("nvidia-smi unavailable")
        return {"gpu_memory_used_bytes": self.memory}


def verify(provider=None, vllm=None, telemetry=None, check_memory=False, sleeps=None,
           stable_probes=3):
    vllm = vllm or Vllm()
    verifier = real_verifier(provider or Provider(), telemetry or Telemetry(vllm), THRESHOLD,
                             check_memory, stable_probes=stable_probes, probe_interval=0.5,
                             sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return verifier("vllm", "old")


def test_restart_plus_stable_real_inference_passes_every_check():
    checks = verify()
    assert checks == {"workload_restarted": True, "gpu_observable": True,
                      "vllm_metrics_readable": True, "inference_probe_stable": True}
    assert all(checks.values())


def test_stable_window_requires_consecutive_probes_spaced_by_the_interval():
    vllm, sleeps = Vllm(probes=(True,)), []
    verify(vllm=vllm, sleeps=sleeps, stable_probes=3)
    assert vllm.probe_calls == 3 and sleeps == [0.5, 0.5]


@pytest.mark.parametrize("script", [(False,), (True, False, True), (True, True, False)])
def test_failing_or_flapping_inference_is_not_stable(script):
    checks = verify(vllm=Vllm(probes=script))
    assert checks["inference_probe_stable"] is False and not all(checks.values())


def test_a_failed_probe_stops_the_window_early():
    vllm = Vllm(probes=(True, False, True))
    verify(vllm=vllm)
    assert vllm.probe_calls == 2


def test_restart_that_changed_nothing_is_not_verified_even_if_inference_works():
    checks = verify(provider=Provider(id_="old"))
    assert checks["workload_restarted"] is False and not all(checks.values())


def test_metrics_endpoint_failing_is_not_verified_even_if_inference_works():
    checks = verify(vllm=Vllm(metrics_ok=False))
    assert checks["vllm_metrics_readable"] is False and not all(checks.values())


def test_metrics_up_but_inference_failing_is_not_verified():
    checks = verify(vllm=Vllm(probes=(False,), metrics_ok=True))
    assert checks["vllm_metrics_readable"] is True
    assert checks["inference_probe_stable"] is False and not all(checks.values())


def test_unobservable_gpu_is_not_verified():
    vllm = Vllm()
    checks = verify(vllm=vllm, telemetry=Telemetry(vllm, gpu_ok=False))
    assert checks["gpu_observable"] is False and not all(checks.values())


def test_docker_health_lagging_does_not_block_verification_inference_is_primary():
    # docker still says "starting" (ready=False) although inference already works
    checks = verify(provider=Provider(ready=False))
    assert all(checks.values()) and "workload_ready" not in checks


def test_provider_failure_is_a_failed_check_not_an_exception():
    checks = verify(provider=Provider(error=ClusterError("docker unavailable")))
    assert checks == {"workload_available": False}


def test_gpu_memory_is_checked_only_for_memory_pressure_incidents():
    vllm = Vllm()
    high = Telemetry(vllm, memory=5.2e9)
    assert "gpu_memory_below_threshold" not in verify(vllm=vllm, telemetry=high)
    checked = verify(vllm=vllm, telemetry=high, check_memory=True)
    assert checked["gpu_memory_below_threshold"] is False and not all(checked.values())
    ok = verify(vllm=vllm, telemetry=Telemetry(vllm, memory=2.9e9), check_memory=True)
    assert ok["gpu_memory_below_threshold"] is True and all(ok.values())
