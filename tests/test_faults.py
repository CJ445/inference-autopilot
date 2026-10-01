"""The guarded fault injector: every way it could pause the wrong thing, or fail to resume, is a test."""
import threading

import pytest
from faults_support import Container, FakeClock, FakeDocker, cid, dead_reaper, live_reaper

from aiops.faults import core
from aiops.faults import reaper as reaper_module
from aiops.faults.core import AllowlistedDocker, FaultError, read_lease, recover, write_lease
from aiops.faults.injector import FaultInjector


@pytest.fixture
def rig(tmp_path):
    docker = FakeDocker()
    clock = FakeClock()
    lease = tmp_path / "state.json.fault.json"
    spawned = []

    def spawn(path):
        spawned.append(path)
        return live_reaper(path)

    inj = FaultInjector("vllm", lease, run=AllowlistedDocker("vllm", run=docker), spawn=spawn,
                        clock=clock, sleep=clock.sleep)
    rig = type("Rig", (), {})()
    rig.docker, rig.clock, rig.lease_path, rig.spawned, rig.inj = docker, clock, lease, spawned, inj
    rig.new_injector = lambda **kw: FaultInjector(
        "vllm", lease, run=AllowlistedDocker("vllm", run=docker), spawn=kw.get("spawn", spawn),
        clock=clock, sleep=clock.sleep)
    return rig


def inject(rig, duration=60):
    return rig.inj.inject("pause_workload", "vllm", duration)


def codes(fn):
    with pytest.raises(FaultError) as e:
        fn()
    return e.value.code, str(e.value)


# --- a fault, step by step ----------------------------------------------------------------------------

def test_a_fault_pauses_exactly_the_managed_workload_under_a_lease(rig):
    active = inject(rig, 60)
    assert rig.docker.main.paused
    lease = read_lease(rig.lease_path)
    assert lease["status"] == "ACTIVE" and lease["fault_type"] == "pause_workload"
    assert lease["workload"] == "vllm" and lease["container_id"] == cid(1)
    assert lease["expected_identity"] == f"{cid(1)}:2026-10-02T10:00:00Z"
    assert lease["expires_at"] == rig.clock.t + 60 and lease["created_at"]
    assert active["status"] == "ACTIVE" and rig.inj.summary()["active"]["fault_id"] == lease["fault_id"]
    pauses = [c for c in rig.docker.calls if c[1] == "pause"]
    assert pauses == [["docker", "pause", cid(1)]]                    # one pause, of the full id


def test_the_lease_and_the_safety_net_exist_before_anything_is_paused(rig):
    seen = {}

    def at_pause(docker):
        lease = read_lease(rig.lease_path)
        seen.update(status=lease["status"], reaper=lease.get("reaper"), expires=lease["expires_at"])
    rig.docker.before_pause = at_pause
    inject(rig, 60)
    assert seen["status"] == "ARMING" and seen["reaper"]["pid"] and seen["expires"] == rig.clock.t + 60


def test_the_identity_is_checked_again_immediately_before_the_pause(rig):
    def swap(docker):
        raise AssertionError("pause must not be reached")
    # the workload is restarted after the first check and before the second
    calls = {"n": 0}
    original = rig.docker.__call__

    def sneaky(argv, **kw):
        if argv[1] == "inspect":
            calls["n"] += 1
            if calls["n"] == 2:                                      # between the two checks
                rig.docker.restart(rig.docker.main, "2026-10-02T10:30:00Z")
        return original(argv, **kw)
    inj = FaultInjector("vllm", rig.lease_path, run=AllowlistedDocker("vllm", run=sneaky),
                        spawn=live_reaper, clock=rig.clock, sleep=rig.clock.sleep)
    code, _ = codes(lambda: inj.inject("pause_workload", "vllm", 60))
    assert code == "PRECONDITION_FAILED"
    assert "pause" not in rig.docker.verbs() and not rig.docker.main.paused
    assert read_lease(rig.lease_path)["status"] == "FAILED"


# --- what it refuses to touch ---------------------------------------------------------------------------

def test_a_container_without_the_project_labels_is_never_paused(rig):
    rig.docker.containers.clear()
    stranger = rig.docker.add(Container(cid(9), labelled=False))
    code, msg = codes(lambda: inject(rig))
    assert code == "PRECONDITION_FAILED" and "no managed workload" in msg
    assert not stranger.paused and "pause" not in rig.docker.verbs()


