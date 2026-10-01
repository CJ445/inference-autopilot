import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
import urllib.request
from pathlib import Path

import pytest
from test_doctor import SHORT, System, which_all  # noqa: F401
from test_engine import CONFIG, World
from test_profile import DOCKER, write
from test_store import faulted_world
from test_vllm import Server

from aiops.doctor import FAIL, PASS, run_doctor
from aiops.engine import Engine
from aiops.lifecycle import (is_running, proc_start, read_state, spawn_watchdog, start, status,
                             stop)
from aiops.runtime import build_engine
from aiops.store import Store, StoreUnavailable
from aiops.watchdog import boot_id


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_config(tmp_path, url="http://127.0.0.1:1", port=None, edit=lambda t: t):
    text = DOCKER.replace("port = 8123", f"port = {port or free_port()}") \
                 .replace("http://127.0.0.1:8001", url)
    return write(tmp_path, edit(text))


def ok_doctor(profile):
    return [{"check": "x", "status": PASS, "detail": "ok", "blocking": True}]


def fake_build(world):
    def build(profile):
        db = Path(profile["control_plane"]["db"])
        db.parent.mkdir(parents=True, exist_ok=True)
        return Engine(world, world, CONFIG, store=Store(db))
    return build


class FakeWatchdog:
    def __init__(self, owner, profile):
        self.owner, self.profile, self.disarmed, self.exit_code = owner, profile, 0, None
        self.pid = 424242

    def wait_armed(self, timeout):
        if self.owner.on_arm:
            self.owner.on_arm(self)
        return self.owner.arms

    def poll(self):
        return self.exit_code

    def disarm(self):
        self.disarmed += 1


class FakeWatchdogs:
    """Stand-in spawner: tests of start/stop semantics must never arm a real watchdog on the
    pytest process. The real spawner is exercised against a sacrificial child below."""

    def __init__(self, arms=True, raises=None, on_arm=None):
        self.arms, self.raises, self.on_arm, self.created = arms, raises, on_arm, []
        self.stale_at_spawn = None

    def __call__(self, profile):
        if self.raises:
            raise self.raises
        self.stale_at_spawn = Path(profile["control_plane"]["state_file"] + ".watchdog").exists()
        wd = FakeWatchdog(self, profile)
        self.created.append(wd)
        return wd


