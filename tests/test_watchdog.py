import os
import sys
import time

from aiops.watchdog import check, supervise

LIMITS = {"max_gpu_memory_percent": 85, "max_gpu_percent": 90,
          "max_temperature_c": 80, "max_ram_percent": 90}
SAFE = {"gpu_memory_percent": 10, "gpu_percent": 5, "temperature_c": 50, "ram_percent": 40}
SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def run(argv=SLEEP, sampler=lambda: SAFE, max_seconds=5, **kw):
    return supervise(argv, LIMITS, sampler, interval=0.02, max_seconds=max_seconds, **kw)


def test_check_reports_nothing_when_within_limits():
    assert check(SAFE, LIMITS) == []


def test_check_names_every_exceeded_limit():
    hot = {**SAFE, "gpu_memory_percent": 86, "temperature_c": 81}
    assert sorted(check(hot, LIMITS)) == ["gpu_memory_percent", "temperature_c"]


def test_limit_exactly_at_threshold_is_allowed():
    assert check({**SAFE, "gpu_memory_percent": 85}, LIMITS) == []


def test_well_behaved_child_completes():
    r = run([sys.executable, "-c", "pass"])
    assert (r["outcome"], r["returncode"]) == ("COMPLETED", 0)


def test_child_is_killed_when_gpu_memory_exceeds_limit():
    r = run(sampler=lambda: {**SAFE, "gpu_memory_percent": 91})
    assert r["outcome"] == "ABORTED" and r["reason"] == ["gpu_memory_percent"]
    assert not alive(r["pid"])


def test_child_is_killed_when_temperature_exceeds_limit():
    r = run(sampler=lambda: {**SAFE, "temperature_c": 95})
    assert r["outcome"] == "ABORTED" and r["reason"] == ["temperature_c"]
    assert not alive(r["pid"])


def test_child_is_killed_at_the_time_budget():
    start = time.monotonic()
    r = run(max_seconds=0.3)
    assert r["outcome"] == "ABORTED" and r["reason"] == ["max_duration"]
    assert time.monotonic() - start < 3 and not alive(r["pid"])


def test_unreadable_sensors_fail_closed():
    def broken():
        raise OSError("nvidia-smi not found")

    r = run(sampler=broken)
    assert r["outcome"] == "ABORTED" and r["reason"] == ["sampler_failed"]
    assert not alive(r["pid"])


def test_sensor_reading_missing_a_limited_signal_fails_closed():
    r = run(sampler=lambda: {"gpu_memory_percent": 10})
    assert r["outcome"] == "ABORTED" and r["reason"] == ["sampler_failed"]
    assert not alive(r["pid"])


def test_grandchildren_die_with_the_child(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(30)")
    r = run([sys.executable, "-c", code], max_seconds=0.5)
    assert r["outcome"] == "ABORTED"
    time.sleep(0.2)
    assert not alive(int(pidfile.read_text()))


def test_child_is_not_left_running_if_supervision_itself_raises():
    pids = []

    def sampler():
        raise KeyboardInterrupt  # supervisor interrupted

    try:
        supervise(SLEEP, LIMITS, sampler, interval=0.02, max_seconds=5,
                  on_start=lambda pid: pids.append(pid))
    except KeyboardInterrupt:
        pass
    time.sleep(0.2)
    assert pids and not alive(pids[0])


def test_nvidia_smi_output_is_parsed_into_percentages():
    from aiops.watchdog import read_sensors

    class R:
        stdout = "1024, 8192, 46, 7\n"

    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return R()

    s = read_sensors(run=fake_run)
    assert s["gpu_memory_percent"] == 12.5
    assert (s["temperature_c"], s["gpu_percent"]) == (46, 7)
    assert 0 < s["ram_percent"] < 100  # real /proc/meminfo
    assert seen[0][0] == "nvidia-smi" and "shell" not in seen[0]


def test_missing_nvidia_smi_raises_so_the_watchdog_fails_closed():
    import pytest

    from aiops.watchdog import read_sensors

    def absent(argv, **kw):
        raise FileNotFoundError("nvidia-smi")

    with pytest.raises(OSError):
        read_sensors(run=absent)
    r = supervise(SLEEP, LIMITS, lambda: read_sensors(run=absent), interval=0.02,
                  max_seconds=5)
    assert r["outcome"] == "ABORTED" and r["reason"] == ["sampler_failed"]


def test_malformed_nvidia_smi_output_raises():
    import pytest

    from aiops.watchdog import read_sensors

    class R:
        stdout = "[N/A], 8192, 46, 7\n"

    with pytest.raises(ValueError):
        read_sensors(run=lambda argv, **kw: R())
