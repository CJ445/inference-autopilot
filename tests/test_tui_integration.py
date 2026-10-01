"""The real TUI binary in a real pseudo-terminal against a real running control-plane API.

The server is the actual aiops API + engine with a stand-in cluster, so no hardware is needed;
the real-GPU run of the same flow lives in tests/gpu/test_real_tui.py.
"""
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from term_screen import VScreen
from test_engine import CONFIG, World

from aiops.engine import Engine
from aiops.serve import Service
from aiops.system import System

ROOT = Path(__file__).parent.parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|\x1b[=>]")


@pytest.fixture(scope="session")
def tui_bin():
    for variant in ("release", "debug"):
        p = ROOT / "tui" / "target" / variant / "aiops-tui"
        if p.exists():
            return str(p)
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed and the TUI is not built")
    subprocess.run(["cargo", "build", "--release", "--locked", "--manifest-path",
                    str(ROOT / "tui" / "Cargo.toml")], check=True, capture_output=True, timeout=900)
    return str(ROOT / "tui" / "target" / "release" / "aiops-tui")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ControlPlane:
    """The real Service (tick loop + API) over a stand-in cluster."""

    def __init__(self, port=None, fault=True):
        self.world = World()
        self.world.fault, self.world.failures = fault, 5
        self.engine = Engine(self.world, self.world, CONFIG)
        self.port = port or free_port()
        self.service = None

    def start(self):
        self.stop_requested = threading.Event()
        self.system = System(
            {"config_path": "/etc/aiops/aiops.toml", "name": "stand-in", "provider": "kubernetes",
             "workload": {"name": "vllm-0"}, "control_plane": {"port": self.port}},
            self.stop_requested.set,
            doctor=lambda p: [
                {"check": "python", "status": "PASS", "detail": "3.13.5", "blocking": True},
                {"check": "gpu_headroom", "status": "WARN", "detail": "only 900 MiB free",
                 "blocking": False},
                {"check": "kubectl", "status": "NOT_APPLICABLE", "detail": "n/a", "blocking": False}])
        self.service = Service(self.engine, port=self.port, interval=0.2, system=self.system,
                               info={"profile": "stand-in", "provider": "kubernetes",
                                     "workload": "vllm-0", "pid": os.getpid()},
                               status_extra=lambda: {"watchdog": {
                                   "state": "armed", "pid": 1, "protected_pid": os.getpid(),
                                   "limits": {"max_ram_percent": 90},
                                   "last_sample": {"ram_percent": 40.0},
                                   "last_check_age_seconds": 0.2}})
        self.service.start()
        return self

    def stop(self):
        if self.service:
            self.service.stop()
            self.service = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def api(self, path):
        with urllib.request.urlopen(self.url + path, timeout=5) as r:
            return json.load(r)