def http(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.load(r)


def wait_for(fn, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.02)
    raise AssertionError("timed out")


class Running:
    """`start` running in a thread, exactly as the CLI would run it in the foreground."""

    def __init__(self, config, **kw):
        self.stop_event, self.lines, self.rc = threading.Event(), [], []
        kw.setdefault("doctor", ok_doctor)
        kw.setdefault("build", fake_build(World()))
        kw.setdefault("spawn_watchdog", FakeWatchdogs())
        self.thread = threading.Thread(target=lambda: self.rc.append(
            start(config, self.stop_event, out=self.lines.append, **kw)), daemon=True)
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()
        return self.rc[0]


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


def state_path(tmp_path):
    return tmp_path / "run" / "aiops.state.json"


# --- start --------------------------------------------------------------------------------

def test_start_runs_the_control_plane_records_its_state_and_stops_cleanly(tmp_path, config):
    from aiops.profile import load_profile

    port = load_profile(config)["control_plane"]["port"]
    r = Running(config)
    state = wait_for(lambda: read_state(state_path(tmp_path)))
    assert state["pid"] == os.getpid() and state["profile"] == "docker-real-gpu"
    assert state["provider"] == "docker" and state["workload"] == "vllm" and state["port"] == port
    assert is_running(state)
    assert wait_for(lambda: http(port, "/health")["status"] == "HEALTHY")
    assert http(port, "/api/v1/status")["info"]["profile"] == "docker-real-gpu"

    assert r.finish() == 0
    assert not state_path(tmp_path).exists()
    with pytest.raises(urllib.error.URLError):
        http(port, "/health")


def test_start_is_idempotent_and_never_builds_a_second_control_plane(tmp_path, config):
    built = []

    def build(profile):
        built.append(1)
        return fake_build(World())(profile)

    first = Running(config, build=build)
    wait_for(lambda: read_state(state_path(tmp_path)))
    lines = []
    assert start(config, threading.Event(), out=lines.append, doctor=ok_doctor, build=build) == 0
    assert any("already running" in l for l in lines) and built == [1]
    assert first.finish() == 0


def test_start_rejects_an_invalid_configuration_before_doing_anything(tmp_path):
    bad = write(tmp_path, 'profile = "fake-gpu"\n')
    lines, called = [], []
    rc = start(bad, threading.Event(), out=lines.append,
               doctor=lambda p: called.append("doctor"), build=lambda p: called.append("build"))
    assert rc == 2 and called == [] and any("configuration error" in l for l in lines)
    assert not state_path(tmp_path).exists()


def test_start_refuses_when_a_blocking_prerequisite_fails(tmp_path, config):
    built, lines = [], []
    failing = lambda p: [{"check": "workload", "status": FAIL, "blocking": True,
                          "detail": "no managed workload named 'vllm'"}]
    rc = start(config, threading.Event(), out=lines.append, doctor=failing,
               build=lambda p: built.append(1))
    assert rc == 1 and built == []
    assert any("no managed workload" in l for l in lines)
    assert any("refusing to start" in l for l in lines)
    assert not state_path(tmp_path).exists() and not (tmp_path / "data").exists()


def test_start_proceeds_when_only_workload_health_checks_fail(tmp_path, config):
    # a hung workload is what the control plane exists to handle; it must still be startable
    unhealthy = lambda p: [
        {"check": "workload", "status": PASS, "blocking": True, "detail": "found"},
        {"check": "vllm_probe", "status": FAIL, "blocking": False, "detail": "probe failed"}]
    r = Running(config, doctor=unhealthy)
    wait_for(lambda: read_state(state_path(tmp_path)))
    assert any("probe failed" in l for l in r.lines)
    assert r.finish() == 0


def test_start_fails_closed_when_persistence_cannot_be_opened(tmp_path, config):
    def broken(profile):
        raise StoreUnavailable("attempt to write a readonly database")

    lines = []
    rc = start(config, threading.Event(), out=lines.append, doctor=ok_doctor, build=broken)
    assert rc == 1 and any("persistence" in l.lower() or "database" in l.lower() for l in lines)
    assert not state_path(tmp_path).exists()


def test_start_with_a_corrupt_database_is_blocked_by_the_real_doctor(tmp_path, vllm_server_url):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "aiops.db").write_bytes(b"not a database " * 40)
    cfg = make_config(tmp_path, url=vllm_server_url)
    built, lines = [], []
    rc = start(cfg, threading.Event(), out=lines.append,
               doctor=lambda p: run_doctor(p, run=System(), which=which_all),
               build=lambda p: built.append(1))
    assert rc == 1 and built == [] and any("database" in l for l in lines)


@pytest.fixture
def vllm_server_url():
    s = Server()
    yield s.url
    s.httpd.shutdown()


def test_start_replaces_stale_state_left_by_a_dead_process(tmp_path, config):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"pid": dead.pid, "proc_start": "1", "port": 1}))
    r = Running(config)
    state = wait_for(lambda: (read_state(state_path(tmp_path)) or {}).get("pid") == os.getpid())
    assert state and any("stale" in l for l in r.lines)
    assert r.finish() == 0


def test_a_recycled_pid_is_not_mistaken_for_a_running_control_plane(tmp_path):
    # same pid as a live process, but a different start time: not ours
    state = {"pid": os.getpid(), "proc_start": "definitely-not-this-process"}
    assert proc_start(os.getpid()) and not is_running(state)
    assert is_running({"pid": os.getpid(), "proc_start": proc_start(os.getpid())})


def test_start_with_an_unreadable_state_file_fails_closed(tmp_path, config):
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text("{ this is not json")
    built, lines = [], []
    rc = start(config, threading.Event(), out=lines.append, doctor=ok_doctor,
               build=lambda p: built.append(1))
    assert rc == 1 and built == [] and any("state file" in l for l in lines)
    assert state_path(tmp_path).read_text() == "{ this is not json"   # left for the operator


