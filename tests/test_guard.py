"""The independent control-plane watchdog: identity, budgets, fail-closed, process, isolation.

Everything here uses real sacrificial child processes; no hardware is needed (the kubernetes-style
config exercises the real host-RAM and runtime budgets through /proc)."""
import ast
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_profile import DOCKER, KUBERNETES, write

from aiops.profile import load_profile
from aiops.watchdog import (EXIT_ABORTED, EXIT_CONFIG, EXIT_IDENTITY, EXIT_SENSORS, ConfigError,
                            IdentityError, Protected, boot_id, evaluate, guard, identity_of,
                            load_config, proc_start)

ROOT = Path(__file__).parent.parent
WATCHDOG_FILE = ROOT / "aiops" / "watchdog.py"
LIMITS = {"max_gpu_memory_percent": 85, "max_temperature_c": 80, "max_ram_percent": 90,
          "max_runtime_seconds": 100}
SAFE = {"gpu_memory_percent": 35.0, "temperature_c": 55.0, "ram_percent": 50.0,
        "gpu_percent": 0.0}


@pytest.fixture
def procs():
    started = []

    def make(ignore_term=False, seconds=60):
        code = ("import signal, time\n"
                + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
                + f"print('ready', flush=True)\ntime.sleep({seconds})\n")
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "ready"
        started.append(p)
        return p

    yield make
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait()


# --- identity ---------------------------------------------------------------------------------

def test_a_live_process_has_an_identity_and_can_be_protected(procs):
    p = procs()
    ident = identity_of(p.pid)
    assert ident == {"pid": p.pid, "start": proc_start(p.pid), "boot_id": boot_id()}
    protected = Protected(ident)
    assert protected.alive() is True
    p.kill()
    p.wait()
    assert protected.alive() is False
    protected.close()


def test_a_wrong_start_time_is_a_reused_pid_and_is_refused(procs):
    p = procs()
    ident = identity_of(p.pid)
    with pytest.raises(IdentityError, match="identity"):
        Protected({**ident, "start": str(int(ident["start"]) + 1)})


def test_a_stale_pid_whose_process_is_gone_is_refused(procs):
    p = procs()
    ident = identity_of(p.pid)
    p.kill()
    p.wait()
    with pytest.raises(IdentityError):
        Protected(ident)
    with pytest.raises(IdentityError):
        identity_of(p.pid)


def test_a_process_that_exited_but_is_not_yet_reaped_is_refused(procs):
    p = subprocess.Popen([sys.executable, "-c", "print('x')"], stdout=subprocess.PIPE)
    ident = identity_of(p.pid)
    p.stdout.read()
    deadline = time.monotonic() + 5
    while Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z":
        assert time.monotonic() < deadline
        time.sleep(0.02)                                   # exited, zombie until we wait()
    with pytest.raises(IdentityError):
        Protected(ident)
    p.wait()


def test_a_missing_process_is_refused():
    with pytest.raises(IdentityError):
        identity_of(4_194_000)


@pytest.mark.parametrize("pid", [0, -1, 1, "123", None, True, 2**30, 3.5])
def test_invalid_pids_are_refused(pid):
    with pytest.raises(IdentityError):
        Protected({"pid": pid, "start": "1", "boot_id": boot_id()})
    if isinstance(pid, int) and not isinstance(pid, bool):
        with pytest.raises(IdentityError):
            identity_of(pid)


def test_the_watchdog_will_not_protect_itself():
    with pytest.raises(IdentityError, match="itself"):
        Protected(identity_of(os.getpid()))


def test_a_different_boot_is_refused(procs):
    p = procs()
    with pytest.raises(IdentityError, match="boot"):
        Protected({**identity_of(p.pid), "boot_id": "some-other-boot"})


@pytest.mark.parametrize("ident", [{}, {"pid": 5}, {"pid": 5, "start": "1"}, None, "x",
                                   {"pid": 5, "start": 1, "boot_id": "b"}])
def test_malformed_identities_are_refused(ident):
    with pytest.raises(IdentityError):
        Protected(ident)


def test_signalling_an_already_exited_process_is_harmless(procs):
    p = procs()
    protected = Protected(identity_of(p.pid))
    p.kill()
    p.wait()
    assert protected.signal(signal.SIGTERM) is False       # no error, nothing else signalled


