"""SIMULATION mode: the production loop over a synthetic world, proven isolated and labelled.

These tests assert state transitions (incident states, audit events, lifecycle generation,
verification checks, the probe log), not text.
"""
import ast
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from aiops.api import make_server
from aiops.engine import Engine
from aiops.sim import scenario as scenario_module
from aiops.sim.scenario import GOLDEN, SCENARIOS, ScenarioError
from aiops.sim.session import SimSession
from aiops.sim.world import SimWorld
from aiops.store import Store, StoreUnavailable

ROOT = Path(__file__).parent.parent
SIM_DIR = ROOT / "aiops" / "sim"
EVENTS = ["fault_injected", "incident_created", "rca_generated", "remediation_proposed",
          "policy_evaluated", "approval_granted", "remediation_started", "remediation_finished",
          "verification_finished"]
TIMELINE = ["DETECTED", "TRIAGING", "DIAGNOSED", "PROPOSED", "POLICY_CHECK", "APPROVED",
            "EXECUTING", "VERIFYING", "RESOLVED"]


def make(tmp_path, **kw):
    return SimSession(tmp_path / "simulation.db", verify_timeout=0.2, **kw)


def run(session, decision):
    return SCENARIOS[GOLDEN](session, lambda incident: decision)


def events(session):
    return [e["event"] for e in session.engine.audit.events]


# --- the golden scenario ----------------------------------------------------------------------

def test_the_golden_scenario_resolves_through_the_production_loop(tmp_path):
    s = make(tmp_path)
    result = run(s, "approve")
    inc = s.incident
    assert result.outcome == "RESOLVED" and inc.status == "RESOLVED"
    assert [t["state"] for t in inc.timeline] == TIMELINE
    assert events(s) == EVENTS
    assert s.world.generation == 2 and s.world.restarts == 1       # the lifecycle identity changed
    verification = s.engine.audit.events[-1]["data"]["checks"]
    assert verification == {"workload_restarted": True, "gpu_observable": True,
                            "vllm_metrics_readable": True, "inference_probe_stable": True}
    # healthy x2, the two failed probes the detector needed, then the 3 consecutive completions
    assert s.world.probe_log == [True, True, False, False, True, True, True]
    assert s.engine.audit.verify() and s.engine.pending == {}


def test_the_scenario_is_deterministic(tmp_path):
    a = SimSession(tmp_path / "first.db", verify_timeout=0.2)
    b = SimSession(tmp_path / "second.db", verify_timeout=0.2)
    ra, rb = run(a, "approve"), run(b, "approve")
    assert [(s["title"], s["detail"]) for s in ra.steps] == [(s["title"], s["detail"]) for s in rb.steps]
    assert events(a) == events(b)
    assert [t["state"] for t in a.incident.timeline] == [t["state"] for t in b.incident.timeline]
    assert a.world.probe_log == b.world.probe_log


def test_one_failed_probe_is_not_an_incident_even_here(tmp_path):
    s = make(tmp_path)
    s.tick()
    s.inject_fault()
    assert s.tick() is None and s.engine.incidents == [] and s.stage == "detecting"
    assert s.tick() is not None and s.stage == "awaiting_approval"


# --- negative paths ---------------------------------------------------------------------------

def test_without_a_decision_nothing_is_ever_restarted(tmp_path):
    s = make(tmp_path)
    result = run(s, None)
    assert result.outcome == "AWAITING_APPROVAL"
    for _ in range(5):
        s.tick()                                                   # time passes; still nothing
    assert s.incident.status == "POLICY_CHECK" and s.world.restarts == 0
    assert s.world.generation == 1 and s.incident.incident_id in s.engine.pending
    assert "approval_granted" not in events(s)