def test_start_fails_cleanly_when_the_port_is_taken(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        cfg = make_config(tmp_path, port=s.getsockname()[1])
        lines = []
        rc = start(cfg, threading.Event(), out=lines.append, doctor=ok_doctor,
                   build=fake_build(World()))
    assert rc == 1 and any("port" in l.lower() for l in lines)
    assert not state_path(tmp_path).exists()


# --- stop ---------------------------------------------------------------------------------

def test_stop_signals_the_running_control_plane_and_waits_for_it_to_exit(tmp_path, config):
    r = Running(config)
    wait_for(lambda: read_state(state_path(tmp_path)))
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        r.stop_event.set()      # what the SIGTERM handler does in the real process

    lines = []
    assert stop(config, out=lines.append, kill=kill, wait_seconds=10) == 0
    assert sent == [(os.getpid(), 15)]                 # SIGTERM to the recorded pid
    r.thread.join(timeout=10)
    assert not r.thread.is_alive() and r.rc == [0]
    assert not state_path(tmp_path).exists()


def test_stop_is_safe_when_already_stopped(tmp_path, config):
    lines = []
    assert stop(config, out=lines.append, kill=lambda *a: pytest.fail("signalled nothing")) == 0
    assert any("already stopped" in l for l in lines)
    assert stop(config, out=lines.append, kill=lambda *a: pytest.fail("again")) == 0   # twice


def test_stop_cleans_up_stale_state_without_signalling_anything(tmp_path, config):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"pid": dead.pid, "proc_start": "1"}))
    assert stop(config, out=lambda l: None,
                kill=lambda *a: pytest.fail("must not signal a dead pid")) == 0
    assert not state_path(tmp_path).exists()


def test_stop_never_forces_a_control_plane_that_does_not_exit(tmp_path, config):
    r = Running(config)
    wait_for(lambda: read_state(state_path(tmp_path)))
    lines = []
    sent = []
    assert stop(config, out=lines.append, kill=lambda pid, sig: sent.append(sig),
                wait_seconds=0.3) == 1
    assert sent == [15] and any("did not exit" in l for l in lines)   # SIGTERM only, no SIGKILL
    assert state_path(tmp_path).exists()
    assert r.finish() == 0


# --- status: observed state, never assumed ---------------------------------------------------

def observed(tmp_path, url, system=None, **kw):
    return status(make_config(tmp_path, url=url), run=system or System(), **kw)


def test_status_reports_observed_infrastructure_even_when_the_control_plane_is_stopped(
        tmp_path, vllm_server_url):
    s = observed(tmp_path, vllm_server_url)
    assert s["control_plane"]["state"] == "stopped"
    assert (s["profile"], s["provider"], s["workload"]["name"]) == (
        "docker-real-gpu", "docker", "vllm")
    assert s["workload"]["observed"]["found"] is True and s["workload"]["observed"]["ready"] is True
    assert s["gpu"]["uuid"].startswith("GPU-") and s["gpu"]["memory_total_mib"] == 8188
    assert s["vllm"]["probe"]["ok"] is True and s["vllm"]["metrics_readable"] is True
    assert s["last_telemetry"]["available"] is False   # control plane stopped: nothing to report


def test_status_reflects_the_workloads_real_state_not_the_desired_one(tmp_path, vllm_server_url):
    s = observed(tmp_path, vllm_server_url, System(health="unhealthy"))
    assert s["workload"]["observed"]["ready"] is False
    gone = observed(tmp_path, vllm_server_url, System(ids=""))
    assert gone["workload"]["observed"]["found"] is False
    assert "no managed workload" in gone["workload"]["observed"]["error"]


def test_status_reports_unavailable_gpu_and_vllm_as_such(tmp_path):
    s = observed(tmp_path, "http://127.0.0.1:1",
                 System(fail={"nvidia-smi": FileNotFoundError("nvidia-smi")}))
    assert "error" in s["gpu"] and s["vllm"]["probe"]["ok"] is False
    assert s["vllm"]["metrics_readable"] is False


