import subprocess
import sys

import pytest

from aiops.gpu_fault import DEFAULT_LIMITS, run_gpu_pressure, validate


@pytest.mark.parametrize("budget_mib, hold_seconds", [
    (4096, 60), (1, 1), (1024, 10),
])
def test_requests_within_bounds_are_accepted(budget_mib, hold_seconds):
    validate(budget_mib, hold_seconds)


@pytest.mark.parametrize("budget_mib, hold_seconds", [
    (4097, 10),      # over the 4 GiB budget cap
    (0, 10),
    (-5, 10),
    (1024, 0),       # a timeout is mandatory
    (1024, -1),
    (1024, 61),      # over the 60 s cap
    (None, 10),
    (1024, None),
    ("1024", 10),
    (1024.5, 10),
])
def test_unsafe_or_missing_bounds_are_rejected(budget_mib, hold_seconds):
    with pytest.raises(ValueError):
        validate(budget_mib, hold_seconds)


def test_default_watchdog_limits_match_the_agreed_safety_budget():
    assert DEFAULT_LIMITS == {"max_gpu_memory_percent": 85, "max_gpu_percent": 90,
                              "max_temperature_c": 80, "max_ram_percent": 90}


def test_launcher_rejects_unsafe_request_before_spawning_anything(tmp_path):
    def must_not_run(*a, **kw):
        raise AssertionError("spawned a process for an invalid request")

    with pytest.raises(ValueError):
        run_gpu_pressure(99999, 10, tmp_path / "r.json", supervise_fn=must_not_run)


def test_launcher_runs_the_child_under_the_watchdog_with_fixed_argv(tmp_path):
    seen = {}

    def fake_supervise(argv, limits, sampler, interval, max_seconds, **kw):
        seen.update(argv=argv, limits=limits, max_seconds=max_seconds)
        return {"outcome": "COMPLETED", "reason": [], "returncode": 0, "pid": 1}

    result = run_gpu_pressure(1024, 10, tmp_path / "r.json", supervise_fn=fake_supervise)
    assert seen["argv"][:3] == [sys.executable, "-m", "aiops.gpu_fault"]
    assert "--budget-mib" in seen["argv"] and "1024" in seen["argv"]
    assert seen["limits"] == DEFAULT_LIMITS
    assert 10 < seen["max_seconds"] <= 60 + 60  # bounded, with startup margin
    assert result["outcome"] == "COMPLETED" and result["report"] is None


def child(*args):
    return subprocess.run([sys.executable, "-m", "aiops.gpu_fault", *args],
                          capture_output=True, text=True, timeout=30)


def test_child_refuses_an_oversized_budget_before_touching_cuda():
    r = child("--budget-mib", "999999", "--hold-seconds", "5", "--report", "/dev/null")
    assert r.returncode != 0 and "budget" in r.stderr.lower()


def test_child_requires_an_explicit_hold_time():
    r = child("--budget-mib", "512", "--report", "/dev/null")
    assert r.returncode != 0