# --- budgets ----------------------------------------------------------------------------------

def test_a_reading_below_every_budget_is_clean():
    assert evaluate(SAFE, LIMITS, runtime=10) == []


def test_exactly_at_a_budget_is_allowed():
    at = {**SAFE, "gpu_memory_percent": 85, "temperature_c": 80, "ram_percent": 90}
    assert evaluate(at, LIMITS, runtime=100) == []


@pytest.mark.parametrize("override, runtime, expected", [
    ({"gpu_memory_percent": 85.1}, 1, ["gpu_memory_percent"]),
    ({"temperature_c": 80.1}, 1, ["temperature_c"]),
    ({"ram_percent": 90.1}, 1, ["ram_percent"]),
    ({}, 100.1, ["runtime_seconds"]),
])
def test_each_budget_is_enforced_on_its_own(override, runtime, expected):
    assert evaluate({**SAFE, **override}, LIMITS, runtime) == expected


def test_simultaneous_violations_are_all_reported():
    hot = {**SAFE, "gpu_memory_percent": 99, "temperature_c": 99, "ram_percent": 99}
    assert sorted(evaluate(hot, LIMITS, runtime=500)) == [
        "gpu_memory_percent", "ram_percent", "runtime_seconds", "temperature_c"]


def test_the_runtime_budget_is_enforced_even_without_any_sensor_reading():
    assert evaluate(None, LIMITS, runtime=101) == ["runtime_seconds"]
    assert evaluate(None, LIMITS, runtime=1) == []


# --- the guard loop: deterministic, fail closed ------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Spy(Protected):
    signals = []

    def signal(self, sig):
        Spy.signals.append((self.pid, sig))
        return super().signal(sig)


def run_guard(p, sampler, limits=LIMITS, cycles=None, grace=2, max_failures=3, on_cycle=None,
              protected_cls=Spy):
    Spy.signals = []
    clock = Clock()
    seen = {"n": 0}

    def stop():
        seen["n"] += 1
        return cycles is not None and seen["n"] > cycles

    return guard(protected_cls(identity_of(p.pid)), limits, sampler, interval=1.0, grace=grace,
                 max_sensor_failures=max_failures, on_cycle=on_cycle, stop=stop, sleep=clock.sleep,
                 clock=clock)


def test_a_healthy_run_signals_nothing_and_leaves_the_process_alone(procs):
    p = procs()
    r = run_guard(p, lambda: dict(SAFE), cycles=20)
    assert r["outcome"] == "DISARMED" and Spy.signals == [] and p.poll() is None


@pytest.mark.parametrize("override, reason", [
    ({"gpu_memory_percent": 86}, ["gpu_memory_percent"]),
    ({"temperature_c": 81}, ["temperature_c"]),
    ({"ram_percent": 91}, ["ram_percent"]),
])
def test_a_budget_violation_terminates_only_the_protected_process(procs, override, reason):
    p, bystander = procs(), procs()
    r = run_guard(p, lambda: {**SAFE, **override})
    assert (r["outcome"], r["reason"]) == ("ABORTED", reason)
    assert r["action"] == "SIGTERM" and r["exited"] is True
    assert p.wait(timeout=5) == -signal.SIGTERM
    assert bystander.poll() is None                         # nothing unrelated is touched
    assert Spy.signals == [(p.pid, signal.SIGTERM)]


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace_period(procs):
    p = procs(ignore_term=True)
    r = run_guard(p, lambda: {**SAFE, "ram_percent": 99}, grace=0.4)
    assert r["outcome"] == "ABORTED" and r["action"] == "SIGKILL" and r["exited"] is True
    assert p.wait(timeout=5) == -signal.SIGKILL
    assert [s for _, s in Spy.signals] == [signal.SIGTERM, signal.SIGKILL]


def test_the_runtime_budget_terminates_the_protected_process(procs):
    p = procs()
    r = run_guard(p, lambda: dict(SAFE), limits={**LIMITS, "max_runtime_seconds": 5})
    assert (r["outcome"], r["reason"]) == ("ABORTED", ["runtime_seconds"])
    assert p.wait(timeout=5) == -signal.SIGTERM


