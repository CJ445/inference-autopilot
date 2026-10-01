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
    assert detail["proposal"] == {"action": "restart_pod", "parameters": {"pod": "vllm-0"}}


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

    w.get_pod = boom
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
