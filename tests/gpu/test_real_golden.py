"""THE real golden scenario (PRD §71, as redefined by ADR-030). Opt in: AIOPS_GPU=1

    docker pause  ->  real inference probes fail  ->  two consecutive failures (ADR-027)  ->
    incident  ->  evidence  ->  deterministic RCA  ->  restart proposal  ->  policy: approval
    required  ->  operator approval  ->  real restart  ->  the lifecycle identity changes  ->
    real inference recovers  ->  3 consecutive successful completions  ->  RESOLVED  ->  audit

The same `assert_golden` that judges the simulated scenario (tests/test_golden_parity.py) judges
this one, so both are held to identical semantics. Everything that proves recovery here is observed
independently of the control plane: `docker inspect` for the lifecycle identity and the paused flag,
and direct inference requests to vLLM.

GPU memory exhaustion is deliberately NOT the scenario (ADR-018): vLLM preallocates its VRAM, so a
bounded allocator-cap stressor never made it fail.
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
from golden_acceptance import assert_golden  # noqa: E402
from test_real_watchdog import ControlPlane  # noqa: E402

from aiops.docker import DockerProvider  # noqa: E402
from aiops.store import Store  # noqa: E402
from aiops.vllm import VllmClient  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")


def paused():
    return json.loads(docker("inspect", NAME))[0]["State"]["Paused"]


def test_golden_real_docker_pause_scenario(vllm, tmp_path):
    provider = DockerProvider("vllm")
    with ControlPlane(tmp_path) as c:
        c.start()
        # -- a healthy, protected, REAL system ---------------------------------------------------
        wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"], 60, "a real observation")
        time.sleep(5)                                                     # several healthy ticks
        status = http(c.port, "/api/v1/status")
        assert status["mode"] == "REAL" and status["workload"]["state"] == "running"
        assert http(c.port, "/api/v1/incidents")["incidents"] == []
        assert c.wd_record()["status"] == "ARMED"
        identity_before = provider.get_workload("vllm")["id"]            # observed by Docker, not by aiops
        assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True

        # -- the fault: a real, bounded, reversible pause ----------------------------------------
        docker("pause", NAME)
        assert paused()
        incidents = wait_for(lambda: http(c.port, "/api/v1/incidents")["incidents"], 90, "an incident")
        time.sleep(4)                                                     # more ticks: nothing new
        incidents = http(c.port, "/api/v1/incidents")["incidents"]
        assert len(incidents) == 1
        iid = incidents[0]["incident_id"]

        # -- diagnosed, proposed, waiting for the operator; NOTHING has been restarted ------------
        pending = http(c.port, f"/api/v1/incidents/{iid}")
        assert pending["category"] == "INFERENCE_UNRESPONSIVE" and pending["status"] == "POLICY_CHECK"
        assert pending["proposal"] == {"action": "restart_workload", "parameters": {"workload": "vllm"}}
        by_metric = {e["metric"]: e for e in pending["evidence"]}
        assert by_metric["inference_probe"]["value"]["ok"] is False
        assert by_metric["inference_probe"]["source"] == "vllm-probe"
        assert by_metric["gpu_memory_used_bytes"]["source"] == "nvidia-smi"
        assert provider.get_workload("vllm")["id"] == identity_before     # no restart without approval
        assert paused()                                                   # still hung
        assert c.wd_record()["status"] == "ARMED"                         # a hang is not the watchdog's business

        # -- the operator approves; the server executes and verifies -------------------------------
        done = http(c.port, f"/api/v1/incidents/{iid}/remediation/approve", method="POST", timeout=240)
        assert done["status"] == "RESOLVED", done

        # -- recovery, observed independently of the control plane ---------------------------------
        identity_after = provider.get_workload("vllm")["id"]
        assert identity_after != identity_before                          # a genuinely new run
        assert not paused()
        for n in range(3):                                                # 3 consecutive real completions
            probe = VllmClient(BASE, MODEL, timeout=5).probe()
            assert probe["ok"] is True, (n, probe)
            time.sleep(1)

        # -- the same acceptance criteria as the simulation (PRD §72) -------------------------------
        detail = http(c.port, f"/api/v1/incidents/{iid}")
        events = http(c.port, "/api/v1/audit")["events"]
        result = assert_golden(detail, events, identity_before, identity_after, mode="REAL")
        assert all(result.values())
        assert detail["verification"]["checks"] == {
            "workload_restarted": True, "gpu_observable": True, "vllm_metrics_readable": True,
            "inference_probe_stable": True}

        # -- the chain on disk agrees; one incident; the watchdog never interfered -------------------
        stored, pending_left, audit = Store(tmp_path / "aiops.db", readonly=True).load()
        assert audit.verify() and pending_left == {} and [i.status for i in stored] == ["RESOLVED"]
        assert len(http(c.port, "/api/v1/incidents")["incidents"]) == 1
        assert c.wd_record()["status"] == "ARMED" and c.proc.poll() is None

        # -- explicit stop leaves nothing behind (the workload is the operator's, so it stays) ------
        assert c.stop().returncode == 0 and c.proc.wait(timeout=30) == 0
        assert not c.state.exists() and not c.wd_state.exists() and not c.stray()
        assert json.loads(docker("inspect", NAME))[0]["State"]["Running"]