def test_a_container_labelled_for_another_workload_is_never_paused(rig):
    rig.docker.containers.clear()
    other = rig.docker.add(Container(cid(9), workload="someone-else"))
    assert codes(lambda: inject(rig))[0] == "PRECONDITION_FAILED"
    assert not other.paused and "pause" not in rig.docker.verbs()


def test_an_ambiguous_workload_is_refused(rig):
    rig.docker.add(Container(cid(2)))                                   # a second labelled container
    code, msg = codes(lambda: inject(rig))
    assert code == "PRECONDITION_FAILED" and "ambiguous" in msg
    assert not any(c.paused for c in rig.docker.containers.values())


def test_a_ps_inspect_identity_mismatch_is_refused(rig):
    rig.docker.inspect_id_override = cid(7)
    code, msg = codes(lambda: inject(rig))
    assert code == "PRECONDITION_FAILED" and "identity" in msg and "pause" not in rig.docker.verbs()


def test_a_stopped_or_already_paused_workload_is_refused(rig):
    rig.docker.main.running = False
    assert "not running" in codes(lambda: inject(rig))[1]
    rig.docker.main.running, rig.docker.main.paused = True, True
    assert "already paused" in codes(lambda: inject(rig))[1]
    assert "pause" not in rig.docker.verbs()


@pytest.mark.parametrize("args, fragment", [
    (("kill_workload", "vllm", 60), "unknown fault type"),
    (("pause_workload", "other", 60), "not the configured workload"),
    (("pause_workload", "vllm", 14), "from 15 to 300"),
    (("pause_workload", "vllm", 301), "from 15 to 300"),
    (("pause_workload", "vllm", "60"), "from 15 to 300"),
    (("pause_workload", "vllm", True), "from 15 to 300"),
    (("pause_workload", "vllm", None), "from 15 to 300"),
    ((None, "vllm", 60), "unknown fault type"),
])
def test_unknown_types_other_targets_and_unsafe_durations_are_refused(rig, args, fragment):
    code, msg = codes(lambda: rig.inj.inject(*args))
    assert code == "INVALID_REQUEST" and fragment in msg
    assert rig.docker.calls == [] and read_lease(rig.lease_path) is None     # nothing even looked


def test_a_second_fault_is_refused_while_one_is_active_and_allowed_after(rig):
    inject(rig)
    assert codes(lambda: inject(rig))[0] == "FAULT_ACTIVE"
    rig.inj.cancel()
    inject(rig)                                                             # allowed again
    assert rig.docker.main.paused


def test_an_unreadable_lease_fails_closed(rig):
    rig.lease_path.write_text("{ not json")
    assert codes(lambda: inject(rig))[0] == "LEASE_UNREADABLE"
    assert rig.docker.calls == []


# --- ending a fault -----------------------------------------------------------------------------------------

def test_the_reaper_resumes_the_workload_at_the_deadline_with_no_injector_alive(rig):
    inject(rig, 30)
    del rig.inj                                                            # the injector is gone (a crash)
    assert rig.docker.main.paused
    outcome = reaper_module.run(rig.lease_path, docker_run=AllowlistedDocker("vllm", run=rig.docker),
                                clock=rig.clock, sleep=rig.clock.sleep)
    assert outcome == "RECOVERED" and not rig.docker.main.paused
    lease = read_lease(rig.lease_path)
    assert lease["status"] == "RECOVERED" and lease["recovery"]["by"] == "reaper"
    assert lease["recovery"]["action"] == "unpaused"
    assert rig.clock.t >= lease["expires_at"]                              # not a second early


def test_the_reaper_does_nothing_before_the_deadline_and_stands_down_when_the_fault_ends(rig):
    inject(rig, 30)
    ticks = {"n": 0}

    def sleep(seconds):
        ticks["n"] += 1
        rig.clock.sleep(1)
        if ticks["n"] == 5:
            rig.inj.cancel()                                               # the operator resumes it
    outcome = reaper_module.run(rig.lease_path, docker_run=AllowlistedDocker("vllm", run=rig.docker),
                                clock=rig.clock, sleep=sleep)
    assert outcome == "stood_down" and not rig.docker.main.paused
    assert ticks["n"] == 5 and read_lease(rig.lease_path)["recovery"]["by"] == "operator"


def test_a_restarted_control_plane_resumes_an_expired_fault(rig):
    inject(rig, 30)
    fresh = rig.new_injector()                                             # a new process, same lease file
    fresh.sweep()
    assert rig.docker.main.paused                                          # not expired yet: left alone
    rig.clock.advance(31)
    fresh.sweep()
    assert not rig.docker.main.paused
    assert read_lease(rig.lease_path)["recovery"]["by"] == "control-plane"


