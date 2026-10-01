"""Bare `aiops` against the real RTX GPU, real vLLM container and the real lifecycle.
Opt in: AIOPS_GPU=1

No control plane running -> `aiops` starts the REAL one (real doctor, real watchdog) detached ->
waits for the real API -> TUI shows real GPU/vLLM state -> Q leaves the control plane running ->
`aiops` again attaches (no second control plane) -> `aiops stop` leaves nothing behind.
"""
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from vllm_support import MODEL, docker, http, processes, state  # noqa: F401

sys.path.insert(0, str(Path(__file__).parent.parent))
from test_aiops_command import Pty, env_for  # noqa: E402
from test_real_watchdog import ControlPlane, wait_for  # noqa: E402

from aiops import lifecycle  # noqa: E402
from aiops.docker import DockerProvider  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")

AIOPS = str(Path(__file__).parent.parent.parent / "bin" / "aiops")


def test_bare_aiops_starts_attaches_and_stops_the_real_control_plane(vllm, tmp_path, tui_bin):
    import subprocess
    uuid = subprocess.run(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    provider = DockerProvider("vllm")
    workload_before = provider.get_workload("vllm")["id"]
    with ControlPlane(tmp_path) as c:                    # config only: it is NOT started here
        assert not c.state.exists() and not c.stray()

        # -- first `aiops`: nothing is running, so it starts the real control plane ------------
        t = Pty([AIOPS, "--config", str(c.cfg)], env_for(tui_bin), rows=52, cols=132)
        try:
            assert t.wait_screen("● CONTROL ONLINE", timeout=120)
            assert t.wait_screen("✓ HEALTHY", timeout=60)               # plain by default
            assert t.wait_screen("✓ WATCHING")
            t.send(b"d")                                                # then the technical view
            for needle in ["INFERENCE AUTOPILOT", "docker-real-gpu", uuid[:12], "Probe ✓",
                           "Metrics ✓", "● ARMED", "Identity ✓", "AUDIT ✓ VERIFIED",
                           f"vLLM · {MODEL}"]:
                assert t.wait_screen(needle, timeout=30), needle
            state_ = lifecycle.read_state(c.state)
            pid = state_["pid"]
            assert lifecycle.is_running(state_) and os.getsid(pid) == pid   # detached session
            assert c.wd_record()["status"] == "ARMED"                        # the REAL watchdog
            assert c.status()["watchdog"]["state"] == "armed"
            t.send(b"q")
            assert t.proc.wait(timeout=20) == 0
        finally:
            t.close()

        # -- the TUI is gone; the control plane and its watchdog are still there ---------------
        time.sleep(1.0)
        assert lifecycle.is_running(lifecycle.read_state(c.state))
        assert http(c.port, "/health")["status"] in ("HEALTHY", "DEGRADED")
        assert c.wd_record()["status"] == "ARMED"
        assert provider.get_workload("vllm")["id"] == workload_before       # workload untouched

        # -- second `aiops`: attaches; still exactly one control plane --------------------------
        t = Pty([AIOPS, "--config", str(c.cfg)], env_for(tui_bin), rows=52, cols=132)
        try:
            assert t.wait_screen("● CONTROL ONLINE", timeout=60)
            assert "Starting control plane" not in t.vs.text()
            assert lifecycle.read_state(c.state)["pid"] == pid
            assert len(processes("aiops start --config", str(c.cfg))) == 1
            t.send(b"q")
            assert t.proc.wait(timeout=20) == 0
        finally:
            t.close()

        # -- explicit stop: the real graceful path, nothing left behind -------------------------
        assert c.stop().returncode == 0
        wait_for(lambda: not c.state.exists(), 30, "state released")
        assert not c.stray()
        assert not c.wd_state.exists()                                       # watchdog disarmed
        assert provider.get_workload("vllm")["id"] == workload_before        # still untouched
