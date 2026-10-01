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


def test_healthy_gpu_and_successful_probe_create_no_incident():
    r = Rig(memory_bytes=2.9e9)
    assert r.engine.tick() is None
    assert r.engine.incidents == [] and r.engine.health == "HEALTHY"


def test_normal_gpu_with_a_failed_probe_is_inference_unresponsive_from_real_evidence():
    r = Rig(memory_bytes=2.9e9)   # memory well under the threshold: NOT pressure
    r.fail_probe()
    inc = r.engine.tick()
    assert inc.category == "INFERENCE_UNRESPONSIVE" and inc.status == "POLICY_CHECK"
    by_metric = {e["metric"]: e for e in inc.evidence}
    probe = by_metric["inference_probe"]
    assert probe["source"] == "vllm-probe" and probe["relation"] == "supports"
    assert probe["value"]["ok"] is False and probe["value"]["error"] == "timeout"
    gpu = by_metric["gpu_memory_used_bytes"]
    assert gpu["source"] == "nvidia-smi" and gpu["resource"] == UUID
    assert gpu["relation"] == "supports" and gpu["value"] == 2.9e9  # normal, so not pressure
    assert by_metric["vllm_metrics"]["source"] == "vllm-metrics"
    assert by_metric["vllm_metrics"]["relation"] == "supports"      # /metrics down too
    assert r.engine.pending[inc.incident_id] == {
        "action": "restart_workload", "parameters": {"workload": "vllm"}}
    assert r.cluster.restarts == 0


def test_unresponsive_incident_is_deduplicated_across_ticks():
    r = Rig(memory_bytes=2.9e9)
    r.fail_probe()
    first = r.engine.tick()
    assert r.engine.tick() is first and len(r.engine.incidents) == 1


def test_gpu_pressure_alone_never_creates_an_inference_unresponsive_incident():
    r = Rig(memory_bytes=5.2e9)   # pressure, probe healthy
    r.engine.tick()
    assert [i.category for i in r.engine.incidents] == ["GPU_MEMORY_PRESSURE"]


def test_pressure_with_a_failing_probe_keeps_the_existing_pressure_path_only():
    r = Rig(memory_bytes=5.2e9)
    r.fail_probe()
    inc = r.engine.tick()
    assert inc.category == "GPU_MEMORY_PRESSURE" and len(r.engine.incidents) == 1


def test_insufficient_incident_is_cleared_when_the_pressure_disappears():
    r = Rig(memory_bytes=5.2e9)
    inc = r.engine.tick()
    assert inc.status == "INSUFFICIENT_EVIDENCE"

    r.memory = 2.9e9
    assert r.engine.tick() is None
    assert inc.status == "CLEARED"
    assert r.engine.audit.events[-1]["event"] == "incident_cleared" and r.engine.audit.verify()

    r.memory = 5.2e9   # pressure returns: a NEW incident, the cleared one stays closed
    again = r.engine.tick()
    assert again is not inc and again.incident_id == "inc_002"


def test_telemetry_outage_does_not_clear_an_insufficient_incident():
    from aiops.prometheus import TelemetryError

    r = Rig(memory_bytes=5.2e9)
    inc = r.engine.tick()

    def broken():
        raise TelemetryError("nvidia-smi unavailable")

    r.telemetry.gpu = broken
    assert r.engine.tick() is None and r.engine.health == "DEGRADED"
    assert inc.status == "INSUFFICIENT_EVIDENCE"


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


# --- approval -> restart -> verification on the real-shaped observation -------------------

class RecoveringCluster(World):
    """A restart really changes identity and (optionally) makes vLLM answer again."""

    def __init__(self, rig, recovers=True, changes_identity=True):
        super().__init__()
        self.rig, self.recovers, self.changes_identity = rig, recovers, changes_identity

    def get_workload(self, name):
        return {"id": self.uid, "ready": False}   # docker health lags: never relied upon

    def restart_workload(self, name):
        self.restarts += 1
        if self.changes_identity:
            self.uid = f"uid-{self.restarts}"
        if self.recovers:
            self.rig.vllm.ok, self.rig.vllm.error = True, None
        return "ok"


def approved_rig(memory=2.9e9, **cluster_kw):
    r = Rig(memory_bytes=memory)
    r.cluster = RecoveringCluster(r, **cluster_kw)
    r.engine = Engine(r.telemetry, r.cluster, {**CONFIG, "stable_probes": 3,
                                                "probe_interval": 0})
    r.fail_probe()
    return r


def verification_checks(engine):
    return next(e["data"]["checks"] for e in reversed(engine.audit.events)
                if e["event"] == "verification_finished")


def test_hung_workload_restart_with_real_inference_recovery_is_verified_and_resolved():
    r = approved_rig()
    inc = r.engine.tick()
    assert inc.category == "INFERENCE_UNRESPONSIVE" and r.cluster.restarts == 0
    r.engine.approve(inc.incident_id)
    assert inc.status == "RESOLVED" and r.cluster.restarts == 1
    checks = verification_checks(r.engine)
    assert checks["inference_probe_stable"] is True and checks["workload_restarted"] is True
    assert r.engine.audit.verify()


def test_restart_that_does_not_restore_inference_stays_unresolved():
    r = approved_rig(recovers=False)
    inc = r.engine.tick()
    r.engine.approve(inc.incident_id)
    assert inc.status == "UNRESOLVED"
    assert verification_checks(r.engine)["inference_probe_stable"] is False


def test_restart_that_changed_nothing_stays_unresolved_even_if_inference_answers():
    r = approved_rig(changes_identity=False)
    inc = r.engine.tick()
    r.engine.approve(inc.incident_id)
    assert inc.status == "UNRESOLVED"
    assert verification_checks(r.engine)["workload_restarted"] is False


def test_pressure_incident_also_requires_memory_to_have_dropped():
    r = approved_rig(memory=5.2e9)          # memory stays high after the restart
    inc = r.engine.tick()
    assert inc.category == "GPU_MEMORY_PRESSURE"
    r.engine.approve(inc.incident_id)
    assert inc.status == "UNRESOLVED"
    assert verification_checks(r.engine)["gpu_memory_below_threshold"] is False