def test_the_monitor_resumes_at_the_deadline_and_replaces_a_dead_reaper(rig):
    inject(rig, 30)
    # the reaper died: the monitor puts a new one in place
    lease = read_lease(rig.lease_path)
    write_lease(rig.lease_path, {**lease, "reaper": {"pid": 2 ** 22 + 1, "proc_start": "0"}})
    before = len(rig.spawned)
    rig.inj.tick()
    assert len(rig.spawned) == before + 1 and rig.docker.main.paused
    rig.clock.advance(31)
    rig.inj.tick()
    assert not rig.docker.main.paused and read_lease(rig.lease_path)["status"] == "RECOVERED"


def test_cancelling_resumes_now_and_a_second_cancel_has_nothing_to_do(rig):
    inject(rig, 60)
    ended = rig.inj.cancel()
    assert ended["status"] == "RECOVERED" and not rig.docker.main.paused
    assert ended["recovery"]["by"] == "operator"
    assert codes(rig.inj.cancel)[0] == "PRECONDITION_FAILED"


def test_a_graceful_control_plane_stop_ends_the_fault(rig):
    inject(rig, 60)
    rig.inj.shutdown()
    assert not rig.docker.main.paused
    assert read_lease(rig.lease_path)["recovery"]["by"] == "control-plane-stop"


# --- recovery never touches a different or changed workload ------------------------------------------------------

def test_a_workload_restarted_while_the_fault_was_active_is_not_touched(rig):
    inject(rig, 60)
    rig.docker.restart(rig.docker.main, "2026-10-02T10:20:00Z")           # e.g. an approved remediation
    rig.docker.calls.clear()
    assert recover(rig.lease_path, AllowlistedDocker("vllm", run=rig.docker), by="x") == "IDENTITY_CHANGED"
    assert "unpause" not in rig.docker.verbs()


def test_a_workload_replaced_while_the_fault_was_active_is_not_touched_even_if_paused(rig):
    inject(rig, 60)
    replacement = rig.docker.replace(cid(5))
    replacement.paused = True                                              # someone else paused the new one
    rig.docker.calls.clear()
    assert recover(rig.lease_path, AllowlistedDocker("vllm", run=rig.docker), by="x") == "IDENTITY_CHANGED"
    assert replacement.paused and "unpause" not in rig.docker.verbs()      # not ours to resume


def test_an_unpaused_or_vanished_workload_has_nothing_to_undo(rig):
    inject(rig, 60)
    rig.docker.main.paused = False                                         # the operator resumed it
    rig.docker.calls.clear()
    assert recover(rig.lease_path, AllowlistedDocker("vllm", run=rig.docker), by="x") == "RECOVERED"
    assert read_lease(rig.lease_path)["recovery"]["action"] == "already_running"
    assert "unpause" not in rig.docker.verbs()
    inject(rig, 60)
    rig.docker.containers.clear()
    assert recover(rig.lease_path, AllowlistedDocker("vllm", run=rig.docker), by="x") == "RECOVERED"
    assert read_lease(rig.lease_path)["recovery"]["action"] == "nothing_to_undo"


def test_recovery_that_cannot_identify_the_workload_refuses_to_act(rig):
    inject(rig, 60)
    rig.docker.add(Container(cid(2)))                                      # now ambiguous
    rig.docker.calls.clear()
    assert recover(rig.lease_path, AllowlistedDocker("vllm", run=rig.docker), by="x") == "RECOVERY_REFUSED"
    assert "unpause" not in rig.docker.verbs() and rig.docker.main.paused


