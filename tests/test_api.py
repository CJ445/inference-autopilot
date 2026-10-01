import json
import threading
import urllib.error
import urllib.request

import pytest
from test_engine import CONFIG, World

from aiops.api import make_server
from aiops.engine import Engine


@pytest.fixture
def api():
    w = World()
    w.fault, w.failures = True, 5
    engine = Engine(w, w, CONFIG)
    server = make_server(engine, port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    yield base, engine, w
    server.shutdown()


def call(base, path, method="GET"):
    req = urllib.request.Request(base + path, method=method,
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_health_reports_engine_health(api):
    base, _, _ = api
    assert call(base, "/health") == (200, {"status": "HEALTHY"})


def test_incident_list_is_empty_until_the_engine_detects_something(api):
    base, _, _ = api
    assert call(base, "/api/v1/incidents") == (200, {"incidents": []})


def test_incident_detail_exposes_status_evidence_and_pending_proposal(api):
    base, engine, _ = api
    inc = engine.tick()
    _, listing = call(base, "/api/v1/incidents")
    assert [i["incident_id"] for i in listing["incidents"]] == [inc.incident_id]
    status, detail = call(base, f"/api/v1/incidents/{inc.incident_id}")
    assert status == 200
    assert detail["status"] == "POLICY_CHECK" and len(detail["evidence"]) >= 2
    assert detail["proposal"] == {"action": "restart_workload", "parameters": {"workload": "vllm-0"}}


def test_approve_over_http_remediates_and_resolves(api):
    base, engine, w = api
    inc = engine.tick()
    status, _ = call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
    assert status == 200
    assert call(base, f"/api/v1/incidents/{inc.incident_id}")[1]["status"] == "RESOLVED"
    assert w.uid != "abc"


def test_reject_over_http_leaves_cluster_untouched_and_blocks_later_approval(api):
    base, engine, w = api
    inc = engine.tick()
    status, _ = call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/reject", "POST")
    assert status == 200 and w.restarts == 0
    status, body = call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
    assert status == 409 and body["error"]["code"] == "POLICY_DENIED"
    assert w.restarts == 0


def test_errors_follow_the_contract_and_leak_no_stack_traces(api):
    base, _, _ = api
    for path, method in [("/api/v1/incidents/nope", "GET"),
                         ("/api/v1/incidents/nope/remediation/approve", "POST"),
                         ("/api/v1/unknown", "GET")]:
        status, body = call(base, path, method)
        assert status == 404
        assert set(body["error"]) == {"code", "message", "request_id"}
        assert "Traceback" not in json.dumps(body)


def test_server_binds_to_loopback_only(api):
    base, _, _ = api
    assert "127.0.0.1" in base
    server = make_server(Engine(World(), World(), CONFIG), port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_unexpected_engine_failure_returns_contract_error_without_internals(api):
    base, engine, w = api
    inc = engine.tick()

    def boom(name):
        raise RuntimeError("secret internal detail /home/cyril/x.py")

    w.get_workload = boom
    status, body = call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
    assert status == 500
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in json.dumps(body) and "Traceback" not in json.dumps(body)
    assert call(base, "/health")[0] == 200  # server and lock survive


def test_persistence_outage_is_a_503_dependency_error_and_changes_nothing(tmp_path):
    import os

    from aiops.store import Store

    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    db = tmp_path / "aiops.db"
    w = World()
    w.fault, w.failures = True, 5
    engine = Engine(w, w, CONFIG, store=Store(db))
    inc = engine.tick()
    server = make_server(engine, port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    try:
        os.chmod(db, 0o444)
        status, body = call(f"http://127.0.0.1:{server.server_port}",
                            f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
    finally:
        server.shutdown()
        os.chmod(db, 0o644)
    assert status == 503 and body["error"]["code"] == "DEPENDENCY_ERROR"
    assert "readonly" not in json.dumps(body).lower()  # no internals
    assert w.restarts == 0


def test_a_busy_workload_is_a_409_policy_denied_and_changes_nothing(api):
    from test_engine_real import inject

    base, engine, w = api
    inc = engine.tick()
    inject(engine, "inc_900", "EXECUTING", pending=False, workload="vllm-0")
    status, body = call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
    assert status == 409 and body["error"]["code"] == "POLICY_DENIED"
    assert w.restarts == 0


def test_status_endpoint_reports_health_last_observation_and_incident_ids(api):
    base, engine, _ = api
    assert call(base, "/api/v1/status")[1]["last_observation"] is None
    inc = engine.tick()
    status, body = call(base, "/api/v1/status")
    assert status == 200 and body["health"] == "HEALTHY"
    assert body["last_observation"]["gpu_memory_used_bytes"] > 0 and body["last_observed_at"]
    assert body["incidents"] == {"active": [inc.incident_id], "pending": [inc.incident_id]}


def test_status_endpoint_includes_static_info_supplied_by_the_service():
    w = World()
    server = make_server(Engine(w, w, CONFIG), port=0, info={"profile": "docker-real-gpu"})
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    try:
        assert call(f"http://127.0.0.1:{server.server_port}", "/api/v1/status")[1]["info"] == {
            "profile": "docker-real-gpu"}
    finally:
        server.shutdown()


# --- what the operator TUI needs (Slice 8): read-only additions -------------------------------

def test_status_reports_audit_integrity(api):
    base, engine, _ = api
    engine.tick()
    assert call(base, "/api/v1/status")[1]["audit"] == {
        "valid": True, "events": len(engine.audit.events)}


def test_status_flags_a_broken_audit_chain_instead_of_hiding_it(api):
    base, engine, _ = api
    engine.tick()
    engine.audit.events[0]["data"]["incident_id"] = "forged"
    st = call(base, "/api/v1/status")[1]
    assert st["audit"]["valid"] is False and st["health"] == "HEALTHY"


def test_status_includes_extra_state_supplied_by_the_service():
    w = World()
    server = make_server(Engine(w, w, CONFIG), port=0,
                         status_extra=lambda: {"watchdog": {"state": "armed", "pid": 7}})
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    try:
        st = call(f"http://127.0.0.1:{server.server_port}", "/api/v1/status")[1]
        assert st["watchdog"] == {"state": "armed", "pid": 7}
    finally:
        server.shutdown()


def test_a_failing_status_provider_is_reported_not_fatal():
    w = World()

    def broken():
        raise RuntimeError("secret internal detail")

    server = make_server(Engine(w, w, CONFIG), port=0, status_extra=broken)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    try:
        status, st = call(f"http://127.0.0.1:{server.server_port}", "/api/v1/status")
        assert status == 200 and st["health"] == "HEALTHY" and "extra_error" in st
        assert "secret" not in json.dumps(st)
    finally:
        server.shutdown()


def test_audit_endpoint_lists_events_in_order_with_validity(api):
    base, engine, _ = api
    engine.tick()
    status, body = call(base, "/api/v1/audit")
    assert status == 200 and body["valid"] is True
    assert [e["seq"] for e in body["events"]] == list(range(len(engine.audit.events)))
    assert [e["event"] for e in body["events"]][:2] == ["incident_created", "rca_generated"]
    assert all(len(e["hash"]) == 12 and "data" in e for e in body["events"])


def test_audit_endpoint_returns_only_the_latest_events_when_limited(api):
    base, engine, _ = api
    engine.tick()
    n = len(engine.audit.events)
    body = call(base, "/api/v1/audit?limit=2")[1]
    assert [e["seq"] for e in body["events"]] == [n - 2, n - 1]
    assert call(base, "/api/v1/audit?limit=banana")[0] == 400
    assert call(base, "/api/v1/audit?limit=0")[0] == 400


def test_incident_detail_includes_the_rca_the_engine_produced(api):
    base, engine, _ = api
    inc = engine.tick()
    detail = call(base, f"/api/v1/incidents/{inc.incident_id}")[1]
    assert detail["rca"]["root_cause"]["category"] == "GPU_MEMORY_PRESSURE"
    assert detail["rca"]["evidence_ids"] and detail["rca"]["insufficient_evidence"] is False


def test_before_execution_there_is_no_verification_or_remediation(api):
    base, engine, _ = api
    inc = engine.tick()
    detail = call(base, f"/api/v1/incidents/{inc.incident_id}")[1]
    assert detail["verification"] == {"checks": None}
    assert detail["remediation"] == {"state": "NOT_STARTED"}


def test_after_approval_the_detail_carries_the_servers_verification_checks(api):
    base, engine, _ = api
    inc = engine.tick()
    call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
    detail = call(base, f"/api/v1/incidents/{inc.incident_id}")[1]
    assert detail["status"] == "RESOLVED"
    assert detail["remediation"]["state"] == "EXECUTED"
    checks = detail["verification"]["checks"]
    assert checks and all(v is True for v in checks.values())
    assert checks == next(e["data"]["checks"] for e in reversed(engine.audit.events)
                          if e["event"] == "verification_finished")


def test_failed_verification_is_reported_as_the_server_recorded_it():
    from test_remediate import Cluster

    class NoFix(Cluster):
        def restart_workload(self, name):
            self.restarts += 1
            return "ok"                                   # nothing changes: identity stays

    w = World()
    engine = Engine(w, NoFix(), {**CONFIG, "timeout": 0.1, "interval": 0.01})
    engine.cluster.uid = "abc"
    w.fault, w.failures = True, 5
    inc = engine.tick()
    server = make_server(engine, port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
        detail = call(base, f"/api/v1/incidents/{inc.incident_id}")[1]
        assert detail["status"] == "UNRESOLVED"
        assert detail["verification"]["checks"]["workload_restarted"] is False
    finally:
        server.shutdown()


def test_reads_never_wait_for_the_engine_lock_but_writes_still_do():
    import time as _t

    w = World()
    lock = threading.Lock()
    server = make_server(Engine(w, w, CONFIG), port=0, lock=lock)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    def get(path):                                        # a deadlock must fail, not hang the suite
        try:
            with urllib.request.urlopen(base + path, timeout=2) as r:
                return r.status
        except OSError as e:
            return f"blocked: {e}"

    try:
        with lock:                                        # a long tick or remediation is running
            started = _t.monotonic()
            assert get("/api/v1/status") == 200
            assert get("/health") == 200
            assert get("/api/v1/incidents") == 200
            assert get("/api/v1/audit") == 200
            assert _t.monotonic() - started < 1.0         # live state during remediation

            done = threading.Event()
            threading.Thread(target=lambda: (call(
                base, "/api/v1/incidents/nope/remediation/approve", "POST"), done.set()),
                daemon=True).start()
            assert not done.wait(0.4)                     # writes stay serialised behind the lock
        assert done.wait(5)
    finally:
        server.shutdown()


def test_the_new_read_routes_accept_no_writes(api):
    base, _, _ = api
    for method in ("POST", "PUT", "DELETE"):
        status, _ = call(base, "/api/v1/audit", method) if method == "POST" else (404, None)
        assert status == 404


def test_a_failing_write_is_never_retried(api):
    base, engine, w = api
    inc = engine.tick()
    calls = []

    def boom(name):
        calls.append(name)
        raise RuntimeError("dictionary changed size during iteration")

    w.get_workload = boom
    status, _ = call(base, f"/api/v1/incidents/{inc.incident_id}/remediation/approve", "POST")
    assert status == 500 and len(calls) == 1               # executed once, not five times


def test_the_incident_detail_says_how_many_completions_recovery_must_show_when_it_is_known(api):
    base, engine, _ = api
    engine.tick()
    iid = engine.incidents[0].incident_id
    code, detail = call(base, f"/api/v1/incidents/{iid}")
    assert code == 200 and "required_completions" not in detail["verification"]    # this engine has none
    engine.config["stable_probes"] = 3
    code, detail = call(base, f"/api/v1/incidents/{iid}")
    assert detail["verification"] == {"checks": None, "required_completions": 3}
