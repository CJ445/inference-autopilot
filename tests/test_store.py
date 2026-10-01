import sqlite3

import pytest
from test_engine import CONFIG, World

from aiops.engine import Engine
from aiops.incident import Incident
from aiops.store import AuditTampered, Store


def faulted_world():
    w = World()
    w.fault, w.failures = True, 5
    return w


def test_incident_survives_dict_roundtrip():
    inc = Incident("inc_001", "vllm", "GPU_MEMORY_PRESSURE")
    inc.transition("TRIAGING")
    inc.evidence = [{"evidence_id": "e1"}]
    copy = Incident.from_dict(inc.to_dict())
    assert (copy.incident_id, copy.status, copy.evidence, copy.timeline) == \
           (inc.incident_id, "TRIAGING", inc.evidence, inc.timeline)


def test_restarted_engine_restores_incident_pending_proposal_and_audit(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    first = Engine(w, w, CONFIG, store=Store(db)).tick()

    restored = Engine(w, w, CONFIG, store=Store(db))
    inc = restored.incidents[0]
    assert inc.incident_id == first.incident_id and inc.status == "POLICY_CHECK"
    assert inc.evidence == first.evidence and inc.timeline == first.timeline
    assert restored.pending == {first.incident_id: {"action": "restart_pod",
                                                    "parameters": {"pod": "vllm-0"}}}
    assert len(restored.audit.events) == 4 and restored.audit.verify()


def test_restarted_engine_still_deduplicates_and_can_approve(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    Engine(w, w, CONFIG, store=Store(db)).tick()

    restored = Engine(w, w, CONFIG, store=Store(db))
    inc = restored.tick()
    assert len(restored.incidents) == 1
    restored.approve(inc.incident_id)
    assert inc.status == "RESOLVED"


def test_resolution_and_audit_chain_are_persisted(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    e = Engine(w, w, CONFIG, store=Store(db))
    e.approve(e.tick().incident_id)

    restored = Engine(w, w, CONFIG, store=Store(db))
    assert restored.incidents[0].status == "RESOLVED"
    assert restored.pending == {}
    assert len(restored.audit.events) == len(e.audit.events) and restored.audit.verify()


def test_approval_is_persisted_before_execution_so_a_crash_cannot_replay_it(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()

    def crash(name):
        raise KeyboardInterrupt("process died mid-remediation")

    e = Engine(w, w, CONFIG, store=Store(db))
    inc = e.tick()
    w.restart_pod = crash
    with pytest.raises(KeyboardInterrupt):
        e.approve(inc.incident_id)

    restored = Engine(w, w, CONFIG, store=Store(db))
    assert restored.pending == {}  # proposal consumed; no second restart can be approved
    with pytest.raises(Exception):
        restored.approve(inc.incident_id)


def test_tampered_audit_row_is_detected_on_load(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    Engine(w, w, CONFIG, store=Store(db)).tick()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit SET data = '{\"incident_id\": \"forged\"}' WHERE seq = 0")
    with pytest.raises(AuditTampered):
        Engine(w, w, CONFIG, store=Store(db))


def crashed_mid_restart(db):
    w = faulted_world()

    def crash(name):
        raise KeyboardInterrupt("process died mid-remediation")

    e = Engine(w, w, CONFIG, store=Store(db))
    inc = e.tick()
    w.restart_pod = crash
    with pytest.raises(KeyboardInterrupt):
        e.approve(inc.incident_id)
    return w, inc


def test_crash_mid_execution_is_stored_truthfully_then_closed_as_unverified(tmp_path):
    db = tmp_path / "aiops.db"
    w, inc = crashed_mid_restart(db)

    with sqlite3.connect(db) as conn:  # what a crash actually left behind
        stored = conn.execute("SELECT data FROM incidents").fetchone()[0]
    assert '"status": "EXECUTING"' in stored

    restored = Engine(w, w, CONFIG, store=Store(db))
    assert restored.incidents[0].status == "EXECUTION_FAILED"  # never RESOLVED
    assert restored.audit.events[-1]["event"] == "interrupted_by_restart"
    assert restored.audit.verify()

    again = Engine(w, w, CONFIG, store=Store(db))  # closure itself was persisted, once
    assert again.incidents[0].status == "EXECUTION_FAILED"
    assert [e["event"] for e in again.audit.events].count("interrupted_by_restart") == 1


def test_interrupted_incident_does_not_block_a_new_incident_for_a_new_fault(tmp_path):
    db = tmp_path / "aiops.db"
    w, _ = crashed_mid_restart(db)
    restored = Engine(w, w, CONFIG, store=Store(db))
    assert restored.tick() is not restored.incidents[0]
    assert len(restored.incidents) == 2


# --- fail closed when persistence is unavailable (PRD §86) -------------------------------

import os  # noqa: E402

from aiops.store import StoreUnavailable  # noqa: E402

needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def break_db(db):
    os.chmod(db, 0o444)  # a real SQLite write failure: "attempt to write a readonly database"


def fix_db(db):
    os.chmod(db, 0o644)


@needs_nonroot
def test_approval_that_cannot_be_persisted_never_touches_the_cluster(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    e = Engine(w, w, CONFIG, store=Store(db))
    inc = e.tick()
    events_before = len(e.audit.events)

    break_db(db)
    with pytest.raises(StoreUnavailable):
        e.approve(inc.incident_id)

    assert w.restarts == 0                                 # no mutation without durable state
    assert inc.status == "POLICY_CHECK"                    # unchanged
    assert inc.incident_id in e.pending                    # proposal kept, so it can be retried
    assert len(e.audit.events) == events_before and e.audit.verify()  # no phantom approval


@needs_nonroot
def test_approval_succeeds_on_retry_once_persistence_returns(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    e = Engine(w, w, CONFIG, store=Store(db))
    inc = e.tick()
    break_db(db)
    with pytest.raises(StoreUnavailable):
        e.approve(inc.incident_id)

    fix_db(db)
    e.approve(inc.incident_id)
    assert inc.status == "RESOLVED" and w.restarts == 1
    assert Engine(w, w, CONFIG, store=Store(db)).incidents[0].status == "RESOLVED"


@needs_nonroot
def test_incident_that_cannot_be_persisted_is_rolled_back_then_retried(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    e = Engine(w, w, CONFIG, store=Store(db))

    break_db(db)
    with pytest.raises(StoreUnavailable):
        e.tick()
    assert e.incidents == [] and e.pending == {} and e.audit.events == []

    fix_db(db)
    inc = e.tick()
    assert inc.incident_id == "inc_001" and inc.status == "POLICY_CHECK"
    assert len(Engine(w, w, CONFIG, store=Store(db)).incidents) == 1


@needs_nonroot
def test_rejection_that_cannot_be_persisted_is_not_half_applied(tmp_path):
    db = tmp_path / "aiops.db"
    w = faulted_world()
    e = Engine(w, w, CONFIG, store=Store(db))
    inc = e.tick()
    timeline_before = list(inc.timeline)

    break_db(db)
    with pytest.raises(StoreUnavailable):
        e.reject(inc.incident_id)

    # a restart must not resurrect a proposal the operator rejected, so memory must agree
    # with disk: still awaiting a decision, still rejectable once the store is back
    assert inc.status == "POLICY_CHECK" and inc.timeline == timeline_before
    assert inc.incident_id in e.pending
    fix_db(db)
    e.reject(inc.incident_id)
    assert inc.status == "REJECTED"
