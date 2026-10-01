"""Practice mode over HTTP: a SIMULATION session hosted next to the real engine, never mixed with it."""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest
from test_engine import CONFIG, World
from test_lifecycle import Running, fake_build, http, make_config, wait_for

from aiops.api import make_server
from aiops.engine import Engine
from aiops.practice import PracticeHost
from aiops.profile import load_profile
from aiops.system import System


def call(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.fixture
def plane():
    """A REAL stand-in engine with its own pending incident (inc_001), plus a practice host."""
    world = World()
    world.fault, world.failures = True, 5
    engine = Engine(world, world, CONFIG)
    engine.tick()                                          # the real incident: inc_001, POLICY_CHECK
    host = PracticeHost(tick_seconds=0.05, verify_timeout=0.3)
    stopped = threading.Event()
    # real system hooks, so that a practice path leaking into them would SUCCEED (and be seen)
    system = System({"config_path": "x", "name": "n", "provider": "docker"}, stopped.set,
                    doctor=lambda p: [])
    server = make_server(engine, port=0, practice=host, system=system)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    host.stopped = stopped
    yield server.server_address[1], engine, world, host
    host.shutdown()
    server.shutdown()
    server.server_close()


def until(port, path, check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, body = call(port, path)
        if code == 200 and check(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting on {path}: last {code} {body}")


# --- the practice lifecycle ---------------------------------------------------------------------

def test_a_practice_session_is_started_and_reports_simulation(plane):
    port, *_ = plane
    assert call(port, "/api/v1/practice/status")[0] == 409          # none yet
    code, body = call(port, "/api/v1/practice/start", "POST")
    assert code == 200 and body == {"active": True, "mode": "SIMULATION", "stage": "healthy"}
    code, status = call(port, "/api/v1/practice/status")
    assert code == 200 and status["mode"] == "SIMULATION"
    assert status["practice"] == {"stage": "healthy"}
    assert status["info"]["provider"] == "simulation" and status["info"]["profile"] == "practice"
    assert status["workload"] == {"name": "sim-vllm", "state": "running"}
    assert status["last_observation"]["inference_probe_ok"] is True
    assert status["incidents"] == {"active": [], "pending": []}
    assert "watchdog" not in status                                 # the real safety state is not shown


def test_the_full_practice_loop_over_http(plane):
    port, *_ = plane
    call(port, "/api/v1/practice/start", "POST")
    assert call(port, "/api/v1/practice/fault", "POST")[0] == 200
    status = until(port, "/api/v1/practice/status",
                   lambda s: s["practice"]["stage"] == "awaiting_approval")
    assert status["incidents"]["pending"] == ["inc_001"]
    code, listed = call(port, "/api/v1/practice/incidents")
    assert [i["mode"] for i in listed["incidents"]] == ["SIMULATION"]
    code, detail = call(port, "/api/v1/practice/incidents/inc_001")
    assert detail["category"] == "INFERENCE_UNRESPONSIVE" and detail["mode"] == "SIMULATION"
    assert detail["rca"]["root_cause"]["category"] == "INFERENCE_UNRESPONSIVE"
    assert all(e["source_type"] == "simulation" for e in detail["evidence"])
    assert detail["proposal"] == {"action": "restart_workload", "parameters": {"workload": "sim-vllm"}}

    code, done = call(port, "/api/v1/practice/incidents/inc_001/remediation/approve", "POST")
    assert code == 200 and done["status"] == "RESOLVED"
    assert done["verification"]["checks"] == {"workload_restarted": True, "gpu_observable": True,
                                              "vllm_metrics_readable": True,
                                              "inference_probe_stable": True}
    status = until(port, "/api/v1/practice/status", lambda s: s["practice"]["stage"] == "resolved")
    code, audit = call(port, "/api/v1/practice/audit")
    assert audit["valid"] is True and all(e["data"]["mode"] == "SIMULATION" for e in audit["events"])


def test_a_rejected_practice_proposal_restarts_nothing(plane):
    port, _, _, host = plane
    call(port, "/api/v1/practice/start", "POST")
    call(port, "/api/v1/practice/fault", "POST")
    until(port, "/api/v1/practice/status", lambda s: s["practice"]["stage"] == "awaiting_approval")
    code, body = call(port, "/api/v1/practice/incidents/inc_001/remediation/reject", "POST")
    assert code == 200 and body["status"] == "REJECTED"
    assert host.session.world.restarts == 0 and host.session.world.generation == 1


def test_the_fault_can_only_be_injected_into_a_healthy_model_and_a_session_must_exist(plane):
    port, *_ = plane
    assert call(port, "/api/v1/practice/fault", "POST")[0] == 409     # no session
    call(port, "/api/v1/practice/start", "POST")
    assert call(port, "/api/v1/practice/fault", "POST")[0] == 200
    code, body = call(port, "/api/v1/practice/fault", "POST")           # not healthy any more
    assert code == 409 and body["error"]["code"] == "PRACTICE_STATE"


def test_starting_again_discards_the_old_session_and_stopping_ends_it(plane):
    port, _, _, host = plane
    call(port, "/api/v1/practice/start", "POST")
    call(port, "/api/v1/practice/fault", "POST")
    until(port, "/api/v1/practice/status", lambda s: s["incidents"]["pending"] == ["inc_001"])
    call(port, "/api/v1/practice/start", "POST")                        # a fresh one
    assert call(port, "/api/v1/practice/status")[1]["incidents"] == {"active": [], "pending": []}
    scratch = host._scratch.name
    code, body = call(port, "/api/v1/practice/stop", "POST")
    assert code == 200 and body["active"] is False
    assert call(port, "/api/v1/practice/status")[0] == 409 and not os.path.exists(scratch)


# --- the two worlds never mix --------------------------------------------------------------------

def test_a_practice_decision_never_touches_the_real_engine_even_with_the_same_incident_id(plane):
    port, real, world, host = plane
    assert list(real.pending) == ["inc_001"]                            # the REAL inc_001
    call(port, "/api/v1/practice/start", "POST")
    call(port, "/api/v1/practice/fault", "POST")
    until(port, "/api/v1/practice/status", lambda s: s["incidents"]["pending"] == ["inc_001"])
    call(port, "/api/v1/practice/incidents/inc_001/remediation/approve", "POST")   # PRACTICE inc_001
    assert world.restarts == 0 and real.incidents[0].status == "POLICY_CHECK"
    assert list(real.pending) == ["inc_001"] and real.mode == "REAL"
    assert host.session.world.restarts == 1                              # only the simulation moved


def test_a_real_decision_never_touches_the_practice_session(plane):
    port, real, world, host = plane
    call(port, "/api/v1/practice/start", "POST")
    call(port, "/api/v1/practice/fault", "POST")
    until(port, "/api/v1/practice/status", lambda s: s["incidents"]["pending"] == ["inc_001"])
    code, body = call(port, "/api/v1/incidents/inc_001/remediation/approve", "POST")    # REAL inc_001
    assert code == 200 and world.restarts == 1
    assert host.session.world.restarts == 0 and host.session.world.generation == 1
    assert host.session.incident.status == "POLICY_CHECK"


def test_the_real_api_never_shows_practice_data(plane):
    port, real, *_ = plane
    call(port, "/api/v1/practice/start", "POST")
    call(port, "/api/v1/practice/fault", "POST")
    until(port, "/api/v1/practice/status", lambda s: s["incidents"]["pending"] == ["inc_001"])
    status = call(port, "/api/v1/status")[1]
    assert status["mode"] == "REAL" and "practice" not in status
    incidents = call(port, "/api/v1/incidents")[1]["incidents"]
    assert len(incidents) == 1 and "mode" not in incidents[0]
    assert incidents[0]["evidence"][0]["source_type"] == "real"
    assert all("mode" not in e["data"] for e in call(port, "/api/v1/audit")[1]["events"])


@pytest.mark.parametrize("path, method", [
    ("/api/v1/practice/version", "GET"), ("/api/v1/practice/config", "GET"),
    ("/api/v1/practice/diagnostics", "GET"), ("/api/v1/practice/control/stop", "POST"),
    ("/api/v1/practice/health", "GET"), ("/api/v1/practice/../control/stop", "POST"),
    ("/api/v1/practice/practice/start", "POST"), ("/api/v1/practice/status", "POST"),
    ("/api/v1/practice/start", "GET"), ("/api/v1/practice/incidents/inc_001/other", "POST")])
def test_a_practice_path_can_reach_nothing_but_the_practice_engine_routes(plane, path, method):
    port, _, _, host = plane
    call(port, "/api/v1/practice/start", "POST")
    code, _ = call(port, path, method)
    assert code in (400, 404), (path, code)
    assert not host.stopped.is_set()                    # and it never stopped the control plane
    assert call(port, "/api/v1/version")[0] == 200      # (the real system routes do answer)


def test_practice_is_absent_on_a_server_without_a_host():
    server = make_server(Engine(World(), World(), CONFIG), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        for path, method in [("/api/v1/practice/start", "POST"), ("/api/v1/practice/status", "GET"),
                             ("/api/v1/practice/fault", "POST")]:
            code, body = call(port, path, method)
            assert code == 404 and body["error"]["code"] == "NOT_AVAILABLE"
    finally:
        server.shutdown()
        server.server_close()


def test_practice_never_reaches_a_real_door(plane, monkeypatch):
    port, *_ = plane

    def shut(*a, **k):
        raise AssertionError("practice reached outside itself")

    for target, name in [(subprocess, "Popen"), (subprocess, "run"), (os, "system"),
                         (shutil, "which")]:
        monkeypatch.setattr(target, name, shut)
    call(port, "/api/v1/practice/start", "POST")
    call(port, "/api/v1/practice/fault", "POST")
    until(port, "/api/v1/practice/status", lambda s: s["incidents"]["pending"] == ["inc_001"])
    assert call(port, "/api/v1/practice/incidents/inc_001/remediation/approve", "POST")[1][
        "status"] == "RESOLVED"


# --- inside the real control plane ----------------------------------------------------------------

def test_a_running_control_plane_hosts_practice_and_stops_it_cleanly(tmp_path):
    config = make_config(tmp_path)
    port = load_profile(config)["control_plane"]["port"]
    world = World()
    r = Running(config, build=fake_build(world))
    wait_for(lambda: http(port, "/health"))
    try:
        assert call(port, "/api/v1/practice/status")[0] == 409
        assert call(port, "/api/v1/practice/start", "POST")[0] == 200
        assert call(port, "/api/v1/status")[1]["mode"] == "REAL"
        assert call(port, "/api/v1/practice/status")[1]["mode"] == "SIMULATION"
        names = {t.name for t in threading.enumerate()}
        assert "aiops-practice" in names
    finally:
        assert r.finish() == 0
    assert "aiops-practice" not in {t.name for t in threading.enumerate()}   # ticker ended
    assert world.restarts == 0


def test_a_confirmed_stop_sent_through_the_practice_prefix_does_nothing(plane):
    port, _, _, host = plane
    call(port, "/api/v1/practice/start", "POST")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/practice/control/stop",
                                 method="POST", data=b"",
                                 headers={"X-Aiops-Confirm": "stop-control-plane"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 404 and not host.stopped.is_set()
