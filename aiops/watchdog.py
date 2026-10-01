"""Independent control-plane watchdog (PRD §14, §42-44; ADR-022).

A safety boundary, not an AIOps component. It imports nothing from aiops (and is spawned as
`python -I aiops/watchdog.py`), so it keeps working when the engine, its database, vLLM or
Prometheus are stuck, broken or wrong. It reads its budgets from the operator's profile file
itself; nothing at runtime can change them.

It has exactly one action: signal the ONE process whose identity it armed on (SIGTERM, then
SIGKILL only if that is ignored for the grace period). It cannot run commands, restart a
workload, touch incident state or take instructions: its only inputs are the command line
(a process identity and a config path), the profile file, nvidia-smi and /proc.
"""
import argparse
import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_CONFIG, EXIT_IDENTITY, EXIT_SENSORS, EXIT_STATE, EXIT_ABORTED = 0, 2, 3, 4, 5, 10
PID_MAX = 2**22
# Linux pidfd system calls (same numbers on x86_64 and aarch64). Used only when this Python's
# os/signal modules lack the wrappers (e.g. conda builds); the kernel needs to be >= 5.3.
SYS_PIDFD_OPEN = 434
SYS_PIDFD_SEND_SIGNAL = 424
PROFILES = ("docker-real-gpu", "kubernetes")


# -- sensors (independent of the engine's telemetry) -------------------------------------------

def check(sample, limits):
    """Names of signals above their max_<signal> limit. Raises KeyError if one is missing."""
    return [key[len("max_"):] for key, limit in limits.items()
            if sample[key[len("max_"):]] > limit]


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


# -- process identity: never a bare PID --------------------------------------------------------

class IdentityError(Exception):
    pass


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _raise_errno():
    errno = ctypes.get_errno()
    raise OSError(errno, os.strerror(errno))   # ESRCH -> ProcessLookupError, EPERM -> PermissionError


def _pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    fd = _libc().syscall(ctypes.c_long(SYS_PIDFD_OPEN), ctypes.c_long(pid), ctypes.c_long(0))
    if fd < 0:
        _raise_errno()
    return fd


def _pidfd_send_signal(fd, sig):
    if hasattr(signal, "pidfd_send_signal"):
        return signal.pidfd_send_signal(fd, sig)
    result = _libc().syscall(ctypes.c_long(SYS_PIDFD_SEND_SIGNAL), ctypes.c_long(fd),
                             ctypes.c_long(sig), ctypes.c_void_p(None), ctypes.c_long(0))
    if result < 0:
        _raise_errno()


