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
from aiops.gpu_fault import run_gpu_pressure
from aiops.telemetry import RealTelemetry
from aiops.vllm import VllmClient
from aiops.watchdog import check, read_sensors

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")

NAME, IMAGE, MODEL = "aiops-vllm", "vllm/vllm-openai:v0.10.0", "facebook/opt-125m"
BASE = "http://127.0.0.1:8001"
GUARD = {"max_gpu_memory_percent": 85, "max_temperature_c": 80, "max_ram_percent": 90}
THRESHOLD = 4_500_000_000  # vLLM alone holds ~2.9 GB; the 2 GiB stressor lifts it to ~5.0 GB


def docker(*args, check_rc=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          check=check_rc).stdout.strip()


def state():
    s = json.loads(docker("inspect", NAME))[0]["State"]
    return s["Running"] and not s["Paused"] and s.get("Health", {}).get("Status") == "healthy"


@pytest.fixture(scope="module")
def vllm():
    image_ok = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    if image_ok.returncode:
        pytest.skip(f"{IMAGE} not pulled")
    baseline = read_sensors()
    assert baseline["gpu_memory_percent"] < 20, "GPU is busy"
    assert baseline["temperature_c"] < 70, "GPU is hot"
    docker("rm", "-f", NAME, check_rc=False)
    stop = threading.Event()

    def guard():
        while not stop.is_set():
            try:
                bad = check(read_sensors(), GUARD)
            except Exception:
                bad = ["sensor_failed"]
            if bad:
                subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
                return
            time.sleep(1)

    threading.Thread(target=guard, daemon=True).start()
    health = ("python3 -c \"import urllib.request;"
              "urllib.request.urlopen('http://localhost:8000/health',timeout=3)\"")
    try:
        docker("run", "-d", "--name", NAME, "--gpus", "device=0",
               "--label", "com.inference-autopilot.managed=true",
               "--label", "com.inference-autopilot.workload=vllm",
               "--memory", "8g", "--cpus", "4", "--shm-size", "1g",
               "-p", "127.0.0.1:8001:8000", "-v", "aiops-hf-cache:/hf", "-e", "HF_HOME=/hf",
               "--health-cmd", health, "--health-interval", "5s", "--health-timeout", "5s",
               "--health-retries", "3", "--health-start-period", "240s",
               IMAGE, "--model", MODEL, "--gpu-memory-utilization", "0.35",
               "--max-model-len", "512", "--enforce-eager")
        deadline = time.monotonic() + 240
        while not state():
            assert time.monotonic() < deadline, "vLLM never became healthy"
            time.sleep(1)
        yield
    finally:
        stop.set()
        docker("unpause", NAME, check_rc=False)
        docker("rm", "-f", NAME, check_rc=False)
        time.sleep(3)
        assert read_sensors()["gpu_memory_percent"] < baseline["gpu_memory_percent"] + 4


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
    engine = Engine(telemetry(), DockerProvider(), {
        "service": "vllm", "workload": "vllm", "gpu_threshold": THRESHOLD,
        "error_rate_limit": 0.05})
    provider = DockerProvider()
    id_before = provider.get_workload("vllm")["id"]

    result = {}
    stressor = threading.Thread(
        target=lambda: result.update(run_gpu_pressure(2048, 45, tmp_path / "r.json")))
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
