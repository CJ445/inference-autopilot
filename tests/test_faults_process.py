"""The reaper as a REAL, detached process, against a fake `docker` executable on PATH.

This is the crash-safety claim made concrete: the process that started a fault can die, the control
plane can die, and an independent process still resumes the workload at the deadline (and only the
workload this fault paused).
"""
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from faults_support import cid

from aiops.faults import injector as injector_module
from aiops.faults.core import AllowlistedDocker, read_lease, write_lease
from aiops.faults.injector import FaultInjector, spawn_reaper
from aiops.watchdog import proc_start

ROOT = Path(__file__).parent.parent
FAKE_DOCKER = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    path = os.environ["FAKE_DOCKER_STATE"]
    state = json.load(open(path))
    argv = sys.argv[1:]
    state["calls"].append(argv)
    out = ""
    if argv[0] == "ps":
        out = "".join(c["id"][:12] + "\\n" for c in state["containers"]
                      if c["labels"].get("com.inference-autopilot.managed") == "true"
                      and c["labels"].get("com.inference-autopilot.workload") == "vllm")
    elif argv[0] == "inspect":
        c = next(c for c in state["containers"] if c["id"].startswith(argv[1]))
        out = json.dumps([{{"Id": c["id"], "State": {{"Running": c["running"], "Paused": c["paused"],
                          "Restarting": False, "StartedAt": c["started"]}}, "Config": {{"Labels": c["labels"]}}}}])
    elif argv[0] in ("pause", "unpause"):
        c = next(c for c in state["containers"] if c["id"] == argv[1])
        c["paused"] = argv[0] == "pause"
    else:
        print("unexpected", argv, file=sys.stderr); sys.exit(9)
    json.dump(state, open(path, "w"))
    print(out, end="")
''')
LABELS = {"com.inference-autopilot.managed": "true", "com.inference-autopilot.workload": "vllm"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable))
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps({"calls": [], "containers": [
        {"id": cid(1), "labels": LABELS, "running": True, "paused": False,
         "started": "2026-10-02T10:00:00Z"}]}))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    w = type("World", (), {})()
    w.state_path, w.lease_path = state, tmp_path / "fault.json"
    w.state = lambda: json.loads(state.read_text())
    return w


def wait(fn, timeout=10, what=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def paused_lease(world, seconds, identity=None):
    """A fault the injector 'already started': the container is paused and the lease is ACTIVE."""
    state = world.state()
    state["containers"][0]["paused"] = True
    world.state_path.write_text(json.dumps(state))
    now = time.time()
    write_lease(world.lease_path, {
        "fault_id": "f1", "fault_type": "pause_workload", "workload": "vllm", "container_id": cid(1),
        "expected_identity": identity or f"{cid(1)}:2026-10-02T10:00:00Z", "status": "ACTIVE",
        "created_at": "x", "created_epoch": now, "expires_at": now + seconds, "duration_seconds": 1,
        "paused_at": "x", "audited_injected": False, "audited_recovered": False})


def test_a_detached_reaper_resumes_the_workload_at_the_deadline(world):
    paused_lease(world, 2)
    proc = spawn_reaper(world.lease_path)
    lease = wait(lambda: (l := read_lease(world.lease_path)).get("reaper") and l, what="the reaper to arm")
    assert lease["reaper"]["pid"] == proc.pid and lease["reaper"]["proc_start"] == proc_start(proc.pid)
    assert os.getsid(proc.pid) == proc.pid                          # its own session: not tied to us
    assert world.state()["containers"][0]["paused"] is True          # not before the deadline
    wait(lambda: read_lease(world.lease_path)["status"] == "RECOVERED", 10, "recovery")
    assert world.state()["containers"][0]["paused"] is False
    assert read_lease(world.lease_path)["recovery"]["by"] == "reaper"
    assert proc.wait(timeout=10) == 0


def test_the_reaper_outlives_the_process_that_started_the_fault(world, tmp_path):
    paused_lease(world, 3)
    # a short-lived "injector" starts the reaper and dies at once, like a crash
    subprocess.run([sys.executable, "-c",
                    f"from aiops.faults.injector import spawn_reaper; spawn_reaper({str(world.lease_path)!r})"],
                   check=True, cwd=ROOT, timeout=30)
    wait(lambda: read_lease(world.lease_path).get("reaper"), 10, "the orphaned reaper to arm")
    assert world.state()["containers"][0]["paused"] is True
    wait(lambda: read_lease(world.lease_path)["status"] == "RECOVERED", 12, "recovery by the orphan")
    assert world.state()["containers"][0]["paused"] is False


def test_the_reaper_never_resumes_a_workload_that_was_restarted_meanwhile(world):
    paused_lease(world, 2)
    state = world.state()
    state["containers"][0].update(started="2026-10-02T10:20:00Z", paused=True)   # a different run
    world.state_path.write_text(json.dumps(state))
    spawn_reaper(world.lease_path)
    wait(lambda: read_lease(world.lease_path)["status"] == "IDENTITY_CHANGED", 10, "the reaper to stand down")
    final = world.state()
    assert not any(call[0] == "unpause" for call in final["calls"])           # it never unpaused anything
    assert final["containers"][0]["paused"] is True                            # not ours to resume


def test_the_reaper_stands_down_when_the_lease_ends_early(world):
    paused_lease(world, 60)
    proc = spawn_reaper(world.lease_path)
    wait(lambda: read_lease(world.lease_path).get("reaper"), 10, "arming")
    lease = read_lease(world.lease_path)
    write_lease(world.lease_path, {**lease, "status": "RECOVERED"})            # ended by someone else
    assert proc.wait(timeout=10) == 0
    assert not any(call[0] == "unpause" for call in world.state()["calls"])


def test_a_real_injection_with_a_real_reaper_pauses_and_then_resumes(world, monkeypatch):
    monkeypatch.setattr(injector_module, "MIN_DURATION", 1, raising=False)
    monkeypatch.setattr("aiops.faults.core.MIN_DURATION", 1)
    inj = FaultInjector("vllm", world.lease_path)                              # real spawn, real runner
    active = inj.inject("pause_workload", "vllm", 2)
    assert active["status"] == "ACTIVE" and world.state()["containers"][0]["paused"] is True
    lease = read_lease(world.lease_path)
    assert os.getsid(lease["reaper"]["pid"]) == lease["reaper"]["pid"]
    wait(lambda: read_lease(world.lease_path)["status"] == "RECOVERED", 12, "the reaper to resume it")
    assert world.state()["containers"][0]["paused"] is False
    verbs = [c[0] for c in world.state()["calls"]]
    assert set(verbs) <= {"ps", "inspect", "pause", "unpause"}                  # nothing else was ever run