def test_status_of_a_running_control_plane_includes_its_last_real_observation(
        tmp_path, vllm_server_url):
    cfg = make_config(tmp_path, url=vllm_server_url)
    world = World()
    r = Running(cfg, build=fake_build(world))
    try:
        wait_for(lambda: read_state(state_path(tmp_path)))
        wait_for(lambda: status(cfg, run=System())["last_telemetry"].get("available"))
        s = status(cfg, run=System())
        assert s["control_plane"]["state"] == "running" and s["control_plane"]["pid"] == os.getpid()
        assert s["last_telemetry"]["health"] == "HEALTHY"
        assert s["last_telemetry"]["age_seconds"] >= 0
    finally:
        r.finish()


def test_status_reports_a_stale_state_file_as_stale_not_running(tmp_path, vllm_server_url):
    cfg = make_config(tmp_path, url=vllm_server_url)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"pid": dead.pid, "proc_start": "1", "port": 1}))
    s = status(cfg, run=System())
    assert s["control_plane"]["state"] == "stale"
    assert state_path(tmp_path).exists()                      # status never mutates


def test_status_shows_active_incident_pending_proposal_and_audit_integrity(
        tmp_path, vllm_server_url):
    cfg = make_config(tmp_path, url=vllm_server_url)
    (tmp_path / "data").mkdir()
    w = faulted_world()
    inc = Engine(w, w, CONFIG, store=Store(tmp_path / "data" / "aiops.db")).tick()
    s = status(cfg, run=System())
    assert s["active_incident"] == [{"id": inc.incident_id, "category": "GPU_MEMORY_PRESSURE",
                                     "status": "POLICY_CHECK"}]
    assert s["pending_proposal"] == [{"incident_id": inc.incident_id, "action": "restart_workload",
                                      "workload": "vllm-0"}]
    assert s["audit_integrity"].startswith("valid")


def test_status_without_a_database_creates_none_and_says_so(tmp_path, vllm_server_url):
    s = observed(tmp_path, vllm_server_url)
    assert s["audit_integrity"] == "no database" and s["active_incident"] == []
    assert not (tmp_path / "data").exists() and not state_path(tmp_path).exists()


def test_status_flags_a_tampered_audit_chain(tmp_path, vllm_server_url):
    import sqlite3

    cfg = make_config(tmp_path, url=vllm_server_url)
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "aiops.db"
    w = faulted_world()
    Engine(w, w, CONFIG, store=Store(db)).tick()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit SET data = '{\"incident_id\": \"forged\"}' WHERE seq = 0")
    assert status(cfg, run=System())["audit_integrity"].startswith("TAMPERED")


# --- safety: lifecycle never bypasses policy, never substitutes telemetry --------------------

def test_no_lifecycle_operation_ever_restarts_anything_or_consumes_a_pending_proposal(
        tmp_path, vllm_server_url):
    cfg = make_config(tmp_path, url=vllm_server_url)
    (tmp_path / "data").mkdir()
    w = faulted_world()
    pre = Engine(w, w, CONFIG, store=Store(tmp_path / "data" / "aiops.db")).tick()
    system = System()

    r = Running(cfg, doctor=lambda p: run_doctor(p, run=system, which=which_all),
                build=lambda p: build_engine(p, run=system))
    wait_for(lambda: read_state(state_path(tmp_path)))
    time.sleep(0.5)                                    # several ticks run against the pending one
    status(cfg, run=system)
    stop(cfg, out=lambda l: None, kill=lambda pid, sig: r.stop_event.set(), wait_seconds=10)
    r.thread.join(timeout=10)

    assert not [c for c in system.calls if c[:2] in (["docker", "restart"], ["docker", "kill"],
                                                      ["docker", "rm"], ["docker", "exec"])]
    incidents, pending, audit = Store(tmp_path / "data" / "aiops.db", readonly=True).load()
    assert pending and pre.incident_id in pending       # still waiting for explicit approval
    assert next(i for i in incidents if i.incident_id == pre.incident_id).status == "POLICY_CHECK"
    assert "approval_granted" not in [e["event"] for e in audit.events]


