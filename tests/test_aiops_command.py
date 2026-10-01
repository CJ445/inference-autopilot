"""The real `aiops` launcher in a real pseudo-terminal, with the real Rust TUI binary."""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest
from term_screen import VScreen
from test_cli import blocked_config
from test_launcher import plane, state_of  # noqa: F401  (the detached control-plane fixture)

from aiops import lifecycle

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def tui_bin():
    for variant in ("release", "debug"):
        p = ROOT / "tui" / "target" / variant / "aiops-tui"
        if p.exists():
            return str(p)
    pytest.skip("the TUI is not built (cargo build --release --manifest-path tui/Cargo.toml)")


class Pty:
    def __init__(self, argv, env, rows=40, cols=120):
        self.master, slave = pty.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.vs = VScreen(rows, cols)
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                     cwd=ROOT, close_fds=True, start_new_session=True)
        os.close(slave)
        self.raw = b""

    def pump(self, seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.raw += chunk
                self.vs.feed(chunk)

    def wait_screen(self, needle, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            if needle in self.vs.text():
                return True
            if self.proc.poll() is not None:
                self.pump(0.3)
                return needle in self.vs.text()
        return False

    def send(self, data):
        os.write(self.master, data)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        os.close(self.master)


def env_for(tui_bin):
    return {**os.environ, "TERM": "xterm-256color", "AIOPS_TUI_BIN": tui_bin}


def test_aiops_attaches_to_a_running_control_plane_opens_the_tui_and_leaves_it_running(
        plane, tui_bin):
    config, profile, spawn, spawned = plane
    from aiops import launcher
    assert launcher.ensure_control_plane(config, out=lambda *_: None, spawn=spawn)[0] == 0
    pid = state_of(profile)["pid"]

    t = Pty([str(ROOT / "bin" / "aiops"), "--config", str(config)], env_for(tui_bin))
    try:
        assert t.wait_screen("INFERENCE AUTOPILOT") and t.wait_screen("● CONTROL ONLINE")
        assert "Starting control plane" not in t.vs.text()       # attached: nothing was started
        assert state_of(profile)["pid"] == pid                    # still the one control plane
        t.send(b"q")
        assert t.proc.wait(timeout=15) == 0                       # the TUI quits cleanly ...
    finally:
        t.close()
    time.sleep(0.5)
    assert lifecycle.is_running(state_of(profile))                # ... and the control plane lives
    assert state_of(profile)["pid"] == pid


def test_aiops_that_cannot_start_the_control_plane_says_why_and_never_opens_the_tui(
        tmp_path, tui_bin):
    cfg = blocked_config(tmp_path)
    t = Pty([str(ROOT / "bin" / "aiops"), "--config", str(cfg)], env_for(tui_bin))
    try:
        assert t.wait_screen("Control plane could not be started", timeout=60)
        assert t.wait_screen("Run diagnostics with") and t.wait_screen("aiops doctor")
        assert t.wait_screen("refusing to start")                 # the lifecycle's real reason
        assert t.proc.wait(timeout=30) == 1
        assert "INFERENCE AUTOPILOT" not in t.vs.text()           # no TUI pretending all is well
    finally:
        t.close()
    assert not (tmp_path / "run" / "aiops.state.json").exists()   # and nothing was left running


def test_aiops_with_a_bad_configuration_is_an_error_not_a_tui(tmp_path, tui_bin):
    bad = tmp_path / "aiops.toml"
    bad.write_text('profile = "fake-gpu"\n')
    t = Pty([str(ROOT / "bin" / "aiops"), "--config", str(bad)], env_for(tui_bin))
    try:
        assert t.wait_screen("configuration error")
        assert t.proc.wait(timeout=30) == 2
    finally:
        t.close()
