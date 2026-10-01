"""The golden acceptance criteria (PRD §72), their ability to FAIL, and the simulated scenario held
to them. The real Docker-pause scenario is held to the same function in tests/gpu/test_real_golden.py."""
import copy
import json
import threading
import urllib.request

import pytest
from golden_acceptance import CRITERIA, assert_golden, evaluate
from test_engine import CONFIG, World
from test_practice_api import call, until

from aiops.api import make_server
from aiops.engine import Engine
from aiops.practice import PracticeHost


@pytest.fixture
def practice():
    host = PracticeHost(tick_seconds=0.05, verify_timeout=0.5)
    engine = Engine(World(), World(), CONFIG)
    server = make_server(engine, port=0, practice=host)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield server.server_address[1], host
    host.shutdown()
    server.shutdown()
    server.server_close()


def run_golden(port, host):
    """The simulated golden scenario over the practice API; returns what the criteria judge."""
    call(port, "/api/v1/practice/start", "POST")
    call(port, "/api/v1/practice/fault", "POST")
    until(port, "/api/v1/practice/status", lambda s: s["practice"]["stage"] == "awaiting_approval")
    before = host.session.world.identity                    # observed from the simulated world
    call(port, "/api/v1/practice/incidents/inc_001/remediation/approve", "POST")
    after = host.session.world.identity
    detail = call(port, "/api/v1/practice/incidents/inc_001")[1]
    events = call(port, "/api/v1/practice/audit")[1]["events"]
    return detail, events, before, after


def test_the_simulated_scenario_meets_every_golden_criterion(practice):
    port, host = practice
    detail, events, before, after = run_golden(port, host)
    assert before == "sim:sim-vllm:generation-1" and after == "sim:sim-vllm:generation-2"
    result = assert_golden(detail, events, before, after, mode="SIMULATION")
    assert set(result) == set(CRITERIA) and all(result.values())


# --- the criteria must be able to fail (a judge that cannot fail proves nothing) ----------------------

@pytest.fixture
def good(practice):
    port, host = practice
    detail, events, before, after = run_golden(port, host)
    return detail, events, before, after


def verdict(good, **changes):
    detail, events, before, after = (copy.deepcopy(x) for x in good)
    for key, fn in changes.items():
        fn(detail=detail, events=events)
    return evaluate(detail, events, before, after, "SIMULATION")


def failing(result):
    return sorted(k for k, v in result.items() if not v)


def test_a_resolved_claim_with_an_unchanged_identity_fails(good):
    detail, events, before, after = good
    assert "infrastructure_state_changed" in failing(evaluate(detail, events, before, before, "SIMULATION"))
    assert "infrastructure_state_changed" in failing(evaluate(detail, events, None, after, "SIMULATION"))


def test_an_unresolved_incident_or_a_failed_check_fails(good):
    assert failing(verdict(good, s=lambda detail, events: detail.update(status="UNRESOLVED"))) == [
        "incident_state_resolved"]
    def break_check(detail, events):
        detail["verification"]["checks"]["inference_probe_stable"] = False
    assert "verification_passed" in failing(verdict(good, s=break_check))


def test_checks_that_differ_from_the_audit_record_fail(good):
    def forge(detail, events):
        detail["verification"]["checks"] = {"workload_restarted": True, "inference_probe_stable": True,
                                            "extra": True}
    assert "verification_passed" in failing(verdict(good, s=forge))      # not what the chain recorded


def test_missing_or_misordered_audit_events_fail(good):
    def drop(detail, events):
        events[:] = [e for e in events if e["event"] != "policy_evaluated"]
    r = failing(verdict(good, s=drop))
    assert "audit_in_order" in r and "policy_decision_recorded" in r
    def swap(detail, events):
        names = [e["event"] for e in events]
        a, b = names.index("approval_granted"), names.index("remediation_started")
        events[a], events[b] = events[b], events[a]
    assert "audit_in_order" in failing(verdict(good, s=swap))


def test_no_root_cause_or_too_little_evidence_fails(good):
    assert "root_cause_present" in failing(verdict(
        good, s=lambda detail, events: detail["rca"].update(root_cause=None)))
    assert "root_cause_has_evidence" in failing(verdict(
        good, s=lambda detail, events: detail["rca"].update(evidence_ids=["inc_001_ev_99"])))
    assert "evidence_count_at_least_2" in failing(verdict(
        good, s=lambda detail, events: detail.update(evidence=detail["evidence"][:1])))


def test_a_simulation_that_looks_real_or_the_reverse_fails(good):
    detail, events, before, after = good
    assert "mode_marking" in failing(evaluate(detail, events, before, after, "REAL"))
    r = failing(verdict(good, s=lambda detail, events: detail["evidence"][0].update(source_type="real")))
    assert "mode_marking" in r


def test_a_remediation_that_never_executed_fails(good):
    def not_executed(detail, events):
        events[:] = [e for e in events if e["event"] != "remediation_finished"]
        detail["remediation"] = {"state": "FAILED"}
    r = failing(verdict(good, s=not_executed))
    assert "remediation_executed" in r and "audit_in_order" in r
