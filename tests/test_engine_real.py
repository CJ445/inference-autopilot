"""The engine on the real-GPU observation shape: nvidia-smi + vLLM probe, no Prometheus."""
import pytest
from test_engine import World

from aiops.engine import Engine, NothingToApprove
from aiops.rca import diagnose
from aiops.telemetry import RealTelemetry

CONFIG = {"service": "vllm", "workload": "vllm", "gpu_threshold": 4_500_000_000,
          "error_rate_limit": 0.05, "timeout": 1, "interval": 0.01}
UUID = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f"


class FakeVllm:
    def __init__(self):
        self.ok, self.error = True, None

    def probe(self):
        return {"ok": self.ok, "latency_ms": 12.0, "error": self.error}

    def metrics(self):
        from aiops.prometheus import TelemetryError
        if not self.ok:
            raise TelemetryError("down")
        return {"vllm_requests_running": 0.0, "vllm_requests_waiting": 0.0}


class Rig:
    def __init__(self, memory_bytes=2.9e9):
        self.memory, self.vllm = memory_bytes, FakeVllm()
        self.cluster = World()
        self.telemetry = RealTelemetry(self._gpu, self.vllm)
        self.engine = Engine(self.telemetry, self.cluster, CONFIG)

    def _gpu(self):
        return {"gpu_uuid": UUID, "gpu_memory_used_bytes": self.memory,
                "gpu_memory_total_bytes": 8.6e9, "gpu_temperature_c": 59.0,
                "gpu_utilization_percent": 0.0}

    def fail_probe(self):
        self.vllm.ok, self.vllm.error = False, "timeout"


def test_memory_pressure_plus_failing_probe_is_diagnosed_from_real_sources():
    r = Rig(memory_bytes=5.2e9)
    r.fail_probe()
    inc = r.engine.tick()
    assert inc.status == "POLICY_CHECK" and inc.category == "GPU_MEMORY_PRESSURE"
    by_metric = {e["metric"]: e for e in inc.evidence}
    gpu, probe = by_metric["gpu_memory_used_bytes"], by_metric["inference_probe"]
    assert gpu["source"] == "nvidia-smi" and gpu["resource"] == UUID
    assert gpu["value"] == 5.2e9 and gpu["relation"] == "supports"
    assert probe["source"] == "vllm-probe" and probe["relation"] == "supports"
    assert probe["value"]["ok"] is False and probe["value"]["error"] == "timeout"
    assert "vllm_allocation_failure" not in by_metric  # never invented
    assert r.engine.pending[inc.incident_id] == {
        "action": "restart_workload", "parameters": {"workload": "vllm"}}
    assert r.cluster.restarts == 0


def test_memory_pressure_with_a_healthy_probe_is_insufficient_evidence_and_never_restarts():
    r = Rig(memory_bytes=5.2e9)
    inc = r.engine.tick()
    assert inc.status == "INSUFFICIENT_EVIDENCE"
    probe = next(e for e in inc.evidence if e["metric"] == "inference_probe")
    assert probe["relation"] == "contradicts"
    assert r.engine.pending == {}
    with pytest.raises(NothingToApprove):
        r.engine.approve(inc.incident_id)
    assert r.cluster.restarts == 0


def test_normal_memory_creates_no_incident():
    r = Rig(memory_bytes=2.9e9)
    r.fail_probe()
    assert r.engine.tick() is None  # known gap: an unresponsive workload at normal memory


def test_insufficient_incident_is_rediagnosed_when_the_probe_later_fails():
    r = Rig(memory_bytes=5.2e9)
    first = r.engine.tick()
    assert first.status == "INSUFFICIENT_EVIDENCE"

    r.fail_probe()
    again = r.engine.tick()

    assert again is first and len(r.engine.incidents) == 1   # same incident, not a duplicate
    assert first.status == "POLICY_CHECK" and first.incident_id in r.engine.pending
    probe = next(e for e in first.evidence if e["metric"] == "inference_probe")
    assert probe["relation"] == "supports"
    states = [t["state"] for t in first.timeline]
    assert states.count("TRIAGING") == 2 and states[-1] == "POLICY_CHECK"


def test_unchanged_insufficient_incident_does_not_spam_the_audit_log():
    r = Rig(memory_bytes=5.2e9)
    r.engine.tick()
    n = len(r.engine.audit.events)
    for _ in range(5):
        r.engine.tick()
    assert len(r.engine.audit.events) == n and len(r.engine.incidents) == 1


def test_rca_counts_a_failed_probe_as_supporting_and_a_healthy_probe_as_not():
    gpu = {"evidence_id": "e1", "metric": "gpu_memory_used_bytes", "relation": "supports"}
    failed = {"evidence_id": "e2", "metric": "inference_probe", "relation": "supports"}
    healthy = {"evidence_id": "e2", "metric": "inference_probe", "relation": "contradicts"}
    assert diagnose([gpu, failed])["root_cause"]["category"] == "GPU_MEMORY_PRESSURE"
    assert diagnose([gpu, healthy])["insufficient_evidence"] is True


def test_gpu_outage_degrades_the_engine_without_inventing_an_incident():
    from aiops.prometheus import TelemetryError

    r = Rig(memory_bytes=5.2e9)

    def broken():
        raise TelemetryError("nvidia-smi unavailable")

    r.telemetry.gpu = broken
    assert r.engine.tick() is None and r.engine.health == "DEGRADED"
    assert r.engine.incidents == []
