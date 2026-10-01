"""The control plane's read-only system endpoints and its one confirmed stop."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from test_engine import CONFIG, World
from test_lifecycle import Running, fake_build, http, make_config, state_path, wait_for

from aiops import __version__
from aiops.api import make_server
from aiops.doctor import FAIL, NOT_APPLICABLE, PASS, WARN
from aiops.engine import Engine
from aiops.profile import load_profile
from aiops.system import System, git_revision, redact

STOP = {"X-Aiops-Confirm": "stop-control-plane"}


def request(port, path, method="GET", headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=b"" if method == "POST" else None, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def mixed_doctor(profile):
    return [{"check": "python", "status": PASS, "detail": "ok", "blocking": True},
            {"check": "gpu_headroom", "status": WARN, "detail": "tight", "blocking": False},
            {"check": "vllm_probe", "status": FAIL, "detail": "no answer", "blocking": False},
            {"check": "kubectl", "status": NOT_APPLICABLE, "detail": "n/a", "blocking": False}]


@pytest.fixture
def running(tmp_path):
    config = make_config(tmp_path)
    world = World()
    r = Running(config, doctor=mixed_doctor, build=fake_build(world))
    r.world = world
    port = load_profile(config)["control_plane"]["port"]
    wait_for(lambda: http(port, "/health"))
    yield r, port, tmp_path, config
    if r.thread.is_alive():
        r.finish()


# --- version ---------------------------------------------------------------------------------

def test_version_reports_the_real_package_version_and_runtime(running):
    _, port, *_ = running
    code, body = request(port, "/api/v1/version")
    assert code == 200 and body["version"] == __version__
    assert set(body) == {"version", "git_revision", "python", "platform"}
    assert body["git_revision"] is None or len(body["git_revision"]) >= 7


def test_git_revision_is_optional_never_required(tmp_path):
    assert git_revision(tmp_path) is None                           # no .git: a release build
    (tmp_path / ".git").mkdir()

    def broken(*a, **k):
        raise OSError("git missing")

    assert git_revision(tmp_path, run=broken) is None
    bad = type("R", (), {"returncode": 0, "stdout": "not a hash\n"})()
    assert git_revision(tmp_path, run=lambda *a, **k: bad) is None
    good = type("R", (), {"returncode": 0, "stdout": "abc1234def56\n"})()
    assert git_revision(tmp_path, run=lambda *a, **k: good) == "abc1234def56"


# --- config ----------------------------------------------------------------------------------

def test_config_shows_the_active_validated_profile_and_its_source(running):
    _, port, tmp_path, config = running
    code, body = request(port, "/api/v1/config")
    profile = load_profile(config)
    assert code == 200 and body["source"] == profile["config_path"]
    assert body["profile"] == "docker-real-gpu" and body["provider"] == "docker"
    assert body["sections"]["workload"]["name"] == profile["workload"]["name"]
    assert body["sections"]["control_plane"]["port"] == profile["control_plane"]["port"]
    assert "config_path" not in body["sections"]


def test_secret_looking_keys_are_never_returned():
    out = redact({"workload": {"name": "vllm", "api_token": "x", "Password": "y",
                               "nested": {"client_secret": "z", "port": 1}}})
    assert out["workload"]["name"] == "vllm" and out["workload"]["nested"]["port"] == 1
    assert out["workload"]["api_token"] == out["workload"]["Password"] == "<redacted>"
    assert out["workload"]["nested"]["client_secret"] == "<redacted>"


# --- diagnostics -----------------------------------------------------------------------------

def test_diagnostics_are_the_doctors_results_with_their_meanings_intact(running):
    _, port, *_ = running
    code, body = request(port, "/api/v1/diagnostics")
    assert code == 200
    assert [(r["check"], r["status"]) for r in body["results"]] == [
        ("python", PASS), ("gpu_headroom", WARN), ("vllm_probe", FAIL), ("kubectl", NOT_APPLICABLE)]
    assert body["results"][2]["detail"] == "no answer" and "ran_at" in body
    assert body["duration_seconds"] >= 0


def test_a_diagnostics_run_in_progress_is_not_started_twice():
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def slow(profile):
        calls.append(1)
        entered.set()
        gate.wait(5)
        return []

    system = System({"config_path": "x", "name": "n", "provider": "docker"}, lambda: None, doctor=slow)
    first = threading.Thread(target=system.diagnostics)
    first.start()
    assert entered.wait(5)
    assert system.diagnostics() is None and calls == [1]
    gate.set()
    first.join()
    assert system.diagnostics()["results"] == []                # runs again once it is free


def test_diagnostics_never_start_or_change_anything(running):
    r, port, tmp_path, config = running
    before = state_path(tmp_path).read_text()
    request(port, "/api/v1/diagnostics")
    assert state_path(tmp_path).read_text() == before and r.thread.is_alive()


# --- stop ------------------------------------------------------------------------------------

def test_stop_needs_an_explicit_confirmation_header(running):
    r, port, tmp_path, _ = running
    code, body = request(port, "/api/v1/control/stop", "POST")
    assert code == 400 and body["error"]["code"] == "CONFIRMATION_REQUIRED"
    code, _ = request(port, "/api/v1/control/stop", "POST", {"X-Aiops-Confirm": "yes"})
    assert code == 400
    code, _ = request(port, "/api/v1/control/stop", "GET")
    assert code == 404
    time.sleep(0.5)
    assert r.thread.is_alive() and http(port, "/health")["status"] == "HEALTHY"


def test_a_confirmed_stop_runs_the_same_graceful_shutdown_as_aiops_stop(running):
    r, port, tmp_path, _ = running
    code, body = request(port, "/api/v1/control/stop", "POST", STOP)
    assert code == 202 and body["stopping"] is True
    r.thread.join(timeout=15)
    assert not r.thread.is_alive() and r.rc == [0]
    assert not state_path(tmp_path).exists()                      # state released, as `stop` does
    assert r.lines[-1] == "stopped"
    with pytest.raises(urllib.error.URLError):
        http(port, "/health")


def test_stop_never_waits_for_the_engine_lock_a_remediation_may_hold(tmp_path):
    engine = Engine(World(), World(), CONFIG)
    lock, stopped = threading.Lock(), threading.Event()
    system = System({"config_path": "x", "name": "n", "provider": "docker"}, stopped.set)
    server = make_server(engine, port=0, lock=lock, system=system)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with lock:                                                # a remediation in flight
            code, _ = request(port, "/api/v1/control/stop", "POST", STOP)
        assert code == 202 and stopped.wait(5)
    finally:
        server.shutdown()
        server.server_close()


def test_the_system_endpoints_do_not_exist_on_the_unmanaged_legacy_server():
    server = make_server(Engine(World(), World(), CONFIG), port=0)       # no system hooks
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        for path, method in [("/api/v1/version", "GET"), ("/api/v1/config", "GET"),
                             ("/api/v1/diagnostics", "GET"), ("/api/v1/control/stop", "POST")]:
            code, body = request(port, path, method, STOP)
            assert code == 404 and body["error"]["code"] == "NOT_AVAILABLE", path
    finally:
        server.shutdown()
        server.server_close()


def test_stop_does_not_touch_the_workload_or_remediation(running):
    r, port, *_ = running
    request(port, "/api/v1/control/stop", "POST", STOP)
    r.thread.join(timeout=15)
    assert r.world.restarts == 0       # the control endpoint has no path to the executor