def test_the_real_profile_degrades_instead_of_substituting_telemetry_when_the_gpu_fails(
        tmp_path, vllm_server_url):
    from aiops.profile import load_profile

    cfg = make_config(tmp_path, url=vllm_server_url)
    system = System(fail={"nvidia-smi": FileNotFoundError("nvidia-smi")})
    port = load_profile(cfg)["control_plane"]["port"]
    r = Running(cfg, build=lambda p: build_engine(p, run=system))
    try:
        assert wait_for(lambda: http(port, "/health")["status"] == "DEGRADED")
        assert http(port, "/api/v1/incidents")["incidents"] == []
        assert http(port, "/api/v1/status")["last_observation"] is None   # nothing invented
    finally:
        r.finish()


# --- the watchdog relationship (Slice 7) --------------------------------------------------------

def wd_state_path(tmp_path):
    return tmp_path / "run" / "aiops.state.json.watchdog"


def write_wd_state(tmp_path, **fields):
    wd_state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    record = {"status": "ARMED", "pid": os.getpid(), "proc_start": proc_start(os.getpid()),
              "protected": {"pid": os.getpid(), "start": proc_start(os.getpid()),
                            "boot_id": boot_id()},
              "limits": {"max_ram_percent": 90, "max_runtime_seconds": 600}, "interval": 1.0,
              "armed_at": now, "last_check_at": now, "consecutive_sensor_failures": 0,
              "last_sample": {"gpu_memory_percent": 35.0, "temperature_c": 55.0,
                              "ram_percent": 50.0, "runtime_seconds": 12.0}}
    record.update(fields)
    wd_state_path(tmp_path).write_text(json.dumps(record))
    return record


def test_start_arms_the_watchdog_before_the_control_plane_observes_anything(tmp_path, config):
    built, seen = [], {}

    def build(profile):
        built.append(fake_build(World())(profile))
        return built[0]

    wds = FakeWatchdogs(on_arm=lambda wd: seen.update(at_arm=built[0].last_observation))
    r = Running(config, build=build, spawn_watchdog=wds)
    wait_for(lambda: read_state(state_path(tmp_path)))
    wait_for(lambda: built[0].last_observation)               # ticking only after arming
    assert len(wds.created) == 1 and seen["at_arm"] is None
    assert wds.created[0].profile["workload"]["name"] == "vllm"
    assert r.finish() == 0
    assert wds.created[0].disarmed == 1                       # stop disarms it


def test_a_watchdog_that_cannot_arm_stops_start_and_leaves_nothing_behind(tmp_path, config):
    from aiops.profile import load_profile

    port = load_profile(config)["control_plane"]["port"]
    built, lines = [], []
    wds = FakeWatchdogs(arms=False)

    def build(profile):
        built.append(fake_build(World())(profile))
        return built[0]

    rc = start(config, threading.Event(), out=lines.append, doctor=ok_doctor, build=build,
               spawn_watchdog=wds)
    assert rc == 1 and any("watchdog" in l and "refusing to start" in l for l in lines)
    assert wds.created[0].disarmed == 1                       # no watchdog left behind
    assert not state_path(tmp_path).exists()
    assert built[0].last_observation is None                  # never ran unprotected
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)   # port released


def test_a_watchdog_that_cannot_even_be_spawned_stops_start(tmp_path, config):
    from aiops.watchdog import IdentityError

    lines = []
    rc = start(config, threading.Event(), out=lines.append, doctor=ok_doctor,
               build=fake_build(World()),
               spawn_watchdog=FakeWatchdogs(raises=IdentityError("cannot protect")))
    assert rc == 1 and any("cannot protect" in l for l in lines)
    assert not state_path(tmp_path).exists()


def test_stop_through_the_lifecycle_disarms_the_watchdog(tmp_path, config):
    wds = FakeWatchdogs()
    r = Running(config, spawn_watchdog=wds)
    wait_for(lambda: read_state(state_path(tmp_path)))
    assert stop(config, out=lambda l: None, kill=lambda pid, sig: r.stop_event.set(),
                wait_seconds=10) == 0
    r.thread.join(timeout=10)
    assert wds.created[0].disarmed == 1 and not state_path(tmp_path).exists()


