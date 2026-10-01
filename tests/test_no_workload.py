"""The control plane runs with zero workloads. The workload is operator-controlled: absent,
stopped or running is runtime state, never a prerequisite, and nothing here ever creates,
starts, pulls or restarts a container."""
import urllib.error
import urllib.request  # noqa: F401

import pytest
from test_doctor import SHORT, System, which_all  # noqa: F401
from test_lifecycle import Running, http, make_config, wait_for
from test_vllm import Server

from aiops.docker import DockerProvider, workload_presence
from aiops.doctor import WARN, blocking_failures, run_doctor
from aiops.kubectl import ClusterError
from aiops.profile import load_profile
from aiops.runtime import build_engine

MUTATING = {"run", "create", "start", "restart", "stop", "rm", "pull", "pause", "unpause", "kill",
            "exec", "update", "rename"}


def docker_verbs(system):
    return {c[1] for c in system.calls if c[0] == "docker"}


@pytest.fixture
def vllm_server():
    s = Server()
    yield s
    s.httpd.shutdown()


# --- presence ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kw, expected", [
    ({"ids": ""}, "absent"),
    ({"running": False, "health": None}, "stopped"),
    ({}, "running"),
    ({"health": "starting"}, "starting"),         # Docker's own start period (model loading)
    ({"health": "unhealthy"}, "running"),     # running but sick is the control plane's business
])
def test_presence_is_absent_stopped_or_running(kw, expected):
    system = System(**kw)
    assert workload_presence(DockerProvider("vllm", run=system), "vllm") == expected
    assert not docker_verbs(system) & MUTATING                       # only ps / inspect


@pytest.mark.parametrize("kw", [
    {"ids": SHORT + "\nba9876543210\n"},                                  # ambiguous
    {"full_id": "f" * 64},                                                # inspect != ps
    {"labels": {"com.inference-autopilot.managed": "true",
                "com.inference-autopilot.workload": "other"}},            # wrong labels
])
def test_ambiguity_and_identity_problems_are_never_reported_as_absent(kw):
    with pytest.raises(ClusterError) as e:
        workload_presence(DockerProvider("vllm", run=System(**kw)), "vllm")
    assert "no managed workload" not in str(e.value)


# --- doctor / start gating --------------------------------------------------------------------

def test_no_workload_does_not_block_start_but_is_still_reported(tmp_path):
    profile = load_profile(make_config(tmp_path))
    results = run_doctor(profile, run=System(ids=""), which=which_all)
    by = {r["check"]: r for r in results}
    assert blocking_failures(results) == []
    assert by["workload"]["status"] == WARN                          # unavailable, not "healthy"


# --- the engine --------------------------------------------------------------------------------

def make_engine(tmp_path, vllm_server, system):
    cfg = make_config(tmp_path, url=vllm_server.url,
                      edit=lambda t: t.replace("interval = 2", "interval = 0.2"))
    return load_profile(cfg), build_engine(load_profile(cfg), run=system)


def test_the_engine_follows_the_workload_through_absent_running_stopped_absent(
        tmp_path, vllm_server):
    system = System(ids="")
    _, engine = make_engine(tmp_path, vllm_server, system)

    engine.tick()                                                    # absent
    assert engine.workload_state == "absent" and engine.health == "HEALTHY"
    assert engine.last_observation is None and engine.last_observed_at is None
    assert engine.incidents == [] and engine.pending == {}

    system.ids = SHORT + "\n"                                        # the operator starts it
    engine.tick()
    assert engine.workload_state == "running"
    assert engine.last_observation["inference_probe_ok"] is True     # real telemetry flows
    assert engine.last_observation["gpu_uuid"] and engine.last_observed_at
    assert engine.incidents == []                                    # healthy: nothing to do

    system.running, system.health = False, None                     # the operator stops it
    engine.tick()
    assert engine.workload_state == "stopped"
    assert engine.last_observation is None and engine.incidents == []

    system.ids = ""                                                  # ... and removes it
    engine.tick()
    assert engine.workload_state == "absent" and engine.last_observation is None
    assert not docker_verbs(system) & MUTATING                       # the engine created nothing


