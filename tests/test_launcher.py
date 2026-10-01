"""`aiops` with no command: attach or start-then-attach, then hand over to the TUI.

The end-to-end tests run the REAL `lifecycle.start` (state file, claim, API, graceful stop) in a
genuinely detached process; only the hardware-facing pieces (doctor, engine world, watchdog) are
the same stand-ins the lifecycle tests use.
"""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from test_lifecycle import free_port, http, make_config, wait_for

from aiops import launcher, lifecycle
from aiops.profile import load_profile

TESTS = Path(__file__).parent
ROOT = TESTS.parent


class Out:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


# a stand-in for `aiops start`: the real lifecycle.start with the test doubles for hardware
STUB = textwrap.dedent("""
    import sys, threading, signal
    sys.path.insert(0, {tests!r}); sys.path.insert(0, {root!r})
    from test_lifecycle import ok_doctor, fake_build, FakeWatchdogs
    from test_engine import World
    from aiops.lifecycle import start
    ev = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: ev.set())
    sys.exit(start({config!r}, ev, doctor=ok_doctor, build=fake_build(World()),
                   spawn_watchdog=FakeWatchdogs()))
""")


@pytest.fixture
def plane(tmp_path):
    """A config, and a spawner that runs the stub as a detached `start`. Ends what it started."""
    config = make_config(tmp_path)
    profile = load_profile(config)
    script = tmp_path / "stub_start.py"
    script.write_text(STUB.format(tests=str(TESTS), root=str(ROOT), config=str(config)))
    spawned = []

    def spawn(config_path, log):
        p = launcher.spawn_start(config_path, log, argv=[sys.executable, str(script)])
        spawned.append(p)
        return p

    yield config, profile, spawn, spawned
    lifecycle.stop(config, out=lambda *_: None)
    for p in spawned:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


def state_of(profile):
    return lifecycle.read_state(Path(profile["control_plane"]["state_file"]))


# --- attach / start / wait ---------------------------------------------------------------------

def test_an_already_running_control_plane_is_attached_not_duplicated(plane):
    config, profile, spawn, spawned = plane
    out = Out()
    code, url = launcher.ensure_control_plane(config, out=out, spawn=spawn)     # starts it
    assert code == 0 and len(spawned) == 1
    pid = state_of(profile)["pid"]

    out2 = Out()
    code, url2 = launcher.ensure_control_plane(config, out=out2, spawn=spawn)   # attaches
    assert code == 0 and url2 == url
    assert len(spawned) == 1 and state_of(profile)["pid"] == pid               # no second one
    assert "Starting control plane" not in out2.text


def test_a_stopped_control_plane_is_started_through_the_existing_start_and_awaited(plane):
    config, profile, spawn, spawned = plane
    assert state_of(profile) is None
    out = Out()
    code, url = launcher.ensure_control_plane(config, out=out, spawn=spawn)
    assert code == 0 and url == f"http://127.0.0.1:{profile['control_plane']['port']}"
    assert "CONTROL ONLINE" in out.text and "Starting control plane" in out.text
    assert "started: profile" in out.text            # the lifecycle's own output, not invented steps
    state = state_of(profile)
    assert lifecycle.is_running(state)
    assert http(profile["control_plane"]["port"], "/health")["status"] == "HEALTHY"   # really up
    assert spawned[0].pid == state["pid"]


def test_the_control_plane_is_detached_and_outlives_the_launcher(plane):
    config, profile, spawn, spawned = plane
    launcher.ensure_control_plane(config, out=Out(), spawn=spawn)
    pid = state_of(profile)["pid"]
    assert os.getsid(pid) == pid and os.getsid(pid) != os.getsid(0)      # its own session
    # what exec'ing the TUI (and the TUI quitting) amounts to for the control plane: nothing
    time.sleep(0.5)
    assert lifecycle.is_running(state_of(profile))
    assert http(profile["control_plane"]["port"], "/health")["status"] == "HEALTHY"


def test_readiness_means_the_api_answers_not_just_that_the_process_exists(tmp_path):
    config = make_config(tmp_path)
    profile = load_profile(config)
    state_file = Path(profile["control_plane"]["state_file"])

    class Child:
        returncode = None

        def poll(self):
            return None

    def spawn(config_path, log):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"pid": os.getpid(), "port": 4242,
                                          "proc_start": lifecycle.proc_start(os.getpid())}))
        return Child()

    answers = iter([False, False, False, True])
    asked = []

    def ready(port):
        asked.append(port)
        return next(answers)

    code, url = launcher.ensure_control_plane(config, out=Out(), spawn=spawn, ready=ready,
                                              sleep=lambda s: None)
    assert code == 0 and url == "http://127.0.0.1:4242" and len(asked) == 4


