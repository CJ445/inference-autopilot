"""Bare `aiops` on the real machine with NO vLLM container. Opt in: AIOPS_GPU=1

The control plane (real doctor, real watchdog) starts and the TUI opens with no workload. `aiops`
creates no container. When the operator later starts the labelled container the real telemetry
appears; when they stop and then remove it the TUI returns to the unavailable state, and at no
point is there an incident, a proposal or a remediation (there is nothing to act on).

Run this file ALONE: it needs to begin with no managed container.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from vllm_support import NAME, docker, http, state  # noqa: F401

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


def test_aiops_opens_with_no_workload_and_follows_it_as_the_operator_starts_and_stops_it(
        tmp_path, tui_bin, request):
    if docker("ps", "-a", "-q", "--filter", "label=com.inference-autopilot.managed=true"):
        pytest.skip("a managed container already exists; run this test alone")
    before = containers()
    with ControlPlane(tmp_path) as c:
        t = Pty([AIOPS, "--config", str(c.cfg)], env_for(tui_bin), rows=52, cols=132)
        try:
            # -- zero workloads: the control plane starts and the TUI opens ----------------------
            assert t.wait_screen("● CONTROL ONLINE", timeout=120)
            assert t.wait_screen("NO WORKLOAD CONNECTED")             # the plain default: a valid, calm state
            assert t.wait_screen("Autopilot will not create or start one automatically.")
            t.send(b"d")                                              # the technical view for the rest
            assert t.wait_screen("○ NO WORKLOAD") and t.wait_screen("No workload running")
            assert "Probe" not in t.vs.text() and "✗ UNRESPONSIVE" not in t.vs.text()
            assert lifecycle.is_running(lifecycle.read_state(c.state))
            assert c.wd_record()["status"] == "ARMED"                    # the real watchdog
            assert containers() == before                                # aiops created nothing
            status = http(c.port, "/api/v1/status")
            assert status["workload"]["state"] == "absent" and status["last_observation"] is None
            time.sleep(8)                                                # several real ticks
            assert http(c.port, "/api/v1/incidents")["incidents"] == []  # nothing to act on
            assert containers() == before

            # -- the operator starts the labelled container: telemetry appears ------------------
            request.getfixturevalue("vllm")           # the harness provisions it, not aiops
            assert t.wait_screen("Probe ✓", timeout=120) and t.wait_screen("Metrics ✓")
            assert "No workload running" not in t.vs.text()
            # Docker's health check lags real recovery (ADR-018): telemetry is shown while it still
            # says `starting`; the state then settles on `running`. Never absent/stopped here.
            assert http(c.port, "/api/v1/status")["workload"]["state"] in ("starting", "running")
            wait_for(lambda: http(c.port, "/api/v1/status")["workload"]["state"] == "running",
                     60, "the workload to settle on running")
            # neither the model load (Docker's start period) nor a single failed probe at the moment
            # it turns healthy (ADR-027: two consecutive failures are required) is an incident
            assert http(c.port, "/api/v1/incidents")["incidents"] == []

            # -- stopped, then removed: back to the unavailable state, still no incident --------
            docker("stop", NAME)
            assert t.wait_screen("○ STOPPED", timeout=60) and t.wait_screen("Workload stopped")
            assert "Probe ✓" not in t.vs.text()
            docker("rm", "-f", NAME)
            assert t.wait_screen("○ NO WORKLOAD", timeout=60)
            # KNOWN LIMIT: during a graceful `docker stop` the container is still `Running` while
            # vLLM shuts down, which is indistinguishable from a hang, so an incident can be opened
            # in that window (and stays pending: ADR-019). Once the workload is gone, though,
            # nothing new is ever opened: there is nothing to act on.
            known = http(c.port, "/api/v1/incidents")["incidents"]
            time.sleep(8)                                                # several real ticks
            assert http(c.port, "/api/v1/incidents")["incidents"] == known
            assert http(c.port, "/api/v1/status")["workload"]["state"] == "absent"
            t.send(b"q")
            assert t.proc.wait(timeout=20) == 0
        finally:
            t.close()
        try:
            assert lifecycle.is_running(lifecycle.read_state(c.state))   # still up after the TUI
        finally:
            assert c.stop().returncode == 0
        wait_for(lambda: not c.state.exists(), 30, "state released")
        assert not c.stray() and not c.wd_state.exists()
