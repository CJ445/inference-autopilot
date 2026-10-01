import pytest

from aiops.engine import Engine, NothingToApprove
from aiops.prometheus import TelemetryError

CONFIG = {"service": "vllm", "workload": "vllm-0", "gpu_threshold": 7_500_000_000,
          "error_rate_limit": 0.05, "timeout": 1, "interval": 0.01}


class World:
    """Telemetry and cluster in one stateful object: a restart really clears the fault."""

    def __init__(self):
        self.fault, self.failures, self.uid, self.restarts = False, 0, "abc", 0
        self.down = False

    def metrics(self):
        if self.down:
            raise TelemetryError("prometheus unavailable")
        return {"gpu_memory_used_bytes": 7_900_000_000 if self.fault else 4_000_000_000,
                "error_rate": 0.4 if self.fault else 0.0,
                "allocation_failures_total": self.failures}

    def get_workload(self, name):
        return {"id": self.uid, "ready": True}

    def restart_workload(self, name):
        self.restarts += 1
        self.uid, self.fault, self.failures = f"uid-{self.restarts}", False, 0
        return "ok"


def engine(world):
    return Engine(world, world, CONFIG)


def test_healthy_system_creates_no_incident():
    e = engine(World())
    assert e.tick() is None
    assert e.incidents == [] and e.health == "HEALTHY"


def test_fault_creates_diagnosed_incident_with_evidence_and_pending_proposal():
    w = World()
    w.fault, w.failures = True, 5
    e = engine(w)
    inc = e.tick()
    assert inc.status == "POLICY_CHECK"  # awaiting approval (PRD §19/§32)
    assert inc.category == "GPU_MEMORY_PRESSURE"
    assert len(inc.evidence) >= 2 and all(ev["incident_id"] == inc.incident_id
                                          for ev in inc.evidence)
    assert e.pending[inc.incident_id] == {"action": "restart_workload",
                                          "parameters": {"workload": "vllm-0"}}
    assert w.restarts == 0  # nothing mutated without approval


def test_repeated_ticks_for_the_same_fault_do_not_create_duplicate_incidents():
    w = World()
    w.fault, w.failures = True, 5
    e = engine(w)
    first = e.tick()
    assert e.tick() is first
    assert len(e.incidents) == 1


def test_approval_runs_remediation_and_resolves_from_observed_state():
    w = World()
    w.fault, w.failures = True, 5
    e = engine(w)
    inc = e.tick()
    e.approve(inc.incident_id)
    assert inc.status == "RESOLVED"
    assert w.uid != "abc"
    assert inc.incident_id not in e.pending
    assert e.audit.verify()


def test_new_fault_after_resolution_opens_a_new_incident():
    w = World()
    w.fault, w.failures = True, 5
    e = engine(w)
    first = e.tick()
    e.approve(first.incident_id)
    w.fault, w.failures = True, 3
    second = e.tick()
    assert second is not first and len(e.incidents) == 2


def test_insufficient_evidence_proposes_nothing_and_cannot_be_approved():
    w = World()
    w.fault, w.failures = True, 0  # memory high but no allocation failures observed
    e = engine(w)
    inc = e.tick()
    assert inc.status == "INSUFFICIENT_EVIDENCE"
    assert e.pending == {}
    with pytest.raises(NothingToApprove):
        e.approve(inc.incident_id)
    assert w.restarts == 0


def test_telemetry_outage_degrades_without_inventing_incidents():
    w = World()
    w.down = True
    e = engine(w)
    assert e.tick() is None
    assert e.health == "DEGRADED" and e.incidents == []


def test_engine_recovers_from_degraded_when_telemetry_returns():
    w = World()
    w.down = True
    e = engine(w)
    e.tick()
    w.down = False
    e.tick()
    assert e.health == "HEALTHY"


def test_audit_records_the_whole_lifecycle_in_order():
    w = World()
    w.fault, w.failures = True, 5
    e = engine(w)
    inc = e.tick()
    assert [ev["event"] for ev in e.audit.events] == [
        "incident_created", "rca_generated", "remediation_proposed", "policy_evaluated"]
    e.approve(inc.incident_id)
    assert [ev["event"] for ev in e.audit.events][4:] == [
        "approval_granted", "remediation_started", "remediation_finished",
        "verification_finished"]


def test_reject_closes_incident_without_touching_the_cluster():
    w = World()
    w.fault, w.failures = True, 5
    e = engine(w)
    inc = e.tick()
    e.reject(inc.incident_id)
    assert inc.status == "REJECTED"
    assert e.pending == {} and w.restarts == 0
    assert e.audit.events[-1]["event"] == "approval_denied"
    with pytest.raises(NothingToApprove):
        e.approve(inc.incident_id)


def test_rejected_incident_does_not_block_a_new_incident_for_a_new_fault():
    w = World()
    w.fault, w.failures = True, 5
    e = engine(w)
    first = e.tick()
    e.reject(first.incident_id)
    assert e.tick() is not first


def test_engine_remembers_the_last_real_observation_and_when_it_was_made():
    w = World()
    e = engine(w)
    assert e.last_observation is None and e.last_observed_at is None
    e.tick()
    assert e.last_observation == w.metrics() and e.last_observed_at.endswith("+00:00")


def test_an_outage_does_not_overwrite_the_last_good_observation():
    w = World()
    e = engine(w)
    e.tick()
    good, when = e.last_observation, e.last_observed_at
    w.down = True
    e.tick()
    assert e.health == "DEGRADED" and e.last_observation == good and e.last_observed_at == when