def test_a_rejected_proposal_restarts_nothing(tmp_path):
    s = make(tmp_path)
    result = run(s, "reject")
    assert result.outcome == "REJECTED" and s.incident.status == "REJECTED"
    assert s.world.restarts == 0 and s.world.generation == 1 and s.engine.pending == {}
    assert "approval_denied" in events(s) and "remediation_started" not in events(s)


def test_a_restart_that_does_not_recover_the_model_is_unresolved_never_resolved(tmp_path):
    s = make(tmp_path, world=SimWorld(recovers=False))
    result = run(s, "approve")
    assert result.outcome == "UNRESOLVED" and s.incident.status == "UNRESOLVED"
    assert s.world.restarts == 1                                   # it did restart ...
    checks = s.engine.audit.events[-1]["data"]["checks"]
    assert checks["workload_restarted"] is True                    # ... the identity did change ...
    assert checks["inference_probe_stable"] is False               # ... but inference is still down


def test_a_persistence_failure_fails_closed_and_consumes_nothing(tmp_path, monkeypatch):
    s = make(tmp_path)
    run(s, None)                                                   # at the decision point
    incident_id, n_events = s.incident.incident_id, len(s.engine.audit.events)

    def broken(*a, **k):
        raise StoreUnavailable("disk full (simulated)")

    monkeypatch.setattr(s.store, "save", broken)
    with pytest.raises(StoreUnavailable):
        s.approve()
    assert s.world.restarts == 0 and s.world.generation == 1       # nothing was mutated
    assert s.incident.status == "POLICY_CHECK" and incident_id in s.engine.pending
    assert len(s.engine.audit.events) == n_events                  # and nothing was recorded


def test_a_malformed_observation_opens_nothing_and_remediates_nothing(tmp_path):
    s = make(tmp_path)
    s.world.probe_override = {"ok": None, "latency_ms": None, "error": "garbled"}
    for _ in range(5):
        assert s.tick() is None
    assert s.engine.incidents == [] and s.engine.pending == {} and s.world.restarts == 0
    s.world.probe_override = None
    s.world.gpu_readable = False                                   # an unreadable observation
    for _ in range(3):
        assert s.tick() is None
    assert s.engine.health == "DEGRADED" and s.engine.incidents == [] and s.world.restarts == 0


def test_the_scenario_raises_instead_of_narrating_a_loop_that_did_not_happen(tmp_path):
    s = make(tmp_path)
    s.engine.presence = lambda: "absent"          # the engine now (correctly) sees no workload
    with pytest.raises(ScenarioError):
        run(s, "approve")


# --- isolation --------------------------------------------------------------------------------

FORBIDDEN_IMPORTS = {"subprocess", "socket", "urllib", "http", "shutil", "ctypes", "multiprocessing",
                     "asyncio", "ssl", "ftplib", "smtplib", "requests", "pty"}
FORBIDDEN_AIOPS = {"aiops.docker", "aiops.gpu", "aiops.vllm", "aiops.runtime", "aiops.lifecycle",
                   "aiops.doctor", "aiops.watchdog", "aiops.supervise", "aiops.gpu_fault",
                   "aiops.launcher", "aiops.system", "aiops.serve", "aiops.api", "aiops.profile"}
ALLOWED_NAMES = {"aiops.kubectl": {"ClusterError"}, "aiops.prometheus": {"TelemetryError"}}
FORBIDDEN_OS_CALLS = {"system", "popen", "fork", "kill", "execv", "execvp", "execl", "spawnv",
                      "posix_spawn", "startfile"}


def sim_sources():
    return sorted(SIM_DIR.glob("*.py"))


def test_the_simulation_package_imports_nothing_that_can_reach_the_outside_world():
    assert len(sim_sources()) >= 5
    for path in sim_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module]
                allowed = ALLOWED_NAMES.get(node.module)
                if allowed is not None:                 # existing exception classes only
                    assert {a.name for a in node.names} <= allowed, (path.name, node.module)
            else:
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                        and node.value.id == "os":
                    assert node.attr not in FORBIDDEN_OS_CALLS, (path.name, node.attr)
                continue
            for name in names:
                top = (name or "").split(".")[0]
                assert top not in FORBIDDEN_IMPORTS, (path.name, name)
                assert name not in FORBIDDEN_AIOPS, (path.name, name)


