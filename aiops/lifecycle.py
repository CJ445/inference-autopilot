"""Explicit lifecycle: start, stop, status. No daemon, no service manager, no second process.

`start` runs the existing Service (tick loop + API) in the FOREGROUND and records a state file;
`stop` signals that process; `status` reports observed infrastructure, never assumed state.
The control plane does not create or own the managed workload (see PRD ADR-021).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from aiops.doctor import (FAIL, WARN, ReadOnlyRun, blocking_failures, format_results,
                          run_doctor)
from aiops.docker import DockerProvider
from aiops.engine import CLOSED
from aiops.gpu import read_gpu
from aiops.kubectl import ClusterError, KubernetesProvider
from aiops.profile import ProfileError, load_profile
from aiops.prometheus import TelemetryError
from aiops.runtime import build_engine
from aiops.serve import Service
from aiops.store import AuditTampered, Store, StoreUnavailable
from aiops.practice import PracticeHost
from aiops.system import System
from aiops.vllm import VllmClient
from aiops import watchdog as watchdog_module
from aiops.watchdog import boot_id, proc_start

EXIT_RUNNING, EXIT_STOPPED, EXIT_UNPROTECTED = 0, 3, 4
ARM_TIMEOUT_SECONDS = 15
PREVIOUS_WATCHDOG_WAIT_SECONDS = 5
WATCHDOG_FILE = Path(watchdog_module.__file__)


class StateCorrupt(ValueError):
    pass


# -- state file ------------------------------------------------------------------------------

def read_state(path):
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    try:
        state = json.loads(text)
        if not isinstance(state, dict) or type(state.get("pid")) is not int:
            raise ValueError("not a state record")
    except ValueError as e:
        raise StateCorrupt(f"state file {path} is unreadable: {e}") from e
    return state


def is_running(state):
    started = proc_start(state["pid"])
    return started is not None and started == state.get("proc_start")


def _claim(path, state):
    """Atomically create the state file; False if another control plane already holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def _release(path):
    state = read_state(path)
    if state and state["pid"] == os.getpid():
        path.unlink(missing_ok=True)


# -- watchdog: an independent process whose lifetime is tied to this invocation -----------------

def watchdog_state_path(profile):
    return Path(profile["control_plane"]["state_file"] + ".watchdog")


def _read_watchdog(path):
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(text)
        if not isinstance(record, dict) or not isinstance(record.get("status"), str):
            raise ValueError("not a watchdog record")
    except ValueError as e:
        raise StateCorrupt(f"watchdog state file {path} is unreadable: {e}") from e
    return record


def _watchdog_alive(record):
    pid = record.get("pid")
    return type(pid) is int and proc_start(pid) is not None and proc_start(pid) == record.get(
        "proc_start")


