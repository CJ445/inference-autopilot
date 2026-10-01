"""Real-GPU safety tests. Opt in: AIOPS_GPU=1 pytest tests/gpu

Loads the real GPU, always bounded and always under the watchdog. Not part of the default run.
"""
import os
import shutil
import time

import pytest

from aiops.gpu_fault import DEFAULT_LIMITS, run_gpu_pressure
from aiops.watchdog import read_sensors

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU to run")

RELEASE_TOLERANCE_PERCENT = 4  # CUDA driver bookkeeping after exit


class Recorder:
    """Sampler that also remembers the worst readings the watchdog saw."""

    def __init__(self):
        self.peak = {"gpu_memory_percent": 0, "temperature_c": 0}

    def __call__(self):
        s = read_sensors()
        for k in self.peak:
            self.peak[k] = max(self.peak[k], s[k])
        return s


def wait_until_released(baseline, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        used = read_sensors()["gpu_memory_percent"]
        if used <= baseline + RELEASE_TOLERANCE_PERCENT:
            return used
        time.sleep(0.5)
    return read_sensors()["gpu_memory_percent"]


@pytest.fixture
def baseline():
    b = read_sensors()
    assert b["gpu_memory_percent"] < 20, "GPU is busy; refusing to run GPU fault tests"
    assert b["temperature_c"] < 70, "GPU is hot; refusing to run GPU fault tests"
    return b["gpu_memory_percent"]


def test_small_bounded_pressure_hits_a_real_cuda_oom_and_releases_memory(baseline, tmp_path):
    rec = Recorder()
    r = run_gpu_pressure(512, 3, tmp_path / "r.json", sampler=rec)
    assert r["outcome"] == "COMPLETED" and r["returncode"] == 0, r
    assert r["report"]["oom_caught"] is True  # genuine torch.cuda.OutOfMemoryError
    assert r["report"]["allocated_mib"] <= 512
    assert rec.peak["gpu_memory_percent"] > baseline + 4  # real telemetry saw the pressure
    assert wait_until_released(baseline) <= baseline + RELEASE_TOLERANCE_PERCENT


def test_watchdog_emergency_stop_works_on_real_hardware(baseline, tmp_path):
    limits = {**DEFAULT_LIMITS, "max_gpu_memory_percent": 10}  # deliberately far below budget
    r = run_gpu_pressure(2048, 20, tmp_path / "r.json", limits=limits)
    assert r["outcome"] == "ABORTED" and r["reason"] == ["gpu_memory_percent"], r
    assert r["report"] is None  # killed before it could finish
    assert wait_until_released(baseline) <= baseline + RELEASE_TOLERANCE_PERCENT


def test_full_agreed_budget_stays_within_all_safety_limits(baseline, tmp_path):
    rec = Recorder()
    r = run_gpu_pressure(4096, 10, tmp_path / "r.json", sampler=rec)
    assert r["outcome"] == "COMPLETED", r
    assert r["report"]["oom_caught"] is True
    assert rec.peak["gpu_memory_percent"] < DEFAULT_LIMITS["max_gpu_memory_percent"]
    assert rec.peak["temperature_c"] < DEFAULT_LIMITS["max_temperature_c"]
    assert wait_until_released(baseline) <= baseline + RELEASE_TOLERANCE_PERCENT
    print(f"peak vram {rec.peak['gpu_memory_percent']:.1f}% temp {rec.peak['temperature_c']}C")