def test_a_start_that_fails_is_reported_with_its_real_output_and_no_tui(tmp_path):
    config = make_config(tmp_path)
    log_holder = {}

    class Dead:
        def poll(self):
            return 1

    def spawn(config_path, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("refusing to start: 1 blocking prerequisite(s) failed\n")
        log_holder["log"] = log
        return Dead()

    out = Out()
    code, url = launcher.ensure_control_plane(config, out=out, spawn=spawn,
                                              ready=lambda p: False, sleep=lambda s: None)
    assert code == 1 and url is None
    assert "could not be started" in out.text and "exited with code 1" in out.text
    assert "refusing to start: 1 blocking prerequisite(s) failed" in out.text
    assert "aiops doctor" in out.text


def test_a_readiness_timeout_is_an_error_and_does_not_kill_what_it_started(tmp_path):
    config = make_config(tmp_path)

    class Slow:
        killed = False

        def poll(self):
            return None

        def kill(self):
            Slow.killed = True

    ticks = iter(range(0, 10_000, 5))
    code, url = launcher.ensure_control_plane(
        config, out=(out := Out()), spawn=lambda c, l: Slow(), ready=lambda p: False,
        sleep=lambda s: None, clock=lambda: next(ticks), timeout=20)
    assert code == 1 and url is None and not Slow.killed
    assert "did not answer within 20s" in out.text and "aiops stop" in out.text


def test_a_running_control_plane_whose_api_never_answers_is_reported_not_entered(tmp_path):
    config = make_config(tmp_path)
    profile = load_profile(config)
    state_file = Path(profile["control_plane"]["state_file"])
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"pid": os.getpid(), "port": 4242,
                                      "proc_start": lifecycle.proc_start(os.getpid())}))
    ticks = iter(range(0, 10_000, 5))
    out = Out()
    code, url = launcher.ensure_control_plane(
        config, out=out, spawn=lambda *a: pytest.fail("must not start a second one"),
        ready=lambda p: False, sleep=lambda s: None, clock=lambda: next(ticks), timeout=20)
    assert code == 1 and url is None and "is not answering" in out.text


def test_a_corrupt_state_file_is_never_started_over(tmp_path):
    config = make_config(tmp_path)
    state_file = Path(load_profile(config)["control_plane"]["state_file"])
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{ not json")
    out = Out()
    code, _ = launcher.ensure_control_plane(
        config, out=out, spawn=lambda *a: pytest.fail("must not start over corrupt state"))
    assert code == 1 and "unreadable" in out.text


def test_a_bad_configuration_is_a_configuration_error(tmp_path):
    bad = tmp_path / "aiops.toml"
    bad.write_text('profile = "fake-gpu"\n')
    out = Out()
    assert launcher.ensure_control_plane(str(bad), out=out) == (2, None)
    assert "configuration error" in out.text


# --- the command itself ------------------------------------------------------------------------

def test_aiops_launches_the_tui_only_after_the_control_plane_is_ready(tmp_path, monkeypatch):
    fake_bin = tmp_path / "aiops-tui"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AIOPS_TUI_BIN", str(fake_bin))
    order, launched = [], []

    def ensure(config, out):
        order.append("ensure")
        return 0, "http://127.0.0.1:4242"

    monkeypatch.setattr(signal, "signal", lambda *a: None)
    launcher.main(["--config", "x.toml"], out=Out(), isatty=lambda: True, ensure=ensure,
                  tui_exec=lambda path, argv: (order.append("exec"), launched.append((path, argv))))
    assert order == ["ensure", "exec"]
    assert launched == [(str(fake_bin), [str(fake_bin), "--url", "http://127.0.0.1:4242"])]


def test_aiops_does_not_launch_the_tui_when_startup_fails(tmp_path, monkeypatch):
    fake_bin = tmp_path / "aiops-tui"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AIOPS_TUI_BIN", str(fake_bin))
    code = launcher.main([], out=Out(), isatty=lambda: True, ensure=lambda c, out: (1, None),
                         tui_exec=lambda *a: pytest.fail("a broken start must not open the UI"))
    assert code == 1


def test_aiops_without_a_built_tui_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOPS_TUI_BIN", str(tmp_path / "missing"))
    out = Out()
    code = launcher.main([], out=out, isatty=lambda: True,
                         ensure=lambda c, out: pytest.fail("must not start a control plane"))
    assert code == 1 and "cargo build --release" in out.text


def test_aiops_needs_a_terminal(monkeypatch):
    out = Out()
    assert launcher.main([], out=out, isatty=lambda: False,
                         ensure=lambda c, out: pytest.fail("no terminal")) == 2
    assert "not a terminal" in out.text and "aiops start" in out.text