class WatchdogProcess:
    """The watchdog child of this `aiops start`. Dies with the invocation; never resurrected."""

    def __init__(self, popen, state_path):
        self._p, self.state_path, self.pid = popen, Path(state_path), popen.pid

    def wait_armed(self, timeout):
        """True only when THIS watchdog published ARMED: a stale record cannot pass for it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._p.poll() is not None:
                return False
            try:
                record = _read_watchdog(self.state_path)
            except StateCorrupt:
                record = None
            if record and record["status"] == "ARMED" and record.get("pid") == self.pid:
                return True
            time.sleep(0.05)
        return False

    def poll(self):
        return self._p.poll()

    def disarm(self, timeout=5):
        try:
            record = _read_watchdog(self.state_path)
        except StateCorrupt:
            record = None
        if record and record["status"] == "ABORTING" and record.get("pid") == self.pid:
            return          # mid-abort: it waits for THIS process to exit and must record ABORTED;
                            # signalling or waiting on it would deadlock the shutdown
        if self._p.poll() is None:
            self._p.send_signal(signal.SIGTERM)      # the watchdog's disarm: it exits quietly
            try:
                self._p.wait(timeout)
            except subprocess.TimeoutExpired:
                self._p.kill()                       # our own child, never the control plane
                self._p.wait()
        try:
            record = _read_watchdog(self.state_path)
        except StateCorrupt:
            return
        if record and record["status"] == "ARMED" and record.get("pid") == self.pid:
            self.state_path.unlink(missing_ok=True)   # an ABORTED record stays as evidence


def spawn_watchdog(profile, protected_pid=None):
    """Start the watchdog as `python -I aiops/watchdog.py` (no aiops package, no engine code).

    It reads the budgets from the profile file itself. The identity handed to it is
    (pid, start time, boot id); the watchdog verifies it and refuses to arm if it cannot.
    """
    pid = os.getpid() if protected_pid is None else protected_pid
    popen = subprocess.Popen(
        [sys.executable, "-I", str(WATCHDOG_FILE), "--config", profile["config_path"],
         "--pid", str(pid), "--proc-start", proc_start(pid) or "0",
         "--boot-id", boot_id() or "unknown"],
        start_new_session=True, stdin=subprocess.DEVNULL)   # terminal signals never reach it
    return WatchdogProcess(popen, watchdog_state_path(profile))


def _clear_previous_watchdog(profile, out):
    """Reject a previous run's record before arming. False if a live watchdog still holds it."""
    path = watchdog_state_path(profile)
    try:
        record = _read_watchdog(path)
    except StateCorrupt as e:
        out(f"refusing to start: {e}; inspect it and remove it if no watchdog is running")
        return False
    if record is None:
        return True
    if record["status"] == "ARMED" and _watchdog_alive(record):
        deadline = time.monotonic() + PREVIOUS_WATCHDOG_WAIT_SECONDS   # it exits once its
        while _watchdog_alive(record) and time.monotonic() < deadline:  # process is gone
            time.sleep(0.1)
        if _watchdog_alive(record):
            out(f"refusing to start: a previous watchdog (pid {record['pid']}) is still running")
            return False
    out(f"clearing the previous run's watchdog record ({record['status']})")
    path.unlink(missing_ok=True)
    return True


# -- start -----------------------------------------------------------------------------------

