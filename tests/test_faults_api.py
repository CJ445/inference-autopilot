"""The fault API: explicit confirmation, validated bodies, never automatic, absent without an injector,
unreachable through the practice prefix, audited once, and ended by a graceful stop."""
import json
import threading
import urllib.error
import urllib.request

import pytest
from faults_support import FakeClock, FakeDocker, cid, live_reaper
from test_engine import CONFIG, World

from aiops.api import make_server
from aiops.engine import Engine
from aiops.faults.core import AllowlistedDocker, read_lease
from aiops.faults.injector import FaultInjector
from aiops.practice import PracticeHost
from aiops.serve import Service
from aiops.store import StoreUnavailable

CONFIRM = {"X-Aiops-Confirm": "inject-fault"}


def request(port, path, method="GET", headers=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else
                                        (b"" if method == "POST" else None))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=data,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.fixture
def plane(tmp_path):
    docker, clock = FakeDocker(), FakeClock()
    inj = FaultInjector("vllm", tmp_path / "fault.json", run=AllowlistedDocker("vllm", run=docker),
                        spawn=live_reaper, clock=clock, sleep=clock.sleep)
    engine = Engine(World(), World(), CONFIG)
    lock = threading.Lock()
    host = PracticeHost(tick_seconds=0.05)
    server = make_server(engine, port=0, lock=lock, faults=inj, practice=host)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield server.server_address[1], inj, docker, lock
    host.shutdown()                                  # no ticker thread may outlive the test
    server.shutdown()
    server.server_close()


def good(**over):
    return {"type": "pause_workload", "target": "vllm", "duration_seconds": 60, **over}


# --- confirmation and validation ---------------------------------------------------------------------