def test_unavailable_telemetry_fails_closed_after_the_configured_tolerance(procs):
    p = procs()
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise OSError("nvidia-smi: command not found")

    r = run_guard(p, broken, max_failures=3)
    assert (r["outcome"], r["reason"]) == ("ABORTED", ["sensor_failed"])
    assert calls["n"] == 3                                  # exactly the tolerance, then act
    assert p.wait(timeout=5) == -signal.SIGTERM


def test_a_single_failed_sample_is_tolerated_and_recovery_resets_the_count(procs):
    p = procs()
    script = iter([OSError("blip"), dict(SAFE), OSError("blip"), dict(SAFE), OSError("blip"),
                   dict(SAFE)])

    def flaky():
        item = next(script)
        if isinstance(item, Exception):
            raise item
        return item

    r = run_guard(p, flaky, cycles=6, max_failures=2)
    assert r["outcome"] == "DISARMED" and p.poll() is None


@pytest.mark.parametrize("bad", [None, "garbage", 42, {}, {"gpu_memory_percent": 1}])
def test_malformed_telemetry_counts_as_a_failure_never_as_a_pass(procs, bad):
    p = procs()
    r = run_guard(p, lambda: bad, max_failures=2)
    assert (r["outcome"], r["reason"]) == ("ABORTED", ["sensor_failed"])


def test_with_one_tolerated_failure_the_first_bad_sample_acts(procs):
    p = procs()
    r = run_guard(p, lambda: (_ for _ in ()).throw(ValueError("[N/A]")), max_failures=1)
    assert r["reason"] == ["sensor_failed"]


def test_runtime_is_still_enforced_while_every_sensor_is_failing(procs):
    p = procs()

    def broken():
        raise OSError("no gpu")

    r = run_guard(p, broken, limits={**LIMITS, "max_runtime_seconds": 3}, max_failures=100)
    assert r["reason"] == ["runtime_seconds"]


def test_a_watchdog_exception_fails_closed(procs):
    p = procs()

    def boom(sample, failures, error):
        raise RuntimeError("heartbeat write failed")

    r = run_guard(p, lambda: dict(SAFE), on_cycle=boom)
    assert (r["outcome"], r["reason"]) == ("ABORTED", ["watchdog_error"])
    assert "heartbeat write failed" in r["error"] and p.wait(timeout=5) == -signal.SIGTERM


def test_a_protected_process_that_disappears_ends_the_guard_without_signalling_anything(procs):
    q = procs()
    protected = Spy(identity_of(q.pid))
    q.kill()
    q.wait()
    Spy.signals = []
    r = guard(protected, LIMITS, lambda: dict(SAFE), interval=1, grace=1, max_sensor_failures=3,
              stop=lambda: False, sleep=lambda s: None, clock=Clock())
    assert r == {"outcome": "PROTECTED_EXITED"} and Spy.signals == []


def test_limits_cannot_be_changed_once_the_guard_is_armed(procs):
    p = procs()
    limits = dict(LIMITS)

    def sneaky():
        limits["max_ram_percent"] = 1000                    # the caller tries to loosen the budget
        return {**SAFE, "ram_percent": 95}

    r = run_guard(p, sneaky, limits=limits)
    assert r["reason"] == ["ram_percent"]                   # the armed copy still applies


def test_the_cycle_callback_sees_every_sample_and_failure_count(procs):
    p = procs()
    seen = []
    run_guard(p, lambda: dict(SAFE), cycles=3, on_cycle=lambda s, f, e: seen.append((s, f, e)))
    assert len(seen) == 3 and all(f == 0 and e is None and s["ram_percent"] == 50 for s, f, e in seen)


# --- the real watchdog process -----------------------------------------------------------------

def watchdog_config(tmp_path, safety="", watchdog="interval = 0.1\nterm_grace_seconds = 1\n",
                    base=KUBERNETES):
    text = base.replace("[safety]", "[safety]\n" + safety) + "\n[watchdog]\n" + watchdog
    text = text.replace('state_file = "run/aiops.state.json"\n', "")   # default: next to the config
    return write(tmp_path, text)


