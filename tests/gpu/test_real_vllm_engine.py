"""Real vLLM on the real GPU through the real engine (no remediation). Opt in: AIOPS_GPU=1

Starts a labeled, bounded vLLM v0.10.0 container (CUDA 12.8 build; see PRD ADR-017), guarded by
the watchdog's limits: the container is force-removed if VRAM/temperature/RAM exceed them.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from aiops.docker import DockerProvider
from aiops.engine import Engine
from aiops.gpu import read_gpu
from aiops.gpu_fault import DEFAULT_LIMITS, run_gpu_pressure
from aiops.telemetry import RealTelemetry
from aiops.vllm import VllmClient

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")

from vllm_support import BASE, MODEL, NAME, THRESHOLD, docker  # noqa: E402


# These tests are about the engine, not about GPU utilization. The stressor only allocates memory;
# nvidia-smi reports a transient ~97% utilization while it tears its CUDA context down at the end
# of the hold, which killed a child that had already finished (ABORTED, gpu_percent). Test-local:
# the production default is unchanged, and the memory, temperature, RAM and time limits stay.
STRESSOR_LIMITS = {k: v for k, v in DEFAULT_LIMITS.items() if k != "max_gpu_percent"}


def telemetry():
    return RealTelemetry(read_gpu, VllmClient(BASE, MODEL, timeout=3))


def test_observation_comes_from_the_real_gpu_and_real_vllm(vllm):
    obs = telemetry().metrics()
    smi_uuid = subprocess.run(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                              capture_output=True, text=True, check=True).stdout.split()[0]
    assert obs["gpu_uuid"] == smi_uuid and obs["gpu_uuid"].startswith("GPU-")
    assert 1.5e9 < obs["gpu_memory_used_bytes"] < 4.5e9          # vLLM's ~2.9 GB, not a constant
    assert obs["gpu_memory_total_bytes"] > 8e9
    assert obs["inference_probe_ok"] is True and obs["inference_probe_error"] is None
    assert obs["vllm_metrics_available"] is True
    first = obs["vllm_requests_succeeded_total"]
    assert telemetry().metrics()["vllm_requests_succeeded_total"] > first  # probes are real work


def test_pressure_with_healthy_inference_is_insufficient_then_a_hung_workload_is_diagnosed(
        vllm, tmp_path):
    engine = Engine(telemetry(), DockerProvider("vllm"), {
        "service": "vllm", "workload": "vllm", "gpu_threshold": THRESHOLD,
        "error_rate_limit": 0.05})
    provider = DockerProvider("vllm")
    id_before = provider.get_workload("vllm")["id"]

    result = {}
    stressor = threading.Thread(
        target=lambda: result.update(run_gpu_pressure(2048, 45, tmp_path / "r.json",
                                                  limits=STRESSOR_LIMITS)))
    stressor.start()
    try:
        deadline = time.monotonic() + 90
        while read_gpu()["gpu_memory_used_bytes"] <= THRESHOLD:
            assert time.monotonic() < deadline, "stressor never raised memory above threshold"
            time.sleep(0.5)

        # real pressure, inference still healthy: the engine must NOT propose a restart
        inc = engine.tick()
        assert inc.status == "INSUFFICIENT_EVIDENCE" and engine.pending == {}
        probe = next(e for e in inc.evidence if e["metric"] == "inference_probe")
        assert probe["relation"] == "contradicts" and probe["value"]["ok"] is True

        # the workload hangs (bounded: unpaused in finally); same incident is re-diagnosed
        docker("pause", NAME)
        again = engine.tick()
        assert again is inc and len(engine.incidents) == 1
        assert inc.status == "POLICY_CHECK"
        by_metric = {e["metric"]: e for e in inc.evidence}
        assert by_metric["gpu_memory_used_bytes"]["source"] == "nvidia-smi"
        assert by_metric["gpu_memory_used_bytes"]["value"] > THRESHOLD
        assert by_metric["inference_probe"]["relation"] == "supports"
        assert by_metric["inference_probe"]["value"]["ok"] is False
        assert engine.pending[inc.incident_id] == {
            "action": "restart_workload", "parameters": {"workload": "vllm"}}
        assert provider.get_workload("vllm")["ready"] is False
    finally:
        docker("unpause", NAME, check_rc=False)
        stressor.join(timeout=120)

    assert result["outcome"] == "COMPLETED" and result["report"]["oom_caught"] is True
    assert provider.get_workload("vllm")["id"] == id_before  # no remediation was executed


def test_paused_vllm_is_diagnosed_approved_restarted_and_verified_by_real_inference(vllm):
    provider = DockerProvider("vllm")
    engine = Engine(telemetry(), provider, {
        "service": "vllm", "workload": "vllm", "gpu_threshold": THRESHOLD,
        "timeout": 150, "interval": 2, "stable_probes": 3, "probe_interval": 1.0})
    assert engine.tick() is None and engine.health == "HEALTHY"   # healthy baseline
    id_before = provider.get_workload("vllm")["id"]

    try:
        docker("pause", NAME)                                     # the bounded, reversible fault
        inc = engine.tick()
        assert inc.category == "INFERENCE_UNRESPONSIVE" and inc.status == "POLICY_CHECK"
        assert [i.category for i in engine.incidents] == ["INFERENCE_UNRESPONSIVE"]
        by_metric = {e["metric"]: e for e in inc.evidence}
        assert by_metric["inference_probe"]["source"] == "vllm-probe"
        assert by_metric["inference_probe"]["value"]["ok"] is False
        assert by_metric["inference_probe"]["relation"] == "supports"
        assert by_metric["gpu_memory_used_bytes"]["source"] == "nvidia-smi"
        assert by_metric["gpu_memory_used_bytes"]["value"] < THRESHOLD   # normal: not pressure
        assert by_metric["vllm_metrics"]["value"] == {"available": False}
        assert engine.pending[inc.incident_id] == {
            "action": "restart_workload", "parameters": {"workload": "vllm"}}
        assert provider.get_workload("vllm")["id"] == id_before   # nothing restarted yet

        engine.approve(inc.incident_id)                           # explicit human approval
    finally:
        docker("unpause", NAME, check_rc=False)                   # no-op once restarted

    # judged from independently observed state, not from the engine's own claim
    assert inc.status == "RESOLVED"
    assert provider.get_workload("vllm")["id"] != id_before       # a genuinely new run
    assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True
    assert json.loads(docker("inspect", NAME))[0]["State"]["Paused"] is False
    assert read_gpu()["gpu_memory_used_bytes"] > 1.5e9            # model reloaded on the GPU
    checks = next(e["data"]["checks"] for e in reversed(engine.audit.events)
                  if e["event"] == "verification_finished")
    assert checks == {"workload_restarted": True, "gpu_observable": True,
                      "vllm_metrics_readable": True, "inference_probe_stable": True}
    assert engine.audit.verify()
    print("\nE2E timeline:", [t["state"] for t in inc.timeline])
    print("E2E audit:", [e["event"] for e in engine.audit.events])
    print("E2E checks:", checks)


def test_real_pressure_on_top_of_a_held_hang_opens_no_second_incident_or_proposal(vllm, tmp_path):
    provider = DockerProvider("vllm")
    engine = Engine(telemetry(), provider, {
        "service": "vllm", "workload": "vllm", "gpu_threshold": THRESHOLD,
        "timeout": 150, "interval": 2})
    id_before = provider.get_workload("vllm")["id"]
    result = {}
    stressor = threading.Thread(
        target=lambda: result.update(run_gpu_pressure(2048, 45, tmp_path / "r.json",
                                                  limits=STRESSOR_LIMITS)))
    try:
        docker("pause", NAME)                                   # the hang comes first
        inc = engine.tick()
        assert inc.category == "INFERENCE_UNRESPONSIVE" and inc.status == "POLICY_CHECK"

        stressor.start()                                        # then real memory pressure
        deadline = time.monotonic() + 90
        while read_gpu()["gpu_memory_used_bytes"] <= THRESHOLD:
            assert time.monotonic() < deadline, "stressor never raised memory above threshold"
            time.sleep(0.5)

        for _ in range(2):
            assert engine.tick() is inc                         # the holder, nothing new
        assert len(engine.incidents) == 1 and list(engine.pending) == [inc.incident_id]
        assert [e["data"]["category"] for e in engine.audit.events
                if e["event"] == "condition_suppressed"] == ["GPU_MEMORY_PRESSURE"]
        assert provider.get_workload("vllm")["id"] == id_before
    finally:
        docker("unpause", NAME, check_rc=False)
        stressor.join(timeout=120)

    engine.reject(inc.incident_id)                              # operator declines
    assert inc.status == "REJECTED" and engine.pending == {}
    assert provider.get_workload("vllm")["id"] == id_before     # no restart ever happened
    assert result["outcome"] == "COMPLETED" and engine.audit.verify()