def test_a_stale_watchdog_state_file_is_rejected_before_arming(tmp_path, config):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_wd_state(tmp_path, pid=dead.pid, proc_start="1")
    wds = FakeWatchdogs()
    r = Running(config, spawn_watchdog=wds)
    wait_for(lambda: read_state(state_path(tmp_path)))
    assert wds.stale_at_spawn is False                        # removed before the new one spawned
    assert r.finish() == 0


def test_a_stale_watchdog_state_file_cannot_pass_for_a_fresh_arming(tmp_path):
    from aiops.profile import load_profile
    from test_profile import KUBERNETES

    cfg = write(tmp_path, KUBERNETES)
    profile = load_profile(cfg)
    (tmp_path / "aiops.state.json.watchdog").write_text(json.dumps(   # someone else's ARMED record
        {"status": "ARMED", "pid": 1, "proc_start": "1"}))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        handle = spawn_watchdog(profile, protected_pid=child.pid + 10_000_000)  # cannot arm
        assert handle.wait_armed(timeout=10) is False          # the stale record is not trusted
        handle.disarm()
    finally:
        child.kill()
        child.wait()


def test_the_real_watchdog_handle_arms_disarms_and_is_safe_to_disarm_twice(tmp_path):
    from aiops.profile import load_profile
    from test_profile import KUBERNETES

    profile = load_profile(write(tmp_path, KUBERNETES))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        handle = spawn_watchdog(profile, protected_pid=child.pid)
        assert handle.wait_armed(timeout=15) is True and handle.poll() is None
        record = json.loads((tmp_path / "aiops.state.json.watchdog").read_text())
        assert record["status"] == "ARMED" and record["protected"]["pid"] == child.pid
        handle.disarm()
        handle.disarm()                                       # idempotent
        assert handle.poll() == 0
        assert child.poll() is None                           # disarming never touches the process
        assert not (tmp_path / "aiops.state.json.watchdog").exists()
    finally:
        child.kill()
        child.wait()


def test_a_watchdog_that_dies_while_running_stops_the_control_plane_fail_closed(tmp_path, config):
    wds = FakeWatchdogs()
    r = Running(config, spawn_watchdog=wds)
    wait_for(lambda: read_state(state_path(tmp_path)))
    wds.created[0].exit_code = 5                              # the watchdog crashed
    r.thread.join(timeout=10)
    assert not r.thread.is_alive() and r.rc == [4]
    assert any("unprotected" in l for l in r.lines)
    assert not state_path(tmp_path).exists()


def test_a_watchdog_initiated_stop_is_reported_and_its_record_is_kept(tmp_path, config):
    r = Running(config)
    wait_for(lambda: read_state(state_path(tmp_path)))
    write_wd_state(tmp_path, status="ABORTING", reason=["ram_percent"])
    r.stop_event.set()                                        # the watchdog's SIGTERM
    r.thread.join(timeout=10)
    assert r.rc == [4] and any("stopped by the watchdog" in l and "ram_percent" in l
                               for l in r.lines)
    assert wd_state_path(tmp_path).exists()                    # evidence stays for status/doctor


def test_stop_of_a_stale_control_plane_also_removes_a_stale_watchdog_record(tmp_path, config):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text(json.dumps({"pid": dead.pid, "proc_start": "1"}))
    write_wd_state(tmp_path, pid=dead.pid, proc_start="1")
    assert stop(config, out=lambda l: None, kill=lambda *a: pytest.fail("no signal")) == 0
    assert not state_path(tmp_path).exists() and not wd_state_path(tmp_path).exists()


# --- status reports the watchdog ---------------------------------------------------------------

def watchdog_of(tmp_path, vllm_server_url):
    return status(make_config(tmp_path, url=vllm_server_url), run=System())["watchdog"]