def spawn_watchdog(cfg, child, ident=None, env=None, extra=()):
    ident = ident or identity_of(child.pid)
    return subprocess.Popen(
        [sys.executable, "-I", str(WATCHDOG_FILE), "--config", str(cfg), "--pid", str(ident["pid"]),
         "--proc-start", str(ident["start"]), "--boot-id", ident["boot_id"], *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def state_file(tmp_path):
    return tmp_path / "aiops.state.json.watchdog"


def read_json(path):
    return json.loads(path.read_text())


def wait_until(fn, timeout=10):
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


def test_the_watchdog_arms_publishes_a_heartbeat_and_disarms_without_touching_the_process(
        tmp_path, procs):
    child = procs()
    cfg = watchdog_config(tmp_path, "max_runtime_seconds = 600\n")
    wd = spawn_watchdog(cfg, child)
    try:
        st = wait_until(lambda: (lambda s: s if s["status"] == "ARMED" else None)(
            read_json(state_file(tmp_path))))
        assert st["pid"] == wd.pid and st["protected"] == identity_of(child.pid)
        assert st["limits"]["max_runtime_seconds"] == 600 and "max_ram_percent" in st["limits"]
        first = st["last_check_at"]
        wait_until(lambda: read_json(state_file(tmp_path))["last_check_at"] != first)  # heartbeat
        assert 0 < read_json(state_file(tmp_path))["last_sample"]["ram_percent"] < 100
    finally:
        wd.send_signal(signal.SIGTERM)
    assert wd.wait(timeout=10) == 0
    assert not state_file(tmp_path).exists() and child.poll() is None   # disarmed, process alive


def test_a_runtime_budget_violation_terminates_the_protected_process_and_records_it(
        tmp_path, procs):
    child, bystander = procs(), procs()
    wd = spawn_watchdog(watchdog_config(tmp_path, "max_runtime_seconds = 1\n"), child)
    assert wd.wait(timeout=15) == EXIT_ABORTED
    assert child.wait(timeout=5) == -signal.SIGTERM and bystander.poll() is None
    final = read_json(state_file(tmp_path))
    assert final["status"] == "ABORTED" and final["reason"] == ["runtime_seconds"]
    assert final["action"] == "SIGTERM" and final["exited"] is True and final["at"]


def test_the_real_host_ram_budget_is_enforced_through_proc_meminfo(tmp_path, procs):
    child = procs()
    wd = spawn_watchdog(watchdog_config(tmp_path, "max_ram_percent = 1\n"), child)
    assert wd.wait(timeout=15) == EXIT_ABORTED
    assert read_json(state_file(tmp_path))["reason"] == ["ram_percent"]
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_a_process_that_ignores_sigterm_is_killed_by_the_real_watchdog(tmp_path, procs):
    child = procs(ignore_term=True)
    wd = spawn_watchdog(watchdog_config(tmp_path, "max_runtime_seconds = 1\n"), child)
    assert wd.wait(timeout=20) == EXIT_ABORTED
    assert child.wait(timeout=5) == -signal.SIGKILL
    assert read_json(state_file(tmp_path))["action"] == "SIGKILL"


def test_the_watchdog_exits_quietly_when_the_protected_process_ends_by_itself(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    wd = spawn_watchdog(watchdog_config(tmp_path), child)
    assert wd.wait(timeout=15) == 0
    assert not state_file(tmp_path).exists()
    child.wait()


def test_an_invalid_configuration_refuses_to_arm_and_touches_nothing(tmp_path, procs):
    child = procs()
    bad = write(tmp_path, 'profile = "fake-gpu"\n')
    wd = spawn_watchdog(bad, child)
    assert wd.wait(timeout=10) == EXIT_CONFIG
    assert not state_file(tmp_path).exists() and child.poll() is None


def test_an_identity_that_cannot_be_established_refuses_to_arm_and_touches_nothing(
        tmp_path, procs):
    child = procs()
    ident = {**identity_of(child.pid), "start": "1"}                   # wrong start time
    wd = spawn_watchdog(watchdog_config(tmp_path), child, ident=ident)
    assert wd.wait(timeout=10) == EXIT_IDENTITY
    assert "identity" in wd.stderr.read() and not state_file(tmp_path).exists()
    assert child.poll() is None


def test_a_stale_pid_refuses_to_arm(tmp_path, procs):
    child = procs()
    ident = identity_of(child.pid)
    child.kill()
    child.wait()
    wd = spawn_watchdog(watchdog_config(tmp_path), child, ident=ident)
    assert wd.wait(timeout=10) == EXIT_IDENTITY and not state_file(tmp_path).exists()


def test_unreadable_gpu_sensors_refuse_to_arm_rather_than_pretend_to_protect(tmp_path, procs):
    child = procs()
    cfg = watchdog_config(tmp_path, base=DOCKER)
    wd = spawn_watchdog(cfg, child, env={"PATH": ""})                  # no nvidia-smi anywhere
    assert wd.wait(timeout=15) == EXIT_SENSORS
    assert "refusing to arm" in wd.stderr.read() and not state_file(tmp_path).exists()
    assert child.poll() is None


def test_the_watchdog_has_no_command_interface():
    for extra in (["--command", "reboot"], ["restart"], ["--exec", "x"], ["--action", "restart"]):
        r = subprocess.run([sys.executable, "-I", str(WATCHDOG_FILE), "--config", "x", "--pid",
                            "5", "--proc-start", "1", "--boot-id", "b", *extra],
                           capture_output=True, text=True)
        assert r.returncode == 2, extra


# --- configuration is read from the operator's file, never from the engine ----------------------

@pytest.mark.parametrize("text", [DOCKER, KUBERNETES])
def test_the_watchdogs_own_config_reader_agrees_with_the_profile_loader(tmp_path, text):
    cfg = write(tmp_path, text.replace("[safety]", "[safety]\nmax_runtime_seconds = 777")
                + "\n[watchdog]\ninterval = 0.25\nterm_grace_seconds = 7\nmax_sensor_failures = 2\n")
    profile, conf = load_profile(cfg), load_config(cfg)
    assert conf["interval"] == 0.25 and conf["grace"] == 7 and conf["max_sensor_failures"] == 2
    assert conf["state_path"] == profile["control_plane"]["state_file"] + ".watchdog"
    s = profile["safety"]
    assert conf["limits"]["max_runtime_seconds"] == s["max_runtime_seconds"] == 777
    assert conf["limits"]["max_ram_percent"] == s["max_ram_percent"]
    if profile["provider"] == "docker":
        assert conf["gpu_index"] == profile["gpu"]["index"]
        assert conf["limits"]["max_gpu_memory_percent"] == s["max_gpu_memory_percent"]
        assert conf["limits"]["max_temperature_c"] == s["max_temperature_c"]
    else:
        assert conf["gpu_index"] is None and "max_gpu_memory_percent" not in conf["limits"]


@pytest.mark.parametrize("text", [
    'profile = "fake"\n', "not = = toml", DOCKER.replace("[safety]", "[safety]\nmax_runtime_seconds = 0"),
    DOCKER.replace("[safety]", "[safety]\nmax_ram_percent = 101"),
    DOCKER + "\n[watchdog]\ninterval = -1\n", DOCKER + "\n[watchdog]\nmax_sensor_failures = 0\n",
])
def test_the_watchdogs_config_reader_rejects_invalid_configuration(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


# --- isolation: what the watchdog can and cannot do ---------------------------------------------

FORBIDDEN_MODULES = ["aiops.engine", "aiops.rca", "aiops.detector", "aiops.incident",
                     "aiops.remediate", "aiops.policy", "aiops.store", "aiops.docker",
                     "aiops.kubectl", "aiops.prometheus", "aiops.vllm", "aiops.telemetry",
                     "aiops.verify", "aiops.api", "aiops.lifecycle", "aiops.profile",
                     "aiops.doctor", "aiops.runtime", "aiops.serve", "aiops.supervise"]


def test_importing_the_watchdog_loads_none_of_the_engine_or_control_plane():
    code = ("import sys; import aiops.watchdog; "
            f"bad = [m for m in {FORBIDDEN_MODULES!r} if m in sys.modules]; "
            "print(','.join(bad) or 'clean')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert r.stdout.strip() == "clean", r.stdout + r.stderr


def test_the_watchdog_runs_isolated_with_no_aiops_package_available():
    r = subprocess.run([sys.executable, "-I", str(WATCHDOG_FILE), "--help"],
                       capture_output=True, text=True, cwd="/")
    assert r.returncode == 0 and "--pid" in r.stdout and "--command" not in r.stdout


def test_the_watchdog_source_can_neither_run_commands_nor_import_aiops():
    tree = ast.parse(WATCHDOG_FILE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("aiops"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("aiops") for a in node.names)
    allowed_function = "read_sensors"             # the one nvidia-smi read
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "subprocess" and node.attr in (
                        "run", "Popen", "call", "check_call", "check_output"):
                assert fn.name == allowed_function, f"subprocess.{node.attr} in {fn.name}()"
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
            {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for forbidden in ("system", "popen", "execv", "execvp", "execl", "spawn", "eval", "exec",
                      "Popen", "killpg", "setsid"):
        assert forbidden not in names, forbidden
    smi = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "nvidia-smi"]
    assert smi, "the GPU sensor must be the independent nvidia-smi path"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
                and n.value in ("docker", "kubectl")]


def test_the_only_signals_the_watchdog_can_send_are_term_and_kill():
    tree = ast.parse(WATCHDOG_FILE.read_text())
    sent = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "signal"
            and n.attr.startswith("SIG")}
    assert sent <= {"SIGTERM", "SIGKILL", "SIGINT", "SIGHUP", "SIG_IGN"}


def test_the_watchdog_makes_exactly_two_system_calls_pidfd_open_and_pidfd_send_signal():
    tree = ast.parse(WATCHDOG_FILE.read_text())
    consts = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and t.id.startswith("SYS_")
              and isinstance(n.value, ast.Constant)}
    assert consts == {"SYS_PIDFD_OPEN": 434, "SYS_PIDFD_SEND_SIGNAL": 424}
    callers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
               for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == "syscall"}
    assert callers == {"_pidfd_open", "_pidfd_send_signal"}


def test_the_abort_is_recorded_before_the_process_is_signalled(procs):
    p = procs()
    seen = []

    def on_abort(reason, sample):
        seen.append((reason, sample["ram_percent"], p.poll()))   # the process is still running

    Spy.signals = []
    clock = Clock()
    r = guard(Spy(identity_of(p.pid)), LIMITS, lambda: {**SAFE, "ram_percent": 95}, interval=1,
              grace=2, max_sensor_failures=3, on_abort=on_abort, sleep=clock.sleep, clock=clock)
    assert r["outcome"] == "ABORTED"
    assert seen == [(["ram_percent"], 95, None)] and Spy.signals[0][1] == signal.SIGTERM


def test_a_failing_abort_hook_never_prevents_the_termination(procs):
    p = procs()

    def broken(reason, sample):
        raise OSError("disk full")

    clock = Clock()
    r = guard(Spy(identity_of(p.pid)), LIMITS, lambda: {**SAFE, "ram_percent": 95}, interval=1,
              grace=2, max_sensor_failures=3, on_abort=broken, sleep=clock.sleep, clock=clock)
    assert r["outcome"] == "ABORTED" and p.wait(timeout=5) == -signal.SIGTERM


# --- the other direction of the boundary: the engine cannot reach the watchdog ----------------------

def test_only_lifecycle_doctor_and_the_stressor_launcher_import_the_watchdog():
    """Engine, RCA, detector, policy, remediation, verification, API, providers and telemetry
    have no path to the watchdog: they cannot configure, pause or influence it."""
    importers = set()
    for path in (ROOT / "aiops").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n == "aiops.watchdog" or n.startswith("aiops.watchdog.") for n in names) or (
                    isinstance(node, ast.ImportFrom) and node.module == "aiops"
                    and any(a.name == "watchdog" for a in node.names)):
                importers.add(path.name)
    assert importers == {"lifecycle.py", "doctor.py", "gpu_fault.py", "supervise.py"}


def test_the_api_exposes_nothing_that_could_control_the_watchdog():
    import threading
    import urllib.error
    import urllib.request

    from test_engine import CONFIG, World

    from aiops.api import make_server
    from aiops.engine import Engine

    w = World()
    engine = Engine(w, w, CONFIG)
    server = make_server(engine, port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for method in ("GET", "POST", "PUT", "DELETE"):
            for path in ("/api/v1/watchdog", "/api/v1/watchdog/limits", "/api/v1/safety",
                         "/api/v1/status/watchdog"):
                req = urllib.request.Request(base + path, method=method,
                                             data=b"{}" if method != "GET" else None)
                with pytest.raises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(req, timeout=5)
                assert e.value.code in (404, 501), (method, path)
    finally:
        server.shutdown()
    assert not any("watchdog" in a.lower() for a in dir(engine))


# --- the real watchdog process against a misbehaving nvidia-smi (no hardware needed) ------------

def fake_nvidia_smi(tmp_path, behaviour):
    """An executable `nvidia-smi` placed first on PATH. `behaviour` is shell run after a call
    counter is incremented in $COUNT (so scripts can succeed N times, then break)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "nvidia-smi"
    script.write_text(f"""#!/bin/sh
COUNT=$(cat "{tmp_path}/smi.count" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo $COUNT > "{tmp_path}/smi.count"
{behaviour}
""")
    script.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


GOOD = 'echo "2895, 8188, 55, 3"'


def test_a_gpu_sensor_that_breaks_mid_run_fails_closed_after_the_tolerance(tmp_path, procs):
    child = procs()
    env = fake_nvidia_smi(tmp_path, f'if [ "$COUNT" -le 3 ]; then {GOOD}; else exit 9; fi')
    cfg = watchdog_config(tmp_path, base=DOCKER, watchdog=(
        "interval = 0.1\nterm_grace_seconds = 1\nmax_sensor_failures = 2\n"))
    wd = spawn_watchdog(cfg, child, env=env)
    assert wd.wait(timeout=20) == EXIT_ABORTED
    final = read_json(state_file(tmp_path))
    assert final["reason"] == ["sensor_failed"] and final["action"] == "SIGTERM"
    assert child.wait(timeout=5) == -signal.SIGTERM


def test_malformed_gpu_output_refuses_to_arm(tmp_path, procs):
    child = procs()
    env = fake_nvidia_smi(tmp_path, 'echo "[N/A], 8188, [N/A], [N/A]"')
    wd = spawn_watchdog(watchdog_config(tmp_path, base=DOCKER), child, env=env)
    assert wd.wait(timeout=15) == EXIT_SENSORS
    assert "refusing to arm" in wd.stderr.read() and not state_file(tmp_path).exists()
    assert child.poll() is None


def test_a_host_with_no_gpu_refuses_to_arm_instead_of_pretending(tmp_path, procs):
    child = procs()
    env = fake_nvidia_smi(tmp_path, 'echo "No devices were found" >&2; exit 6')
    wd = spawn_watchdog(watchdog_config(tmp_path, base=DOCKER), child, env=env)
    assert wd.wait(timeout=15) == EXIT_SENSORS and not state_file(tmp_path).exists()
    assert child.poll() is None


def test_a_gpu_that_reports_over_budget_through_the_real_process_aborts(tmp_path, procs):
    child, bystander = procs(), procs()
    env = fake_nvidia_smi(tmp_path, 'echo "7000, 8188, 55, 3"')          # 85.5% > 85%
    wd = spawn_watchdog(watchdog_config(tmp_path, base=DOCKER), child, env=env)
    assert wd.wait(timeout=20) == EXIT_ABORTED
    final = read_json(state_file(tmp_path))
    assert final["reason"] == ["gpu_memory_percent"] and final["sample"]["gpu_memory_percent"] > 85
    assert child.wait(timeout=5) == -signal.SIGTERM and bystander.poll() is None


def test_a_transiently_flaky_gpu_sensor_is_tolerated_up_to_the_configured_failures(
        tmp_path, procs):
    child = procs()
    env = fake_nvidia_smi(tmp_path, f'if [ $((COUNT % 3)) -eq 0 ]; then exit 9; else {GOOD}; fi')
    cfg = watchdog_config(tmp_path, base=DOCKER, watchdog=(
        "interval = 0.1\nterm_grace_seconds = 1\nmax_sensor_failures = 2\n"))
    wd = spawn_watchdog(cfg, child, env=env)
    try:
        time.sleep(2.0)                                                     # ~20 cycles
        assert wd.poll() is None and child.poll() is None                   # never two in a row
        assert read_json(state_file(tmp_path))["status"] == "ARMED"
    finally:
        wd.send_signal(signal.SIGTERM)
        wd.wait(timeout=10)


def test_a_watchdog_that_cannot_write_its_state_refuses_to_arm(tmp_path, procs):
    child = procs()
    cfg = write(tmp_path, KUBERNETES.replace(
        "[safety]", '[safety]\n') + '\n[control_plane]\nstate_file = "missing_dir/state.json"\n')
    wd = spawn_watchdog(cfg, child)
    assert wd.wait(timeout=10) == 5                           # EXIT_STATE: cannot persist its record
    assert "refusing to arm" in wd.stderr.read() and child.poll() is None