def test_concurrent_recoveries_unpause_exactly_once(rig):
    inject(rig, 60)
    rig.docker.unpause_delay = 0.05      # a slow unpause: without the lock, two recoveries overlap
    run = AllowlistedDocker("vllm", run=rig.docker)
    threads = [threading.Thread(target=recover, args=(rig.lease_path, run, f"t{n}")) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert rig.docker.verbs().count("unpause") == 1 and not rig.docker.main.paused


# --- crash safety of the injection itself --------------------------------------------------------------------------

def test_nothing_is_paused_if_the_safety_net_cannot_be_armed(rig, monkeypatch):
    monkeypatch.setattr("aiops.faults.injector.ARM_TIMEOUT_SECONDS", 1)
    inj = FaultInjector("vllm", rig.lease_path, run=AllowlistedDocker("vllm", run=rig.docker),
                        spawn=dead_reaper, clock=rig.clock, sleep=rig.clock.sleep)
    code, msg = codes(lambda: inj.inject("pause_workload", "vllm", 60))
    assert code == "PRECONDITION_FAILED" and "did not arm" in msg
    assert "pause" not in rig.docker.verbs() and not rig.docker.main.paused
    assert read_lease(rig.lease_path)["status"] == "FAILED"


def test_a_failed_pause_leaves_nothing_paused_and_a_failed_lease(rig):
    rig.docker.fail_pause = True
    assert codes(lambda: inject(rig))[0] == "PRECONDITION_FAILED"
    assert not rig.docker.main.paused and read_lease(rig.lease_path)["status"] == "FAILED"


def test_a_pause_that_took_effect_but_reported_failure_is_undone(rig):
    rig.docker.pause_then_fail = True                                      # paused, then the CLI timed out
    assert codes(lambda: inject(rig))[0] == "PRECONDITION_FAILED"
    assert not rig.docker.main.paused                                      # the abort resumed it
    assert read_lease(rig.lease_path)["status"] == "FAILED"


# --- the injector can run nothing else ---------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["docker", "exec", cid(1), "sh"], ["docker", "rm", "-f", cid(1)], ["docker", "kill", cid(1)],
    ["docker", "restart", cid(1)], ["docker", "run", "alpine"], ["docker", "stop", cid(1)],
    ["docker", "pause", "vllm"], ["docker", "pause", cid(1)[:12]], ["docker", "pause", cid(1), "x"],
    ["docker", "pause", "--all"], ["docker", "unpause", "vllm"], ["docker", "unpause", f"{cid(1)};reboot"],
    ["docker", "inspect", "--format", "{{.Id}}", cid(1)], ["docker", "inspect", "$(reboot)"],
    ["docker", "ps"], ["docker", "ps", "-a"], ["docker", "ps", "-a", "--filter", "label=x", "--format", "{{.ID}}"],
    ["docker", "cp", "a", "b"], ["docker"], ["sh", "-c", "docker pause x"], ["kill", "-9", "1"], [],
])
def test_the_injector_runs_only_the_four_allowed_shapes(argv):
    docker = FakeDocker()
    guarded = AllowlistedDocker("vllm", run=docker)
    with pytest.raises(FaultError) as e:
        guarded(argv)
    assert e.value.code == "REFUSED_OPERATION" and docker.calls == []        # refused BEFORE any process


def test_the_allowed_shapes_are_exactly_these_four():
    docker = FakeDocker()
    g = AllowlistedDocker("vllm", run=docker)
    assert g.allowed(["docker", "ps", "-a", "--filter", "label=com.inference-autopilot.managed=true",
                      "--filter", "label=com.inference-autopilot.workload=vllm", "--format", "{{.ID}}"])
    assert g.allowed(["docker", "inspect", cid(1)[:12]]) and g.allowed(["docker", "inspect", cid(1)])
    assert g.allowed(["docker", "pause", cid(1)]) and g.allowed(["docker", "unpause", cid(1)])
    # a different workload's label is not this injector's ps
    assert not AllowlistedDocker("other", run=docker).allowed(
        ["docker", "ps", "-a", "--filter", "label=com.inference-autopilot.managed=true",
         "--filter", "label=com.inference-autopilot.workload=vllm", "--format", "{{.ID}}"])


def test_an_injector_attempt_to_use_a_verb_outside_the_allowlist_never_reaches_docker(rig):
    inj = rig.inj
    with pytest.raises(FaultError):
        inj._run(["docker", "exec", cid(1), "id"])
    with pytest.raises(FaultError):
        inj._run(["docker", "restart", cid(1)])                            # remediation's job, not ours
    assert rig.docker.calls == []


def test_the_fault_core_has_no_other_way_to_start_a_process():
    import ast
    from pathlib import Path
    for name in ("core.py", "injector.py", "reaper.py"):
        tree = ast.parse((Path(core.__file__).parent / name).read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        spawners = {ast.unparse(c.func) for c in calls
                    if ast.unparse(c.func) in ("subprocess.run", "subprocess.Popen", "os.system", "os.popen",
                                               "subprocess.call", "subprocess.check_output", "os.execv")}
        # the only process starts: the allowlisted runner's default (core) and the reaper spawn (injector)
        assert spawners <= {"subprocess.Popen"} if name == "injector.py" else not spawners - {"subprocess.run"}