def test_a_fault_needs_the_explicit_confirmation_header(plane):
    port, inj, docker, _ = plane
    for headers in ({}, {"X-Aiops-Confirm": "yes"}, {"X-Aiops-Confirm": "stop-control-plane"}):
        code, body = request(port, "/api/v1/faults", "POST", headers, good())
        assert code == 400 and body["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert docker.calls == [] and not docker.main.paused and inj.lease_or_none() is None


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"pause"', b"x" * 2000])
def test_a_malformed_or_oversized_body_is_refused(plane, raw):
    port, _, docker, _ = plane
    code, body = request(port, "/api/v1/faults", "POST", CONFIRM, raw=raw)
    assert code == 400 and body["error"]["code"] == "INVALID_REQUEST" and docker.calls == []


@pytest.mark.parametrize("over, fragment", [
    ({"type": "kill_workload"}, "unknown fault type"), ({"type": None}, "unknown fault type"),
    ({"target": "other"}, "not the configured workload"),
    ({"duration_seconds": 5}, "from 15 to 300"), ({"duration_seconds": 9999}, "from 15 to 300"),
    ({"duration_seconds": "60"}, "from 15 to 300"),
])
def test_unknown_types_other_targets_and_unsafe_durations_are_refused_over_http(plane, over, fragment):
    port, _, docker, _ = plane
    code, body = request(port, "/api/v1/faults", "POST", CONFIRM, good(**over))
    assert code == 400 and fragment in body["error"]["message"] and docker.calls == []


def test_a_missing_duration_uses_the_bounded_default(plane):
    port, inj, docker, _ = plane
    code, body = request(port, "/api/v1/faults", "POST", CONFIRM, {"type": "pause_workload", "target": "vllm"})
    assert code == 202 and read_lease(inj.lease_path)["duration_seconds"] == 120


# --- the fault, its status and its end ----------------------------------------------------------------------

def test_injecting_reporting_cancelling(plane):
    port, inj, docker, _ = plane
    code, body = request(port, "/api/v1/faults", "POST", CONFIRM, good())
    assert code == 202 and body["injected"] is True and body["active"]["status"] == "ACTIVE"
    assert docker.main.paused
    code, status = request(port, "/api/v1/faults")
    assert code == 200 and status["available"] is True and status["active"]["workload"] == "vllm"
    assert status["last"] is None
    assert request(port, "/api/v1/faults", "POST", CONFIRM, good())[1]["error"]["code"] == "FAULT_ACTIVE"

    code, ended = request(port, "/api/v1/faults/cancel", "POST")           # resuming needs no confirmation
    assert code == 200 and ended["active"] is None and ended["last"]["status"] == "RECOVERED"
    assert ended["last"]["recovery"]["by"] == "operator" and not docker.main.paused
    code, body = request(port, "/api/v1/faults/cancel", "POST")
    assert code == 409 and body["error"]["code"] == "PRECONDITION_FAILED"


def test_a_failed_precondition_is_a_conflict_and_nothing_is_paused(plane):
    port, _, docker, _ = plane
    docker.main.paused = True
    code, body = request(port, "/api/v1/faults", "POST", CONFIRM, good())
    assert code == 409 and body["error"]["code"] == "PRECONDITION_FAILED"
    assert "pause" not in docker.verbs()


def test_the_fault_endpoints_never_wait_for_the_engine_lock(plane):
    port, _, docker, lock = plane
    with lock:                                   # a remediation in flight
        assert request(port, "/api/v1/faults", "POST", CONFIRM, good())[0] == 202
        assert request(port, "/api/v1/faults/cancel", "POST")[0] == 200


def test_unknown_fault_paths_and_methods(plane):
    port, *_ = plane
    assert request(port, "/api/v1/faults/cancel", "GET")[0] == 404
    assert request(port, "/api/v1/faults/other", "POST", CONFIRM, good())[0] == 404


# --- where faults do not exist ------------------------------------------------------------------------------------

def test_faults_are_absent_without_an_injector():
    server = make_server(Engine(World(), World(), CONFIG), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        for path, method in [("/api/v1/faults", "GET"), ("/api/v1/faults", "POST"),
                             ("/api/v1/faults/cancel", "POST")]:
            code, body = request(port, path, method, CONFIRM, good() if method == "POST" else None)
            assert code == 404 and body["error"]["code"] == "NOT_AVAILABLE"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path", ["/api/v1/practice/faults", "/api/v1/practice/faults/cancel"])
def test_a_confirmed_fault_sent_through_the_practice_prefix_does_nothing(plane, path):
    port, inj, docker, _ = plane
    request(port, "/api/v1/practice/start", "POST")
    code, _ = request(port, path, "POST", CONFIRM, good())
    assert code == 404 and docker.calls == [] and inj.lease_or_none() is None


# --- audit, shutdown and the engine hook ------------------------------------------------------------------------------

@pytest.fixture
def service(tmp_path):
    docker, clock = FakeDocker(), FakeClock()
    inj = FaultInjector("vllm", tmp_path / "fault.json", run=AllowlistedDocker("vllm", run=docker),
                        spawn=live_reaper, clock=clock, sleep=clock.sleep)
    engine = Engine(World(), World(), CONFIG)
    svc = Service(engine, port=0, interval=60, faults=inj)
    inj.audit = svc.record
    yield svc, inj, docker, clock, engine
    try:
        svc.server.server_close()
    except OSError:
        pass


def test_a_fault_and_its_end_are_audited_once_each_in_the_real_chain(service):
    svc, inj, docker, clock, engine = service
    inj.inject("pause_workload", "vllm", 30)
    for _ in range(3):
        inj.tick()                                                       # repeated passes: no duplicates
    inj.cancel()
    for _ in range(3):
        inj.tick()
    names = [e["event"] for e in engine.audit.events]
    assert names == ["fault_injected", "fault_ended"] and engine.audit.verify()
    started, ended = (e["data"] for e in engine.audit.events)
    assert started["workload"] == "vllm" and started["duration_seconds"] == 30
    assert started["identity"].startswith(cid(1)) and ended["status"] == "RECOVERED"
    assert ended["recovery"]["by"] == "operator" and "mode" not in started       # the REAL chain


def test_an_audit_that_fails_is_retried_not_lost(service):
    svc, inj, docker, clock, engine = service
    inj.inject("pause_workload", "vllm", 30)
    calls = {"n": 0}

    def flaky(event, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StoreUnavailable("disk full")
        svc.engine.record(event, data)
    inj.audit = flaky
    with pytest.raises(StoreUnavailable):
        inj.tick()
    inj.tick()
    assert [e["event"] for e in engine.audit.events] == ["fault_injected"]


def test_a_recovery_done_while_the_control_plane_was_down_is_audited_at_the_next_start(service):
    svc, inj, docker, clock, engine = service
    inj.inject("pause_workload", "vllm", 30)
    inj.tick()                                                           # injection audited
    clock.advance(40)
    from aiops.faults.core import recover
    recover(inj.lease_path, inj._run, by="reaper", clock=clock)          # done by the reaper alone
    assert [e["event"] for e in engine.audit.events] == ["fault_injected"]
    inj.sweep()                                                          # the next control-plane start
    ended = engine.audit.events[-1]
    assert ended["event"] == "fault_ended" and ended["data"]["recovery"]["by"] == "reaper"


def test_stopping_the_service_resumes_a_paused_workload(service):
    svc, inj, docker, clock, engine = service
    inj.inject("pause_workload", "vllm", 60)
    svc.start()
    svc.stop()
    assert not docker.main.paused
    assert read_lease(inj.lease_path)["recovery"]["by"] == "control-plane-stop"


def test_engine_record_is_durable_or_leaves_nothing(tmp_path):
    from aiops.store import Store
    store = Store(tmp_path / "x.db")
    engine = Engine(World(), World(), CONFIG, store=store)
    engine.record("fault_injected", {"a": 1})
    assert [e["event"] for e in Store(tmp_path / "x.db").load()[2].events] == ["fault_injected"]

    def broken(*a, **k):
        raise StoreUnavailable("full")
    store.save = broken
    with pytest.raises(StoreUnavailable):
        engine.record("fault_ended", {})
    assert [e["event"] for e in engine.audit.events] == ["fault_injected"] and engine.audit.verify()