class Term:
    """The TUI running in a pty; reads what it draws, sends what an operator types."""

    def __init__(self, binary, url, rows=40, cols=120, extra=()):
        self.master, slave = pty.openpty()
        self.vs = VScreen(rows, cols)
        self.resize(rows, cols)
        env = {**os.environ, "TERM": "xterm-256color"}
        self.proc = subprocess.Popen([binary, "--url", url, "--interval-ms", "250", *extra],
                                     stdin=slave, stdout=slave, stderr=slave, env=env,
                                     close_fds=True, start_new_session=True)
        os.close(slave)
        self.raw = b""

    def resize(self, rows, cols):
        self.vs.resize(rows, cols)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if getattr(self, "proc", None):
            self.proc.send_signal(signal.SIGWINCH)

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

    def screen(self):
        """What is on the terminal right now (not what was ever written)."""
        return self.vs.text()

    def wait_screen(self, needle, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            if needle in self.screen():
                return True
            if self.proc.poll() is not None:
                self.pump(0.2)
                return needle in self.screen()
        return False

    def mark(self):
        return len(self.raw)

    def text(self, since=0):
        return ANSI.sub("", self.raw[since:].decode("utf-8", "replace"))

    def wait_for(self, needle, since=0, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.1)
            if needle in self.text(since):
                return True
            if self.proc.poll() is not None:
                self.pump(0.2)
                return needle in self.text(since)
        return False

    def send(self, data):
        os.write(self.master, data)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        os.close(self.master)


@pytest.fixture
def cp():
    plane = ControlPlane().start()
    yield plane
    plane.stop()


@pytest.fixture
def term(tui_bin):
    made = []

    def make(url, **kw):
        t = Term(tui_bin, url, **kw)
        made.append(t)
        return t

    yield make
    for t in made:
        t.close()


def once(tui_bin, url, *args):
    return subprocess.run([tui_bin, "--once", "--url", url, *args], capture_output=True,
                          text=True, timeout=20)


def test_the_tui_shows_the_live_state_of_the_real_control_plane(term, cp):
    t = term(cp.url)
    assert t.wait_for("● CONTROL ONLINE")
    for needle in ["stand-in", "inc_001", "GPU_MEMORY_PRESSURE", "AWAITING APPROVAL",
                   "restart_workload", "● ARMED", "AUDIT ✓ VERIFIED"]:
        assert t.wait_for(needle), needle


def test_confirmation_stands_between_the_keypress_and_the_action(term, cp):
    t = term(cp.url)
    assert t.wait_for("inc_001")
    t.send(b"\r")                                   # inspect
    assert t.wait_for("· inc_001 ·") and t.wait_for("AWAITING APPROVAL")
    mark = t.mark()
    t.send(b"a")
    assert t.wait_for("Approve restart_workload?", since=mark)
    assert t.wait_for("[Enter] Confirm", since=mark)
    time.sleep(1.0)
    assert cp.world.restarts == 0                   # nothing happens without the confirmation
    t.send(b"\x1b")                                 # Esc cancels
    time.sleep(1.0)
    assert cp.world.restarts == 0 and cp.api("/api/v1/incidents")["incidents"][0][
        "status"] == "POLICY_CHECK"


def test_approving_through_the_tui_goes_through_the_servers_policy_and_resolves(term, cp, tui_bin):
    t = term(cp.url)
    assert t.wait_for("inc_001")
    t.send(b"\r")
    assert t.wait_for("AWAITING APPROVAL")
    t.send(b"a")
    assert t.wait_for("Approve restart_workload?")
    t.send(b"\r")                                   # confirm
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and cp.world.restarts == 0:
        t.pump(0.1)
    assert cp.world.restarts == 1                   # the SERVER executed it, once
    detail = cp.api("/api/v1/incidents/inc_001")
    assert detail["status"] == "RESOLVED" and detail["remediation"]["state"] == "EXECUTED"
    assert any(e["event"] == "approval_granted" for e in cp.api("/api/v1/audit")["events"])
    assert t.wait_for("RESOLVED")                   # shown because the server said so
    frame = once(tui_bin, cp.url, "--screen", "detail:inc_001", "--height", "60")
    assert frame.returncode == 0
    for needle in ["Verification", "✓ Workload identity changed", "RESULT", "RESOLVED"]:
        assert needle in frame.stdout, frame.stdout


def test_rejecting_through_the_tui_asks_the_server_to_reject_and_restarts_nothing(term, cp):
    t = term(cp.url)
    assert t.wait_for("inc_001")
    t.send(b"r")
    assert t.wait_for("Reject restart_workload?")
    t.send(b"\r")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and cp.api("/api/v1/incidents")["incidents"][0][
            "status"] != "REJECTED":
        t.pump(0.1)
    assert cp.api("/api/v1/incidents")["incidents"][0]["status"] == "REJECTED"
    assert cp.world.restarts == 0


def test_a_server_refusal_is_shown_to_the_operator(term, cp):
    t = term(cp.url)
    assert t.wait_for("inc_001")
    t.send(b"a")
    assert t.wait_for("Approve restart_workload?")
    # the incident is decided elsewhere before the operator confirms
    urllib.request.urlopen(urllib.request.Request(
        cp.url + "/api/v1/incidents/inc_001/remediation/reject", method="POST", data=b""), timeout=5)
    mark = t.mark()
    t.send(b"\r")
    assert t.wait_for("POLICY_DENIED", since=mark)
    assert cp.world.restarts == 0


def test_q_quits_cleanly_and_restores_the_terminal(term, cp):
    t = term(cp.url)
    assert t.wait_for("● CONTROL ONLINE")
    t.send(b"q")
    assert t.proc.wait(timeout=10) == 0
    t.pump(0.3)
    assert b"\x1b[?1049l" in t.raw                   # left the alternate screen


def test_ctrl_c_quits_cleanly(term, cp):
    t = term(cp.url)
    assert t.wait_for("● CONTROL ONLINE")
    t.send(b"\x03")
    assert t.proc.wait(timeout=10) == 0
    t.pump(0.3)
    assert b"\x1b[?1049l" in t.raw


def test_the_tui_waits_for_a_control_plane_that_is_not_up_yet_and_then_recovers(term):
    port = free_port()
    t = term(f"http://127.0.0.1:{port}")
    assert t.wait_for("CONTROL PLANE OFFLINE")
    assert t.wait_for("Retrying")
    assert t.proc.poll() is None                     # offline never crashes it
    plane = ControlPlane(port=port, fault=False).start()
    try:
        mark = t.mark()
        assert t.wait_for("● CONTROL ONLINE", since=mark, timeout=15)
    finally:
        plane.stop()


def test_a_control_plane_that_goes_away_mid_session_is_shown_stale_not_crashed_on(term, cp):
    t = term(cp.url)
    assert t.wait_for("● CONTROL ONLINE")
    cp.stop()
    mark = t.mark()
    assert t.wait_for("CONTROL PLANE OFFLINE", since=mark, timeout=15)
    assert t.wait_for("STALE", since=mark)
    assert t.proc.poll() is None
    cp.start()
    mark = t.mark()
    assert t.wait_for("● CONTROL ONLINE", since=mark, timeout=15)


def test_a_malformed_server_never_crashes_the_tui(term):
    port = free_port()
    stop = threading.Event()

    def garbage():
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(8)
        s.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = s.accept()
            except OSError:
                continue
            conn.recv(4096)
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 14\r\n\r\n{ not json at ")
            conn.close()
        s.close()

    threading.Thread(target=garbage, daemon=True).start()
    try:
        t = term(f"http://127.0.0.1:{port}")
        assert t.wait_for("CONTROL PLANE OFFLINE")
        assert t.wait_for("unexpected response")
        assert t.proc.poll() is None
        t.send(b"q")
        assert t.proc.wait(timeout=10) == 0
    finally:
        stop.set()


def test_a_slow_server_times_out_and_the_tui_stays_responsive(tui_bin):
    port = free_port()
    stop = threading.Event()

    def slow():
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(8)
        s.settimeout(0.2)
        held = []
        while not stop.is_set():
            try:
                held.append(s.accept()[0])           # accept and never answer
            except OSError:
                pass
        s.close()

    threading.Thread(target=slow, daemon=True).start()
    t = Term(tui_bin, f"http://127.0.0.1:{port}", extra=("--timeout-ms", "500"))
    try:
        assert t.wait_for("CONTROL PLANE OFFLINE", timeout=15)
        assert t.wait_for("did not answer in time", timeout=15)
        t.send(b"q")                                  # still responds to keys
        assert t.proc.wait(timeout=10) == 0
    finally:
        stop.set()
        t.close()


def test_resizing_the_terminal_redraws_and_a_tiny_one_degrades_gracefully(term, cp):
    t = term(cp.url)
    assert t.wait_for("● CONTROL ONLINE")
    mark = t.mark()
    t.resize(14, 50)
    assert t.wait_for("too small", since=mark)
    assert t.proc.poll() is None
    mark = t.mark()
    t.resize(40, 120)
    assert t.wait_for("INFERENCE AUTOPILOT", since=mark) and t.wait_for("inc_001", since=mark)


def test_screens_can_be_switched_and_the_audit_log_is_shown(term, cp):
    t = term(cp.url)
    assert t.wait_for("● CONTROL ONLINE")
    t.send(b"3")
    assert t.wait_screen("Audit log")      # the emulated screen: ratatui redraws only changed cells
    assert t.wait_screen("incident_created") and t.wait_screen("rca_generated")
    t.send(b"2")
    assert t.wait_screen("CATEGORY") and t.wait_screen("inc_001")


def test_the_tui_never_starts_another_process(term, cp):
    t = term(cp.url)
    assert t.wait_for("● CONTROL ONLINE")
    t.send(b"\r")
    t.send(b"a")
    t.pump(0.5)
    children = subprocess.run(["ps", "-o", "pid=", "--ppid", str(t.proc.pid)],
                              capture_output=True, text=True).stdout.split()
    assert children == []
    t.send(b"\x1b")


def test_once_prints_the_real_state_and_signals_an_unreachable_control_plane(tui_bin, cp):
    ok = once(tui_bin, cp.url)
    assert ok.returncode == 0 and "inc_001" in ok.stdout and "● CONTROL ONLINE" in ok.stdout
    down = once(tui_bin, f"http://127.0.0.1:{free_port()}", "--height", "24")
    assert down.returncode == 3 and "CONTROL PLANE OFFLINE" in down.stdout


def test_the_tui_refuses_a_non_loopback_control_plane_address(tui_bin):
    r = subprocess.run([tui_bin, "--once", "--url", "http://203.0.113.9:8080"],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 1 and "loopback" in r.stderr


def test_bad_arguments_are_rejected_with_usage(tui_bin):
    for args in (["--interval-ms", "5"], ["--bogus"], ["--url"], ["--timeout-ms", "x"]):
        r = subprocess.run([tui_bin, *args], capture_output=True, text=True, timeout=10)
        assert r.returncode == 1 and "USAGE" in r.stderr, args
    assert subprocess.run([tui_bin, "--help"], capture_output=True, text=True).returncode == 0


# --- the operator application: palette, system screens, stop ------------------------------------------

def test_the_command_palette_opens_filters_and_navigates(term, cp):
    t = term(cp.url)
    assert t.wait_screen("inc_001")
    t.send(b"\x10")                                  # Ctrl+P
    assert t.wait_screen("Search commands")
    t.send(b"about")
    assert t.wait_screen("Open About") and not t.screen().count("Open Overview")
    t.send(b"\r")
    assert t.wait_screen("Autonomous AIOps for LLM inference")
    assert t.wait_screen("Python")                    # the control plane's real /version answer
    t.send(b"q")
    assert t.proc.wait(timeout=10) == 0


def test_settings_show_the_servers_real_active_configuration(term, cp):
    t = term(cp.url)
    assert t.wait_screen("inc_001")
    t.send(b"5")
    assert t.wait_screen("/etc/aiops/aiops.toml") and t.wait_screen("● Valid")
    assert t.wait_screen("[workload]") and t.wait_screen("vllm-0")


def test_diagnostics_show_the_doctors_results_from_the_server(term, cp):
    t = term(cp.url)
    assert t.wait_screen("inc_001")
    t.send(b"6")
    for needle in ["● PASS", "● WARN", "○ NOT_APPLICABLE", "3.13.5", "only 900 MiB free", "Warnings"]:
        assert t.wait_screen(needle), needle


def test_stopping_the_control_plane_from_the_tui_needs_confirmation_and_asks_the_server_once(term, cp):
    t = term(cp.url)
    assert t.wait_screen("inc_001")
    t.send(b"4")
    assert t.wait_screen("[ X ] Stop control plane")
    t.send(b"x")
    assert t.wait_screen("Stop control plane?")
    time.sleep(0.8)
    assert not cp.stop_requested.is_set()              # the dialog alone does nothing
    t.send(b"\x1b")
    time.sleep(0.8)
    assert not cp.stop_requested.is_set()              # Esc cancels
    t.send(b"x")
    assert t.wait_screen("Stop control plane?")
    t.send(b"\r")
    assert cp.stop_requested.wait(10)                  # the SERVER was asked, through its API
    assert t.wait_screen("stop requested")
    assert cp.world.restarts == 0                      # no workload was touched


def test_the_tui_follows_the_workload_appearing_stopping_and_disappearing(term, tui_bin):
    holder = {"state": "absent"}
    plane = ControlPlane(fault=False)
    plane.engine.presence = lambda: holder["state"]
    plane.start()
    try:
        t = term(plane.url)
        assert t.wait_screen("● CONTROL ONLINE")
        assert t.wait_screen("No workload running") and t.wait_screen("○ NO WORKLOAD")
        assert "✗ UNRESPONSIVE" not in t.screen() and "Probe" not in t.screen()
        assert plane.api("/api/v1/status")["workload"]["state"] == "absent"

        holder["state"] = "running"                      # the operator starts the workload
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "No workload running" in t.screen():
            t.pump(0.2)
        assert "No workload running" not in t.screen() and t.wait_screen("Observed")

        holder["state"] = "stopped"                      # ... stops it
        assert t.wait_screen("○ STOPPED") and t.wait_screen("Workload stopped")
        holder["state"] = "absent"                       # ... removes it
        assert t.wait_screen("○ NO WORKLOAD") and t.wait_screen("No workload running")
        assert plane.world.restarts == 0                 # no remediation ever had anything to act on
        t.send(b"q")
        assert t.proc.wait(timeout=10) == 0
    finally:
        plane.stop()