def test_an_absent_or_stopped_workload_is_never_an_unresponsive_incident(tmp_path):
    """vLLM unreachable + no workload = nothing to remediate, not INFERENCE_UNRESPONSIVE."""
    system = System(ids="")
    cfg = make_config(tmp_path, url="http://127.0.0.1:1")           # nothing listens there
    engine = build_engine(load_profile(cfg), run=system)
    for _ in range(3):
        engine.tick()
    assert engine.incidents == [] and engine.pending == {}
    system.ids, system.running, system.health = SHORT + "\n", False, None
    engine.tick()
    assert engine.workload_state == "stopped" and engine.incidents == []


def test_a_present_workload_that_does_not_answer_is_still_diagnosed_as_before(tmp_path):
    """Safety is unchanged: an unresponsive RUNNING workload still opens the incident."""
    system = System()                                               # running and labelled
    cfg = make_config(tmp_path, url="http://127.0.0.1:1")
    engine = build_engine(load_profile(cfg), run=system)
    engine.tick()
    assert engine.workload_state == "running"
    assert [i.category for i in engine.incidents] == ["INFERENCE_UNRESPONSIVE"]
    assert list(engine.pending.values())[0]["action"] == "restart_workload"


def test_an_ambiguous_workload_is_unknown_and_does_not_hide_a_real_problem(tmp_path):
    system = System(ids=SHORT + "\nba9876543210\n")
    engine = build_engine(load_profile(make_config(tmp_path, url="http://127.0.0.1:1")),
                          run=system)
    engine.tick()
    assert engine.workload_state == "unknown"                       # not "absent": still evaluated
    assert engine.last_observation is not None


# --- the real lifecycle, API and wiring ---------------------------------------------------------

def test_the_control_plane_starts_and_reports_the_workload_state_with_zero_workloads(
        tmp_path, vllm_server):
    system = System(ids="")
    cfg = make_config(tmp_path, url=vllm_server.url,
                      edit=lambda t: t.replace("interval = 2", "interval = 0.2"))
    port = load_profile(cfg)["control_plane"]["port"]
    r = Running(cfg, doctor=lambda p: run_doctor(p, run=system, which=which_all),
                build=lambda p: build_engine(p, run=system))
    try:
        status = wait_for(lambda: http(port, "/api/v1/status")["workload"]["state"] == "absent"
                          and http(port, "/api/v1/status"))
        assert status["health"] == "HEALTHY" and status["last_observation"] is None
        assert status["workload"] == {"name": "vllm", "state": "absent"}
        assert status["incidents"] == {"active": [], "pending": []}
        assert any("no managed workload" in l for l in r.lines)      # the WARN note, not a refusal
        assert not any("refusing" in l for l in r.lines)

        system.ids = SHORT + "\n"                                    # operator starts the workload
        status = wait_for(lambda: http(port, "/api/v1/status")["workload"]["state"] == "running"
                          and http(port, "/api/v1/status")["last_observation"]
                          and http(port, "/api/v1/status"))
        assert status["last_observation"]["inference_probe_ok"] is True

        system.ids = ""                                              # operator removes it
        status = wait_for(lambda: http(port, "/api/v1/status")["workload"]["state"] == "absent"
                          and http(port, "/api/v1/status"))
        assert status["last_observation"] is None and status["last_observed_at"] is None
    finally:
        assert r.finish() == 0
    assert not docker_verbs(system) & MUTATING                       # nothing was ever created


def test_the_status_of_a_legacy_engine_has_no_workload_section():
    import threading

    from test_engine import CONFIG, World

    from aiops.api import make_server
    from aiops.engine import Engine
    server = make_server(Engine(World(), World(), CONFIG), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert "workload" not in http(server.server_address[1], "/api/v1/status")
    finally:
        server.shutdown()
        server.server_close()


def test_a_workload_inside_its_start_period_is_observed_but_not_diagnosed_until_it_settles(tmp_path):
    """While Docker says `starting` (the model is loading) a failing probe is a boot, not an
    incident; once Docker says unhealthy/healthy detection is exactly what it was."""
    system = System(health="starting")
    engine = build_engine(load_profile(make_config(tmp_path, url="http://127.0.0.1:1")), run=system)
    for _ in range(3):
        engine.tick()
    assert engine.workload_state == "starting"
    assert engine.last_observation["inference_probe_ok"] is False     # honest: shown, not hidden
    assert engine.incidents == [] and engine.pending == {}
    system.health = "unhealthy"                                       # the start period is over
    engine.tick()
    assert engine.workload_state == "running"
    assert [i.category for i in engine.incidents] == ["INFERENCE_UNRESPONSIVE"]