def start(config_path, stop_event, out=print, doctor=run_doctor, build=build_engine,
          service_cls=Service, spawn_watchdog=spawn_watchdog):
    try:
        profile = load_profile(config_path)
    except ProfileError as e:
        out(f"configuration error: {e}")
        return 2
    cp = profile["control_plane"]
    state_path = Path(cp["state_file"])
    try:
        previous = read_state(state_path)
    except StateCorrupt as e:
        out(f"refusing to start: {e}; inspect it and remove it if no control plane is running")
        return 1
    if previous and is_running(previous):
        out(f"already running (pid {previous['pid']}, port {previous['port']})")
        return 0
    if previous:
        out(f"removing stale state from a previous run (pid {previous['pid']} is gone)")
        state_path.unlink(missing_ok=True)

    results = doctor(profile)
    blockers = blocking_failures(results)
    notes = [r for r in results if r["status"] in (FAIL, WARN) and r not in blockers]
    if blockers:
        out(format_results(blockers + notes))
        out(f"refusing to start: {len(blockers)} blocking prerequisite(s) failed "
            "(run `aiops doctor` for the full report)")
        return 1
    if notes:
        out(format_results(notes))

    if not _clear_previous_watchdog(profile, out):
        return 1

    try:
        engine = build(profile)
    except StoreUnavailable as e:
        out(f"refusing to start: persistence unavailable: {e}")
        return 1
    except Exception as e:
        out(f"refusing to start: cannot build the control plane: {type(e).__name__}: {e}")
        return 1
    info = {"profile": profile["name"], "provider": profile["provider"],
            "workload": profile["workload"]["name"], "pid": os.getpid()}
    if profile["workload"].get("model"):
        info["model"] = profile["workload"]["model"]
    # The API's confirmed stop is this very event: the one `aiops stop` sets via SIGTERM.
    system = System(profile, request_stop=stop_event.set, doctor=doctor)
    try:
        service = service_cls(engine, port=cp["port"], interval=cp["interval"], info=info,
                              status_extra=lambda: {"watchdog": _watchdog_status(profile)},
                              system=system, practice=PracticeHost())
    except OSError as e:
        out(f"refusing to start: cannot bind 127.0.0.1:{cp['port']}: {e} (is the port in use?)")
        return 1

    started_at = datetime.now(timezone.utc).isoformat()
    info["started_at"] = started_at
    state = {**info, "proc_start": proc_start(os.getpid()), "port": cp["port"],
             "db": cp["db"], "config": profile["config_path"]}
    if not _claim(state_path, state):
        service.server.server_close()
        out("already running (another control plane claimed the state file first)")
        return 0
    try:
        watchdog = spawn_watchdog(profile)
    except Exception as e:
        out(f"refusing to start: cannot start the watchdog: {type(e).__name__}: {e}")
        service.server.server_close()
        _release(state_path)
        return 1
    if not watchdog.wait_armed(ARM_TIMEOUT_SECONDS):
        out("refusing to start: the watchdog did not arm, and the control plane must not run "
            "unprotected (its message is above)")
        watchdog.disarm()
        service.server.server_close()
        _release(state_path)
        return 1

    code = 0
    try:
        service.start()
        out(f"started: profile {profile['name']}, workload {profile['workload']['name']!r}, "
            f"pid {os.getpid()}, API http://127.0.0.1:{cp['port']}, watchdog pid {watchdog.pid}")
        while not stop_event.wait(1.0):
            if watchdog.poll() is not None:
                out(f"the watchdog exited unexpectedly (code {watchdog.poll()}); stopping: the "
                    "control plane must not run unprotected")
                code = EXIT_UNPROTECTED
                break
    finally:
        service.stop()
        watchdog.disarm()
        _release(state_path)
    try:
        record = _read_watchdog(watchdog_state_path(profile))
    except StateCorrupt:
        record = None
    if code == 0 and record and record["status"] in ("ABORTING", "ABORTED") and (
            record.get("protected") or {}).get("pid") == os.getpid():
        out("stopped by the watchdog: safety budget exceeded "
            f"({', '.join(record.get('reason') or ['unknown'])}); see `aiops status`")
        code = EXIT_UNPROTECTED
    out("stopped")
    return code


def _drop_stale_watchdog_record(profile):
    """A record whose watchdog process is gone protects nothing; ABORTED records stay."""
    path = watchdog_state_path(profile)
    try:
        record = _read_watchdog(path)
    except StateCorrupt:
        return
    if record and record["status"] == "ARMED" and not _watchdog_alive(record):
        path.unlink(missing_ok=True)


# -- stop ------------------------------------------------------------------------------------

def stop(config_path, out=print, kill=os.kill, wait_seconds=15, poll_seconds=0.05):
    try:
        profile = load_profile(config_path)
    except ProfileError as e:
        out(f"configuration error: {e}")
        return 2
    state_path = Path(profile["control_plane"]["state_file"])
    try:
        state = read_state(state_path)
    except StateCorrupt as e:
        out(f"cannot stop: {e}")
        return 1
    if state is None:
        _drop_stale_watchdog_record(profile)
        out("already stopped")
        return 0
    if not is_running(state):
        state_path.unlink(missing_ok=True)
        _drop_stale_watchdog_record(profile)
        out(f"already stopped (removed stale state for pid {state['pid']})")
        return 0

    kill(state["pid"], signal.SIGTERM)       # graceful only: never SIGKILL a control plane
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not state_path.exists() or not is_running(state):
            state_path.unlink(missing_ok=True)
            out(f"stopped (pid {state['pid']})")
            return 0
        time.sleep(poll_seconds)
    out(f"pid {state['pid']} did not exit within {wait_seconds}s; not forcing it "
        "(a remediation may be in flight)")
    return 1