def _read_stat(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = text.rsplit(")", 1)[1].split()
    return fields[0], fields[19]          # (state, start time in clock ticks since boot)


def proc_start(pid):
    """The process start time: with the boot id it identifies a process across PID reuse."""
    stat = _read_stat(pid)
    return stat[1] if stat else None


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def _valid_pid(pid):
    return type(pid) is int and 1 < pid <= PID_MAX


def identity_of(pid):
    if not _valid_pid(pid):
        raise IdentityError(f"invalid pid {pid!r}")
    stat, boot = _read_stat(pid), boot_id()
    if stat is None or stat[0] in ("Z", "X"):
        raise IdentityError(f"no live process {pid}")
    if boot is None:
        raise IdentityError("cannot read the boot id")
    return {"pid": pid, "start": stat[1], "boot_id": boot}


def pidfd_supported():
    """(ok, detail): can this host give the watchdog a stable process handle?"""
    try:
        os.close(_pidfd_open(os.getpid()))
        return True, "pidfd available"
    except OSError as e:
        return False, f"pidfd unavailable ({e})"


class Protected:
    """A handle on exactly one process, bound by a pidfd so a recycled PID can never be hit."""

    def __init__(self, identity):
        if not isinstance(identity, dict):
            raise IdentityError("identity must be a mapping")
        pid, start, boot = identity.get("pid"), identity.get("start"), identity.get("boot_id")
        if not _valid_pid(pid) or not isinstance(start, str) or not isinstance(boot, str):
            raise IdentityError(f"malformed identity {identity!r}")
        if pid == os.getpid():
            raise IdentityError("the watchdog cannot protect itself")
        if boot != boot_id():
            raise IdentityError("boot id mismatch: the identity is from another boot")
        stat = _read_stat(pid)
        if stat is None or stat[1] != start:
            raise IdentityError("process identity mismatch (pid reused or stale)")
        if stat[0] in ("Z", "X"):
            raise IdentityError("process has already exited")
        try:
            fd = _pidfd_open(pid)
        except OSError as e:
            raise IdentityError(f"cannot open a stable process handle ({e}); refusing to arm") from e
        again = _read_stat(pid)
        if again is None or again[1] != start or again[0] in ("Z", "X"):
            os.close(fd)
            raise IdentityError("process identity changed while opening its handle")
        self.pid, self.identity, self._fd = pid, dict(identity), fd

    def alive(self):
        if self._fd is None:
            return False
        readable, _, _ = select.select([self._fd], [], [], 0)   # a pidfd is readable once exited
        return not readable

    def signal(self, sig):
        """Signal exactly the process this handle was opened on. False if it already exited."""
        if self._fd is None:
            return False
        try:
            _pidfd_send_signal(self._fd, sig)
            return True
        except ProcessLookupError:
            return False

    def terminate(self, grace):
        """SIGTERM, then SIGKILL only if it is still alive after the grace period."""
        action = "SIGTERM"
        try:
            if not self.signal(signal.SIGTERM):
                return {"action": action, "exited": True}
            deadline = time.monotonic() + grace
            while self.alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.alive():
                action = "SIGKILL"
                self.signal(signal.SIGKILL)
                deadline = time.monotonic() + 5
                while self.alive() and time.monotonic() < deadline:
                    time.sleep(0.05)
        except PermissionError as e:
            return {"action": action + "_FAILED", "exited": False, "error": str(e)}
        return {"action": action, "exited": not self.alive()}

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


# -- budgets and the guard loop ----------------------------------------------------------------

def evaluate(sample, limits, runtime):
    """Names of exceeded budgets. The runtime budget needs no sensor, so it always applies."""
    sensor_limits = {k: v for k, v in limits.items() if k != "max_runtime_seconds"}
    names = check(sample, sensor_limits) if sample is not None else []
    if "max_runtime_seconds" in limits and runtime > limits["max_runtime_seconds"]:
        names.append("runtime_seconds")
    return names


def guard(protected, limits, sampler, interval, grace, max_sensor_failures, on_cycle=None,
          stop=None, sleep=time.sleep, clock=time.monotonic, on_abort=None):
    """Watch one process; fail closed. Returns DISARMED, PROTECTED_EXITED or ABORTED."""
    limits = dict(limits)                  # the armed copy: later changes by anyone have no effect
    needed = [k[len("max_"):] for k in limits if k != "max_runtime_seconds"]
    started, failures = clock(), 0
    try:
        while True:
            if stop is not None and stop():
                return {"outcome": "DISARMED"}
            if not protected.alive():
                return {"outcome": "PROTECTED_EXITED"}
            sample = error = None
            try:
                reading = sampler()
                if not isinstance(reading, dict):
                    raise ValueError(f"sensor reading is {type(reading).__name__}, not a mapping")
                missing = [k for k in needed if k not in reading]
                if missing:
                    raise KeyError(f"sensor reading lacks {missing}")
                if any(type(reading[k]) not in (int, float) for k in needed):
                    raise ValueError("sensor reading has a non-numeric value")
                sample = dict(reading)
            except Exception as e:         # unreadable telemetry is a failure, never a pass
                error = f"{type(e).__name__}: {e}"
            failures = failures + 1 if sample is None else 0
            runtime = clock() - started
            violations = evaluate(sample, limits, runtime)
            if failures >= max_sensor_failures:
                violations.append("sensor_failed")
            if sample is not None:
                sample["runtime_seconds"] = runtime
            if on_cycle is not None:
                on_cycle(sample, failures, error)
            if violations:
                if on_abort is not None:       # record the decision first; it must never block it
                    try:
                        on_abort(violations, sample)
                    except Exception:
                        pass
                return {"outcome": "ABORTED", "reason": violations, "sample": sample,
                        **protected.terminate(grace)}
            sleep(interval)
    except Exception as e:                 # the watchdog itself failing must not disarm the boundary
        return {"outcome": "ABORTED", "reason": ["watchdog_error"],
                "error": f"{type(e).__name__}: {e}", **protected.terminate(grace)}


# -- configuration: read from the operator's profile file, never from the engine ---------------

class ConfigError(Exception):
    pass


def _int(table, key, default, lo, hi):
    value = table.get(key, default)
    if type(value) is not int or not lo <= value <= hi:
        raise ConfigError(f"{key} must be an integer between {lo} and {hi}")
    return value


def _positive(table, key, default):
    value = table.get(key, default)
    if type(value) not in (int, float) or value <= 0:
        raise ConfigError(f"{key} must be a number > 0")
    return value


def load_config(path):
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    name = raw.get("profile")
    if name not in PROFILES:
        raise ConfigError(f"unknown profile {name!r}")
    tables = {k: raw.get(k, {}) for k in ("control_plane", "safety", "watchdog", "gpu")}
    if not all(isinstance(t, dict) for t in tables.values()):
        raise ConfigError("sections must be tables")
    safety, wd = tables["safety"], tables["watchdog"]
    gpu = name == "docker-real-gpu"
    limits = {"max_ram_percent": _int(safety, "max_ram_percent", 90, 1, 100),
              "max_runtime_seconds": _int(safety, "max_runtime_seconds", 86400, 1, 10**7)}
    if gpu:
        limits["max_gpu_memory_percent"] = _int(safety, "max_gpu_memory_percent", 85, 1, 100)
        limits["max_temperature_c"] = _int(safety, "max_temperature_c", 80, 1, 120)
    state_file = tables["control_plane"].get("state_file", "aiops.state.json")
    if not isinstance(state_file, str) or not state_file.strip():
        raise ConfigError("state_file must be a non-empty string")
    state = Path(state_file)
    state = state if state.is_absolute() else Path(path).resolve().parent / state
    return {"limits": limits, "interval": _positive(wd, "interval", 1.0),
            "grace": _positive(wd, "term_grace_seconds", 20),
            "max_sensor_failures": _int(wd, "max_sensor_failures", 3, 1, 10),
            "gpu_index": _int(tables["gpu"], "index", 0, 0, 64) if gpu else None,
            "state_path": f"{state}.watchdog"}


# -- the watchdog process ----------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_state(path, state):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def build_parser():
    p = argparse.ArgumentParser(
        prog="watchdog", description="Independent watchdog for ONE process identity. "
        "It can only terminate that process when a configured safety budget is exceeded.")
    p.add_argument("--config", required=True, help="the operator's runtime profile (TOML)")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--proc-start", required=True, help="the process start time (identity)")
    p.add_argument("--boot-id", required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        conf = load_config(args.config)
    except ConfigError as e:
        print(f"watchdog: configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    identity = {"pid": args.pid, "start": args.proc_start, "boot_id": args.boot_id}
    try:
        protected = Protected(identity)
    except IdentityError as e:
        print(f"watchdog: cannot establish the protected process identity: {e}; "
              "refusing to arm", file=sys.stderr)
        return EXIT_IDENTITY
    gpu_index = conf["gpu_index"]
    sampler = (lambda: {"ram_percent": _ram_percent()}) if gpu_index is None else (
        lambda: read_sensors(gpu_index=gpu_index))
    try:
        sampler()
    except Exception as e:
        print(f"watchdog: sensors unavailable ({type(e).__name__}: {e}); refusing to arm",
              file=sys.stderr)
        protected.close()
        return EXIT_SENSORS

    stopping = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(now=True))   # disarm
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    path = conf["state_path"]
    base = {"pid": os.getpid(), "proc_start": proc_start(os.getpid()), "protected": identity,
            "limits": conf["limits"], "interval": conf["interval"], "armed_at": _now()}
    try:
        _write_state(path, {**base, "status": "ARMED", "last_check_at": _now(),
                            "last_sample": None, "consecutive_sensor_failures": 0})
    except OSError as e:
        print(f"watchdog: cannot write its state file: {e}; refusing to arm", file=sys.stderr)
        protected.close()
        return EXIT_STATE

    def heartbeat(sample, failures, error):
        _write_state(path, {**base, "status": "ARMED", "last_check_at": _now(),
                            "last_sample": sample, "consecutive_sensor_failures": failures,
                            "last_error": error})

    def aborting(reason, sample):
        _write_state(path, {**base, "status": "ABORTING", "at": _now(), "reason": reason,
                            "sample": sample})

    result = guard(protected, conf["limits"], sampler, conf["interval"], conf["grace"],
                   conf["max_sensor_failures"], on_cycle=heartbeat,
                   stop=lambda: stopping["now"], on_abort=aborting)
    protected.close()
    if result["outcome"] != "ABORTED":
        Path(path).unlink(missing_ok=True)        # disarmed, or the protected process ended
        return EXIT_OK
    final = {**base, "status": "ABORTED", "at": _now(), "reason": result["reason"],
             "action": result["action"], "exited": result["exited"],
             "sample": result.get("sample"), "error": result.get("error")}
    try:
        _write_state(path, final)
    except OSError:
        pass
    print(f"watchdog: ABORTED the protected process ({', '.join(result['reason'])}); "
          f"{result['action']}, exited={result['exited']}", file=sys.stderr)
    return EXIT_ABORTED


if __name__ == "__main__":
    sys.exit(main())