def test_a_fresh_interpreter_running_the_scenario_never_loads_a_real_provider():
    code = (
        "import sys, tempfile, os\n"
        "from aiops.sim.demo import main\n"
        "import aiops.sim.session, aiops.sim.scenario\n"
        "assert main(['--approve'], out=lambda *a: None) == 0\n"
        f"bad = sorted(m for m in {sorted(FORBIDDEN_AIOPS)!r} if m in sys.modules)\n"
        "print('LOADED', bad)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    assert "LOADED []" in r.stdout


def test_the_scenario_runs_with_every_outside_door_closed(tmp_path, monkeypatch):
    import http.client
    import shutil

    def shut(*a, **k):
        raise AssertionError("the simulation reached outside itself")

    for target, name in [(subprocess, "Popen"), (subprocess, "run"), (subprocess, "call"),
                         (subprocess, "check_output"), (os, "system"), (os, "popen"),
                         (shutil, "which"), (socket.socket, "connect"),
                         (socket, "create_connection"), (urllib.request, "urlopen"),
                         (http.client.HTTPConnection, "request")]:
        monkeypatch.setattr(target, name, shut)
    s = make(tmp_path)
    assert run(s, "approve").outcome == "RESOLVED"


def test_the_simulation_provider_only_acts_on_its_own_workload(tmp_path):
    from aiops.kubectl import ClusterError
    from aiops.sim.world import SimProvider
    w = SimWorld()
    p = SimProvider(w)
    with pytest.raises(ClusterError):
        p.restart_workload("vllm")                       # a real workload's name is refused
    assert w.restarts == 0 and w.generation == 1
    assert p.get_workload("sim-vllm")["id"] == "sim:sim-vllm:generation-1"


# --- the store keeps the two worlds apart --------------------------------------------------------

def test_a_real_store_refuses_a_simulation_database(tmp_path):
    path = tmp_path / "x.db"
    SimSession(path)
    with pytest.raises(StoreUnavailable, match="SIMULATION"):
        Store(path)
    with pytest.raises(StoreUnavailable, match="SIMULATION"):
        Store(path, readonly=True)


def test_a_simulation_store_refuses_a_database_with_real_state(tmp_path):
    path = tmp_path / "real.db"
    real = Engine(SimWorld().read_gpu, None, {"service": "vllm"}, store=Store(path))
    real.audit.append("incident_created", {"incident_id": "inc_001"})
    Store(path).save([], {}, real.audit)
    with pytest.raises(StoreUnavailable, match="not a SIMULATION"):
        Store(path, mode="SIMULATION")


def test_the_real_store_schema_is_unchanged(tmp_path):
    path = tmp_path / "real.db"
    Store(path)
    tables = {r[0] for r in sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"incidents", "pending", "audit"}             # no meta table for real state


def test_a_reopened_simulation_keeps_its_labels(tmp_path):
    path = tmp_path / "simulation.db"
    first = SimSession(path, verify_timeout=0.2)
    run(first, None)
    again = SimSession(path, verify_timeout=0.2)
    assert again.incident.mode == "SIMULATION" and len(again.engine.audit.events) == 5
    again.engine.audit.append("note", {})
    assert again.engine.audit.events[-1]["data"]["mode"] == "SIMULATION"


# --- nothing can be mistaken for real -----------------------------------------------------------

def test_every_record_of_a_simulation_is_marked_simulation(tmp_path):
    s = make(tmp_path)
    run(s, "approve")
    assert all(e["data"]["mode"] == "SIMULATION" for e in s.engine.audit.events)
    inc = s.incident
    assert inc.mode == "SIMULATION" and inc.to_dict()["mode"] == "SIMULATION"
    for e in inc.evidence:
        assert e["source_type"] == "simulation" and e["source"].startswith("simulated-")
    remediation = [e for e in s.engine.audit.events if e["event"].startswith("remediation_")]
    assert remediation and all(e["data"]["mode"] == "SIMULATION" for e in remediation)
    # and it survives storage
    stored = json.loads(sqlite3.connect(tmp_path / "simulation.db").execute(
        "SELECT data FROM incidents").fetchone()[0])
    assert stored["mode"] == "SIMULATION"


def api_get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.load(r)


def test_the_api_reports_the_mode_and_a_real_engine_reports_real(tmp_path):
    s = make(tmp_path)
    run(s, None)
    server = make_server(s.engine, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert api_get(port, "/api/v1/status")["mode"] == "SIMULATION"
        listed = api_get(port, "/api/v1/incidents")["incidents"]
        assert [i["mode"] for i in listed] == ["SIMULATION"]
        assert api_get(port, f"/api/v1/incidents/{listed[0]['incident_id']}")["mode"] == "SIMULATION"
    finally:
        server.shutdown()
        server.server_close()
    from test_engine import CONFIG, World
    real = Engine(World(), World(), CONFIG)
    assert real.mode == "REAL" and real.audit.mode is None


def test_a_real_engine_marks_nothing_and_its_evidence_stays_real(tmp_path):
    from test_engine import CONFIG, World
    w = World()
    w.fault, w.failures = True, 5
    e = Engine(w, w, CONFIG)
    inc = e.tick()
    assert "mode" not in inc.to_dict()
    assert all("mode" not in ev["data"] for ev in e.audit.events)
    assert all(ev["source_type"] == "real" for ev in inc.evidence)


# --- the command ------------------------------------------------------------------------------

def demo(*args, cwd, **kw):
    env = {"PATH": str(cwd), "HOME": str(cwd), "PYTHONPATH": str(ROOT)}   # no docker, no nvidia-smi
    return subprocess.run([sys.executable, "-m", "aiops", "demo", *args], capture_output=True,
                          text=True, cwd=cwd, env=env, timeout=60)


def test_aiops_demo_runs_with_no_config_no_docker_and_no_gpu(tmp_path):
    r = demo("--approve", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    for needle in ["SIMULATION", "nothing here touches your GPU", "generation-1 -> ",
                   "generation-2", "RESOLVED", "hash chain intact", "nothing real changed"]:
        assert needle in r.stdout, needle
    assert list(tmp_path.iterdir()) == []              # it created no files outside its scratch dir


def test_aiops_demo_reject_and_missing_decision(tmp_path):
    r = demo("--reject", cwd=tmp_path)
    assert r.returncode == 0 and "REJECTED" in r.stdout and "RESOLVED" not in r.stdout
    r = demo(cwd=tmp_path)                             # not a terminal and no decision
    assert r.returncode == 2 and "--approve or --reject" in r.stdout
    assert demo("--approve", "--reject", cwd=tmp_path).returncode != 0


def test_the_demo_is_listed_in_the_usage():
    r = subprocess.run([sys.executable, "-m", "aiops", "--help"], capture_output=True, text=True,
                       cwd=ROOT)
    assert "aiops demo" in r.stdout and "SIMULATION" in r.stdout


def test_a_simulation_engine_without_a_store_still_stamps_its_audit_chain():
    from aiops.sim.world import SimProvider, SimTelemetry
    w = SimWorld()
    e = Engine(SimTelemetry(w), SimProvider(w), {"service": "vllm", "workload": "sim-vllm",
                                                  "gpu_threshold": 4_500_000_000}, mode="SIMULATION")
    e.audit.append("anything", {"x": 1})
    assert e.audit.events[0]["data"] == {"x": 1, "mode": "SIMULATION"} and e.audit.verify()