def test_status_reports_no_watchdog_when_none_is_running(tmp_path, vllm_server_url):
    assert watchdog_of(tmp_path, vllm_server_url)["state"] == "not running"


def test_status_reports_an_armed_watchdog_with_its_budgets_and_last_sample(
        tmp_path, vllm_server_url):
    write_wd_state(tmp_path)
    w = watchdog_of(tmp_path, vllm_server_url)
    assert w["state"] == "armed" and w["pid"] == os.getpid() and w["protected_pid"] == os.getpid()
    assert w["limits"]["max_runtime_seconds"] == 600
    assert w["last_sample"]["ram_percent"] == 50.0 and w["last_check_age_seconds"] < 5


def test_status_reports_a_stalled_watchdog_when_its_heartbeat_stops(tmp_path, vllm_server_url):
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    write_wd_state(tmp_path, last_check_at=old)
    assert watchdog_of(tmp_path, vllm_server_url)["state"] == "stalled"


def test_status_reports_a_dead_watchdog_as_stale_not_armed(tmp_path, vllm_server_url):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_wd_state(tmp_path, pid=dead.pid, proc_start="1")
    assert watchdog_of(tmp_path, vllm_server_url)["state"] == "stale"


def test_status_reports_what_the_watchdog_aborted_and_why(tmp_path, vllm_server_url):
    write_wd_state(tmp_path, status="ABORTED", reason=["temperature_c"], action="SIGTERM",
                   exited=True, at=datetime.now(timezone.utc).isoformat())
    w = watchdog_of(tmp_path, vllm_server_url)
    assert w["state"] == "aborted" and w["reason"] == ["temperature_c"] and w["action"] == "SIGTERM"


def test_status_flags_an_unreadable_watchdog_record(tmp_path, vllm_server_url):
    wd_state_path(tmp_path).parent.mkdir(parents=True)
    wd_state_path(tmp_path).write_text("{ nope")
    assert watchdog_of(tmp_path, vllm_server_url)["state"] == "unknown"


def test_status_never_modifies_the_watchdog_record(tmp_path, vllm_server_url):
    write_wd_state(tmp_path, status="ABORTED", reason=["ram_percent"])
    before = wd_state_path(tmp_path).read_text()
    watchdog_of(tmp_path, vllm_server_url)
    assert wd_state_path(tmp_path).read_text() == before


def test_disarming_never_interferes_with_a_watchdog_that_is_already_aborting(tmp_path):
    """The deadlock the real GPU run exposed: the control plane's shutdown must not signal or
    kill a watchdog that is mid-abort (it is waiting for us to exit, and must record ABORTED)."""
    from aiops.profile import load_profile
    from test_profile import KUBERNETES

    cfg = write(tmp_path, KUBERNETES.replace("[safety]", "[safety]\nmax_runtime_seconds = 1")
                + "\n[watchdog]\ninterval = 0.1\nterm_grace_seconds = 3\n")
    profile = load_profile(cfg)
    # a protected process that ignores SIGTERM: the abort takes the full 3 s grace, then SIGKILL
    child = subprocess.Popen([sys.executable, "-c",
                              "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                              "print('r', flush=True); time.sleep(60)"], stdout=subprocess.PIPE)
    child.stdout.readline()
    path = tmp_path / "aiops.state.json.watchdog"
    handle = spawn_watchdog(profile, protected_pid=child.pid)
    try:
        assert handle.wait_armed(timeout=15)
        wait_for(lambda: json.loads(path.read_text())["status"] == "ABORTING", 15)
        started = time.monotonic()
        handle.disarm()                                    # what the control plane does on exit
        assert time.monotonic() - started < 1.5            # did not wait for a mid-abort watchdog
        assert handle.poll() is None                       # and did not kill or signal it
        assert child.wait(timeout=15) == -signal.SIGKILL   # the watchdog finished its job
        deadline = time.monotonic() + 10
        while handle.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert handle.poll() == 10                         # EXIT_ABORTED
        final = json.loads(path.read_text())
        assert final["status"] == "ABORTED" and final["action"] == "SIGKILL"
    finally:
        child.kill()
        child.wait()
