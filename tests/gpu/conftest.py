"""One labeled, bounded vLLM v0.10.0 container (CUDA 12.8 build; PRD ADR-017) for ALL GPU tests,
guarded by the watchdog's limits: force-removed if VRAM/temperature/RAM exceed them.
The control plane does not own this container; the test harness creates and removes it."""
import os
import shutil
import subprocess
import threading
import time

import pytest
from vllm_support import GUARD, IMAGE, MODEL, NAME, docker, state

from aiops.watchdog import check, read_sensors


@pytest.fixture(scope="session")
def vllm():
    if os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"):
        pytest.skip("set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
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
