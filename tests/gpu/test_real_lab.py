"""The Lab and the real fault THROUGH THE OPERATOR UI, on the real machine. Opt in: AIOPS_GPU=1

A real fault is injected from the TUI (F, a dialog, Enter), the real workload really pauses, the real
detector opens an incident, the fault is ended, and an approval made afterwards is refused by the
server: the TUI says so, and the real container is not restarted.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from vllm_support import BASE, MODEL, NAME, docker, http, wait_for

sys.path.insert(0, str(Path(__file__).parent.parent))
from test_real_fault_injector import paused  # noqa: E402
from test_real_watchdog import ControlPlane  # noqa: E402
from test_tui_integration import Term  # noqa: E402

from aiops.docker import DockerProvider  # noqa: E402
from aiops.vllm import VllmClient  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")


def test_a_real_fault_from_the_lab_and_an_approval_that_is_refused_because_the_problem_is_gone(
        vllm, tmp_path, tui_bin):
    provider = DockerProvider("vllm")
    with ControlPlane(tmp_path) as c:
        c.start()
        wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"], 60, "an observation")
        identity = provider.get_workload("vllm")["id"]
        t = Term(tui_bin, f"http://127.0.0.1:{c.port}", rows=44, cols=130)
        try:
            assert t.wait_screen("✓ HEALTHY", timeout=60)
            assert t.wait_screen("[F] Inject a real fault")             # offered on Home, from the server

            # -- the Lab lists the real fault, labelled, and F opens a dialog that sends nothing ----------
            t.send(b"3")
            assert t.wait_screen("Real infrastructure") and t.wait_screen("affects the running workload")
            assert t.wait_screen("[F] Pause the running workload")
            t.send(b"f")
            assert t.wait_screen("REAL INFRASTRUCTURE FAULT") and t.wait_screen("LIVE · GPU-REAL")
            assert t.wait_screen("[Enter] Inject fault")
            time.sleep(1.0)
            assert not paused()                                          # the dialog alone paused nothing
            t.send(b"\x1b")
            time.sleep(0.5)
            assert not paused()                                          # Esc cancels

            # -- confirm: the REAL workload pauses, and the Lab shows it --------------------------------
            t.send(b"f")
            assert t.wait_screen("REAL INFRASTRUCTURE FAULT")
            time.sleep(0.8)
            t.send(b"\r")
            wait_for(paused, 20, "the workload to pause")
            assert t.wait_screen("REAL FAULT IN PROGRESS") and t.wait_screen("it resumes by itself in")

            # -- the real detector opens a real incident; the Lab tells the story from the server ----------
            assert t.wait_screen("Needs your OK", timeout=120)
            for needle in ["Detected", "Diagnosis", "Recovery proposed", "Restart the model server"]:
                assert t.wait_screen(needle), needle
            iid = http(c.port, "/api/v1/incidents")["incidents"][0]["incident_id"]
            assert http(c.port, f"/api/v1/incidents/{iid}")["status"] == "POLICY_CHECK"
            assert provider.get_workload("vllm")["id"] == identity       # nothing restarted

            # -- the operator ends the fault; the model answers again ------------------------------------
            t.send(b"\x10")
            assert t.wait_screen("Search commands")
            t.send(b"resume")
            assert t.wait_screen("Resume the workload now")
            t.send(b"\r")
            wait_for(lambda: not paused(), 30, "the workload to resume")
            wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"]["inference_probe_ok"] is True,
                     90, "the model to answer again")

            # -- approving now is refused by the server, and the TUI says nothing was restarted -------------
            t.send(b"\r")                                                # Enter in the Lab: the incident
            assert t.wait_screen("[ A ] Approve", timeout=30)
            t.send(b"a")
            assert t.wait_screen("RECOVERY REQUEST")
            time.sleep(0.8)
            t.send(b"\r")
            assert t.wait_screen("RECOVERY NO LONGER NEEDED", timeout=30)
            assert t.wait_screen("No restart was performed.")
            assert provider.get_workload("vllm")["id"] == identity       # the REAL container was not restarted
            assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True
            detail = http(c.port, f"/api/v1/incidents/{iid}")
            assert detail["status"] == "CLEARED"
            names = [e["event"] for e in http(c.port, "/api/v1/audit")["events"]]
            assert "approval_refused" in names and "approval_granted" not in names \
                and "remediation_started" not in names
            t.send(b" ")                                                  # any key closes the dialog
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and "RECOVERY NO LONGER NEEDED" in t.screen():
                t.pump(0.2)
            assert "RECOVERY NO LONGER NEEDED" not in t.screen()
            t.send(b"q")
            assert t.proc.wait(timeout=15) == 0
        finally:
            t.close()
        assert json.loads(docker("inspect", NAME))[0]["State"]["Running"] and not paused()
        assert c.wd_record()["status"] == "ARMED"
