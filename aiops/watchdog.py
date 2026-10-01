"""Independent safety supervisor (PRD §42-43).

Depends on nothing else in aiops: not the engine, Kubernetes, Prometheus, the LLM or the
TUI. It owns the experimental child process and fails closed.
"""
import os
import signal
import subprocess
import time


def check(sample, limits):
    """Names of signals above their max_<signal> limit. Raises KeyError if one is missing."""
    return [key[len("max_"):] for key, limit in limits.items()
            if sample[key[len("max_"):]] > limit]


def supervise(argv, limits, sampler, interval, max_seconds, on_start=None):
    """Run argv in its own process group; kill the whole group on any violation."""
    proc = subprocess.Popen(argv, start_new_session=True)
    if on_start:
        on_start(proc.pid)
    deadline = time.monotonic() + max_seconds
    try:
        while True:
            if proc.poll() is not None:
                return _result("COMPLETED", [], proc)
            try:
                reason = check(sampler(), limits)
            except Exception:  # unreadable sensors mean we cannot vouch for safety
                reason = ["sampler_failed"]
            if not reason and time.monotonic() >= deadline:
                reason = ["max_duration"]
            if reason:
                _kill_group(proc)
                return _result("ABORTED", reason, proc)
            time.sleep(interval)
    finally:
        _kill_group(proc)  # never leave the workload behind, even if we are interrupted


def read_sensors(run=subprocess.run, gpu_index=0):
    """Real readings: GPU via nvidia-smi, RAM via /proc/meminfo. Raises if unreadable."""
    out = run(["nvidia-smi", "-i", str(gpu_index),
               "--query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu",
               "--format=csv,noheader,nounits"],
              capture_output=True, text=True, check=True, timeout=10).stdout
    used, total, temp, util = (float(v) for v in out.strip().split(","))
    return {"gpu_memory_percent": 100 * used / total, "gpu_percent": util,
            "temperature_c": temp, "ram_percent": _ram_percent()}


def _ram_percent():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":")
            info[key] = int(value.split()[0])
    return 100 * (1 - info["MemAvailable"] / info["MemTotal"])


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _result(outcome, reason, proc):
    return {"outcome": outcome, "reason": reason, "returncode": proc.returncode,
            "pid": proc.pid}
