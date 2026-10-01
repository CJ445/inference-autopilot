"""The operator TUI against the real RTX 4060, real vLLM and the real `aiops start` control plane.
Opt in: AIOPS_GPU=1

aiops start -> TUI attached -> healthy -> pause the workload -> incident appears -> inspect ->
approve THROUGH THE TUI -> real restart -> verification -> RESOLVED -> aiops stop.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from vllm_support import BASE, MODEL, NAME, docker, processes, smi

sys.path.insert(0, str(Path(__file__).parent.parent))
from test_real_watchdog import ControlPlane  # noqa: E402
from test_tui_integration import Term, tui_bin  # noqa: E402,F401  (fixture)

from aiops.docker import DockerProvider  # noqa: E402
from aiops.store import Store  # noqa: E402
from aiops.vllm import VllmClient  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")


def frame(tui_bin, port, *args):
    r = subprocess.run([tui_bin, "--once", "--url", f"http://127.0.0.1:{port}", "--height", "70",
                        *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_the_operator_tui_drives_a_real_remediation_end_to_end(vllm, tmp_path, tui_bin):
    provider = DockerProvider("vllm")
    with ControlPlane(tmp_path) as c:
        c.start()
        id_before = provider.get_workload("vllm")["id"]
        used, total, temp = smi("memory.used,memory.total,temperature.gpu")
        uuid = subprocess.run(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                              capture_output=True, text=True, check=True).stdout.split()[0]
        t = Term(tui_bin, f"http://127.0.0.1:{c.port}", rows=52, cols=132)
        try:
            # -- healthy: real GPU, real vLLM, real safety state, all from the API -------------
            assert t.wait_screen("● CONTROL ONLINE", timeout=30)
            for needle in ["docker-real-gpu", "GPU 0", uuid[:12], "VRAM", "GiB", "● HEALTHY",
                           "Probe ✓", "Metrics ✓", "No active incidents", "● ARMED",
                           "Identity ✓", "Budgets ✓", "AUDIT ✓ VERIFIED", f"vLLM · {MODEL}"]:
                assert t.wait_screen(needle, timeout=20), needle
            healthy = frame(tui_bin, c.port)
            shown_pct = float(re.search(r"VRAM\s+\S+ / \S+ GiB\s+(\d+\.\d)%", healthy).group(1))
            shown_temp = float(re.search(r"TEMP\s+(\d+)°C", healthy).group(1))
            now_used, now_total, now_temp = smi("memory.used,memory.total,temperature.gpu")
            assert abs(shown_pct - 100 * now_used / now_total) <= 3     # the REAL GPU, not a value
            assert abs(shown_temp - now_temp) <= 6
            assert provider.get_workload("vllm")["id"] == id_before

            # -- the fault: the incident appears in the TUI by itself ----------------------------
            docker("pause", NAME)
            mark = t.mark()
            assert t.wait_screen("✗ UNRESPONSIVE", timeout=60)
            assert t.wait_screen("INFERENCE_UNRESPONSIVE")
            assert t.wait_screen("AWAITING APPROVAL")
            assert t.wait_screen("restart_workload")
            assert provider.get_workload("vllm")["id"] == id_before     # nothing restarted

            # -- inspect: evidence, RCA, proposal, policy; no causal claim about memory -----------
            t.send(b"\r")
            for needle in ["· inc_001 ·", "Evidence", "vllm-probe", "FAILED (timeout)",
                           "Deterministic RCA", "Inference requests are failing or timing out", "Remediation",
                           "restart_workload", "workload=vllm", "Policy", "AWAITING APPROVAL",
                           "Verification", "Not started"]:
                assert t.wait_screen(needle, timeout=20), needle
            assert "exhaust" not in t.text().lower().split("inference requests are failing")[-1][:200]

            # -- approve THROUGH THE TUI: a confirmation first, then exactly one server action ----
            mark = t.mark()
            t.send(b"a")
            assert t.wait_screen("Approve restart_workload?")
            assert t.wait_screen("[Enter] Confirm")
            time.sleep(1.5)
            assert provider.get_workload("vllm")["id"] == id_before     # the dialog did nothing
            mark = t.mark()
            t.send(b"\r")
            assert t.wait_screen("Waiting for the server", timeout=20)

            # -- the TUI shows the SERVER's real transitions while it works -------------------------
            assert t.wait_screen("RESOLVED", timeout=240)
            seen = t.text(mark)
            assert "VERIFYING" in seen or "EXECUTING" in seen or "Waiting for the server" in seen
            assert t.wait_screen("Verification", timeout=30)
            for needle in ["✓ Workload identity changed", "✓ GPU observable",
                           "✓ vLLM metrics readable", "✓ Stable window of real completions",
                           "RESULT", "Timeline"]:
                assert t.wait_screen(needle, timeout=30), needle
            final = frame(tui_bin, c.port, "--screen", "detail:inc_001")
            for needle in ["Verification", "✓ Workload identity changed", "RESOLVED", "EXECUTING",
                           "VERIFYING"]:
                assert needle in final, final
            assert "✗" not in final.split("Verification")[1].split("Timeline")[0]

            # -- independent proof, from the real infrastructure and the real audit ----------------
            assert provider.get_workload("vllm")["id"] != id_before     # genuinely restarted
            assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True
            assert json.loads(docker("inspect", NAME))[0]["State"]["Paused"] is False
            incidents, pending, audit = Store(c.dir / "aiops.db", readonly=True).load()
            assert [i.status for i in incidents] == ["RESOLVED"] and pending == {}
            order = [e["event"] for e in audit.events]
            assert audit.verify() and order.index("policy_evaluated") < order.index(
                "approval_granted") < order.index("remediation_started") < order.index(
                "verification_finished")                                # the policy path ran
            assert order.count("approval_granted") == 1                 # one approval, one restart
            wd = c.wd_record()
            assert wd["status"] == "ARMED" and c.proc.poll() is None    # watchdog never interfered
            assert c.status()["watchdog"]["state"] == "armed"

            # -- back on the dashboard the recovered state is the server's too ---------------------
            mark = t.mark()
            t.send(b"\x1b")
            assert t.wait_screen("RECENT", timeout=20)
            assert t.wait_screen("→ RESOLVED", timeout=20)
            assert t.wait_screen("AUDIT ✓ VERIFIED", timeout=20)
            t.send(b"q")
            assert t.proc.wait(timeout=15) == 0
        finally:
            t.close()

        # -- aiops stop: the lifecycle is unchanged by the TUI ---------------------------------------
        assert c.stop().returncode == 0 and c.proc.wait(timeout=30) == 0
        assert not c.wd_state.exists() and not c.state.exists()
        assert c.stray() == []
    assert processes("aiops-tui") == []