# -- status ----------------------------------------------------------------------------------

def _control_plane(state_path):
    try:
        state = read_state(state_path)
    except StateCorrupt as e:
        return {"state": "unknown", "error": str(e)}, None
    if state is None:
        return {"state": "stopped"}, None
    if not is_running(state):
        return {"state": "stale", "pid": state["pid"]}, None
    return {"state": "running", "pid": state["pid"], "port": state["port"],
            "started_at": state.get("started_at")}, state


def _last_telemetry(cp, state):
    if state is None:
        return {"available": False, "reason": f"control plane {cp['state']}"}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{state['port']}/api/v1/status",
                                    timeout=3) as r:
            body = json.load(r)
    except (OSError, ValueError) as e:
        return {"available": False, "error": f"control plane API unreachable: {e}"}
    at = body.get("last_observed_at")
    if at is None:
        return {"available": False, "health": body.get("health"),
                "reason": "no observation yet"}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds()
    return {"available": True, "observed_at": at, "age_seconds": round(age, 1),
            "health": body.get("health")}


def _watchdog_status(profile):
    try:
        record = _read_watchdog(watchdog_state_path(profile))
    except StateCorrupt as e:
        return {"state": "unknown", "error": str(e)}
    if record is None:
        return {"state": "not running"}
    status_ = record["status"]
    if status_ in ("ABORTING", "ABORTED"):
        return {"state": status_.lower(), "reason": record.get("reason"),
                "action": record.get("action"), "at": record.get("at")}
    if status_ != "ARMED":
        return {"state": "unknown", "error": f"unrecognised watchdog status {status_!r}"}
    if not _watchdog_alive(record):
        return {"state": "stale", "pid": record.get("pid"),
                "note": "the watchdog process is gone; nothing is protecting the control plane"}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["last_check_at"])
           ).total_seconds()
    return {"state": "stalled" if age > max(5 * float(record.get("interval", 1.0)), 5.0)
            else "armed", "pid": record["pid"], "protected_pid": record["protected"]["pid"],
            "limits": record["limits"], "last_sample": record.get("last_sample"),
            "last_check_age_seconds": round(age, 1),
            "consecutive_sensor_failures": record.get("consecutive_sensor_failures", 0)}


def _persisted(profile):
    db = Path(profile["control_plane"]["db"])
    if not db.exists():
        return [], [], "no database"
    try:
        incidents, pending, audit = Store(db, readonly=True).load()
    except AuditTampered:
        return [], [], "TAMPERED: audit hash chain does not verify"
    except StoreUnavailable as e:
        return [], [], f"unreadable: {e}"
    active = [{"id": i.incident_id, "category": i.category, "status": i.status}
              for i in incidents if i.status not in CLOSED]
    proposals = [{"incident_id": i, "action": p["action"], "workload": p["parameters"]["workload"]}
                 for i, p in pending.items()]
    return active, proposals, f"valid ({len(audit.events)} events)"


