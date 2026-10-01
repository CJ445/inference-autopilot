"""Practice on the real machine, with NO vLLM container. Opt in: AIOPS_GPU=1

The real control plane (real doctor, real watchdog, real Docker provider) is running. In the TUI the
operator practices the whole loop on a simulated incident. Nothing real may move: no container is
created, no real incident or proposal appears, the real watchdog and workload state are untouched,
and leaving practice returns to the real "no workload" view.

Run this file ALONE: it needs to begin with no managed container.
"""
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from vllm_support import docker, http

sys.path.insert(0, str(Path(__file__).parent.parent))
from test_aiops_command import Pty, env_for  # noqa: E402
from test_real_watchdog import ControlPlane, wait_for  # noqa: E402

from aiops import lifecycle  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU")

AIOPS = str(Path(__file__).parent.parent.parent / "bin" / "aiops")


def containers():
    return set(docker("ps", "-a", "-q").split())


def test_practice_on_the_real_machine_touches_nothing_real(tmp_path, tui_bin):
    if docker("ps", "-a", "-q", "--filter", "label=com.inference-autopilot.managed=true"):
        pytest.skip("a managed container already exists; run this test alone")
    before = containers()
    with ControlPlane(tmp_path) as c:
        t = Pty([AIOPS, "--config", str(c.cfg)], env_for(tui_bin), rows=40, cols=132)
        try:
            assert t.wait_screen("No workload connected", timeout=120)  # the real, empty system
            assert t.wait_screen("P  Practice an incident")
            t.send(b"d")                                               # the technical view for the rest
            assert t.wait_screen("○ NO WORKLOAD") and t.wait_screen("Press P to practice an incident")
            real = http(c.port, "/api/v1/status")
            assert real["mode"] == "REAL" and real["workload"]["state"] == "absent"
            pid = lifecycle.read_state(c.state)["pid"]

            t.send(b"p")                                                # -- practice ---------------
            assert t.wait_screen("PRACTICE · SIMULATION") and t.wait_screen("Press F to break the model")
            assert "NO WORKLOAD" not in t.vs.text()                     # no real data under the banner
            t.send(b"f")
            assert t.wait_screen("Open the incident (Enter), review it, then approve (A).", timeout=30)
            t.send(b"\r")
            assert t.wait_screen("Classification")
            t.send(b"a")
            assert t.wait_screen("A simulated restart: nothing real is restarted.")
            time.sleep(0.8)
            t.send(b"\r")
            assert t.wait_screen("RESOLVED", timeout=30)

            # nothing real moved while the simulation ran its whole loop
            assert containers() == before                               # no container created
            assert http(c.port, "/api/v1/incidents")["incidents"] == []  # no real incident
            status = http(c.port, "/api/v1/status")
            assert status["mode"] == "REAL" and status["workload"]["state"] == "absent"
            assert status["incidents"] == {"active": [], "pending": []}
            assert c.wd_record()["status"] == "ARMED"                   # the real watchdog is untouched
            assert lifecycle.read_state(c.state)["pid"] == pid

            t.send(b"\x1b")                                             # -- back to the real system -
            time.sleep(0.6)
            t.send(b"\x1b")
            assert t.wait_screen("○ NO WORKLOAD", timeout=30)
            assert "PRACTICE" not in t.vs.text() and "SIMULATION" not in t.vs.text()
            t.send(b"q")
            assert t.proc.wait(timeout=20) == 0
        finally:
            t.close()
        try:
            assert lifecycle.is_running(lifecycle.read_state(c.state))  # the control plane lives on
        finally:
            assert c.stop().returncode == 0
        wait_for(lambda: not c.state.exists(), 30, "state released")
        assert not c.stray() and not c.wd_state.exists()
        assert containers() == before
