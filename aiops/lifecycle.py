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
from aiops.vllm import VllmClient

EXIT_RUNNING, EXIT_STOPPED = 0, 3


class StateCorrupt(ValueError):
    pass


# -- state file ------------------------------------------------------------------------------

def proc_start(pid):
    """The process start time (/proc stat field 22): identifies a process across PID reuse."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()[19]


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


# -- start -----------------------------------------------------------------------------------

def start(config_path, stop_event, out=print, doctor=run_doctor, build=build_engine,
          service_cls=Service):
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
    try:
        service = service_cls(engine, port=cp["port"], interval=cp["interval"], info=info)
    except OSError as e:
        out(f"refusing to start: cannot bind 127.0.0.1:{cp['port']}: {e} (is the port in use?)")
        return 1

    state = {**info, "proc_start": proc_start(os.getpid()), "port": cp["port"],
             "db": cp["db"], "config": profile["config_path"],
             "started_at": datetime.now(timezone.utc).isoformat()}
    if not _claim(state_path, state):
        service.server.server_close()
        out("already running (another control plane claimed the state file first)")
        return 0
    try:
        service.start()
        out(f"started: profile {profile['name']}, workload {profile['workload']['name']!r}, "
            f"pid {os.getpid()}, API http://127.0.0.1:{cp['port']}")
        stop_event.wait()
    finally:
        service.stop()
        _release(state_path)
    out("stopped")
    return 0


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
        out("already stopped")
        return 0
    if not is_running(state):
        state_path.unlink(missing_ok=True)
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
    lines.append(f"audit integrity:    {s['audit_integrity']}")
    return "\n".join(lines)


def _gpu_line(g):
    return (f"{g['uuid']}, {g['memory_used_mib']}/{g['memory_total_mib']} MiB, "
            f"{g['temperature_c']:.0f} C, util {g['utilization_percent']:.0f}%")