def status(config_path, run=subprocess.run, which=shutil.which):
    """Everything about infrastructure here is observed now, read-only."""
    profile = load_profile(config_path)
    ro = ReadOnlyRun(run)
    w = profile["workload"]
    cp, state = _control_plane(Path(profile["control_plane"]["state_file"]))
    active, proposals, audit = _persisted(profile)

    if profile["provider"] == "docker":
        try:
            s = DockerProvider(w["name"], run=ro).get_workload(w["name"])
            observed = {"found": True, "identity": s["id"], "ready": s["ready"]}
        except ClusterError as e:
            observed = {"found": False, "error": str(e)}
        try:
            g = read_gpu(run=ro, gpu_index=profile["gpu"]["index"])
            gpu = {"uuid": g["gpu_uuid"],
                   "memory_used_mib": round(g["gpu_memory_used_bytes"] / 2**20),
                   "memory_total_mib": round(g["gpu_memory_total_bytes"] / 2**20),
                   "temperature_c": g["gpu_temperature_c"],
                   "utilization_percent": g["gpu_utilization_percent"]}
        except TelemetryError as e:
            gpu = {"error": str(e)}
        client = VllmClient(w["vllm_url"], w["model"], timeout=3)
        try:
            client.metrics()
            metrics_readable = True
        except TelemetryError:
            metrics_readable = False
        vllm = {"url": w["vllm_url"], "probe": client.probe(), "metrics_readable": metrics_readable}
    else:
        try:
            s = KubernetesProvider(w["namespace"], run=ro, context=w["context"]).get_workload(
                w["name"])
            observed = {"found": True, "identity": s["id"], "ready": s["ready"]}
        except ClusterError as e:
            observed = {"found": False, "error": str(e)}
        gpu = vllm = {"state": "not applicable to the kubernetes profile"}

    return {"control_plane": cp, "profile": profile["name"], "provider": profile["provider"],
            "workload": {"name": w["name"], "observed": observed}, "gpu": gpu, "vllm": vllm,
            "watchdog": _watchdog_status(profile),
            "last_telemetry": _last_telemetry(cp, state), "active_incident": active,
            "pending_proposal": proposals, "audit_integrity": audit}


def format_status(s):
    obs = s["workload"]["observed"]
    workload = (f"found, identity {obs['identity'][:19]}..., ready={obs['ready']}"
                if obs["found"] else f"NOT FOUND ({obs['error']})")
    cp = s["control_plane"]
    lines = [
        f"control plane:      {cp['state']}" + (f" (pid {cp['pid']})" if "pid" in cp else ""),
        f"profile:            {s['profile']}",
        f"provider:           {s['provider']}",
        f"managed workload:   {s['workload']['name']}",
        f"workload observed:  {workload}",
        f"GPU:                {s['gpu'] if 'error' in s['gpu'] or 'state' in s['gpu'] else _gpu_line(s['gpu'])}",
    ]
    v = s["vllm"]
    lines.append("vLLM:               " + (v["state"] if "state" in v else
                 f"probe {'ok' if v['probe']['ok'] else 'FAILED (' + str(v['probe']['error']) + ')'}, "
                 f"metrics {'readable' if v['metrics_readable'] else 'UNREADABLE'}"))
    t = s["last_telemetry"]
    lines.append("last telemetry:     " + (f"{t['observed_at']} ({t['age_seconds']}s ago, "
                 f"health {t['health']})" if t["available"] else
                 f"n/a ({t.get('reason') or t.get('error')})"))
    lines.append("active incident:    " + (", ".join(f"{i['id']} {i['category']} {i['status']}"
                 for i in s["active_incident"]) or "none"))
    lines.append("pending proposal:   " + (", ".join(f"{p['incident_id']} {p['action']} "
                 f"{p['workload']}" for p in s["pending_proposal"]) or "none"))
    lines.append(f"watchdog:           {_watchdog_line(s['watchdog'])}")
    lines.append(f"audit integrity:    {s['audit_integrity']}")
    return "\n".join(lines)


def _gpu_line(g):
    return (f"{g['uuid']}, {g['memory_used_mib']}/{g['memory_total_mib']} MiB, "
            f"{g['temperature_c']:.0f} C, util {g['utilization_percent']:.0f}%")


def _watchdog_line(w):
    if w["state"] in ("armed", "stalled"):
        sample = w.get("last_sample") or {}
        return (f"{w['state']} (pid {w['pid']} protecting {w['protected_pid']}; last check "
                f"{w['last_check_age_seconds']}s ago; "
                + ", ".join(f"{k} {v:.0f}" for k, v in sample.items() if isinstance(v, (int, float)))
                + ")")
    if w["state"] in ("aborted", "aborting"):
        return f"{w['state']} - {', '.join(w.get('reason') or [])} ({w.get('action')}) at {w.get('at')}"
    return w["state"] + (f" ({w['error']})" if "error" in w else (f" ({w['note']})" if "note" in w else ""))
