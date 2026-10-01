"""The real-GPU acceptance scenario through the PRODUCT lifecycle (bin/aiops). Opt in: AIOPS_GPU=1

doctor -> status -> start (idempotent) -> WATCHDOG ARMED -> pause the managed vLLM -> real probe
fails -> INFERENCE_UNRESPONSIVE -> restart_workload proposal -> explicit approval -> real Docker
restart -> verified by real inference -> RESOLVED (the watchdog never interferes) -> stop ->
WATCHDOG DISARMED (safe twice).
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from vllm_support import BASE, MODEL, NAME, THRESHOLD, docker, processes

from aiops.docker import DockerProvider
from aiops.gpu import read_gpu
from aiops.store import Store
from aiops.vllm import VllmClient

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")

AIOPS = str(Path(__file__).parent.parent.parent / "bin" / "aiops")


def aiops(*args, timeout=120):
    return subprocess.run([AIOPS, *args], capture_output=True, text=True, timeout=timeout)


def http(port, path, method="GET", timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wait_for(fn, timeout, what):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, KeyError) as e:
            last = e
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what} (last error: {last})")


def control_plane_processes(config):
    out = subprocess.run(["pgrep", "-af", "aiops"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if " start " in l and str(config) in l]


def test_the_real_gpu_lifecycle_acceptance_scenario(vllm, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    uuid = subprocess.run(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
                          capture_output=True, text=True, check=True).stdout.split()[0]
    cfg = tmp_path / "aiops.toml"
    cfg.write_text(f'''
profile = "docker-real-gpu"
[control_plane]
port = {port}
db = "aiops.db"
state_file = "aiops.state.json"
interval = 1
[workload]
name = "vllm"
vllm_url = "{BASE}"
model = "{MODEL}"
[gpu]
index = 0
uuid = "{uuid}"
min_memory_mib = 6000
[safety]
gpu_memory_threshold_bytes = {THRESHOLD}
[verification]
timeout = 150
interval = 2
stable_probes = 3
probe_interval = 1
''')
    state_file, db = tmp_path / "aiops.state.json", tmp_path / "aiops.db"
    wd_file = tmp_path / "aiops.state.json.watchdog"
    provider = DockerProvider("vllm")
    server = None
    try:
        # -- doctor: read-only, every real prerequisite passes ------------------------------
        before = sorted(p.name for p in tmp_path.iterdir())
        d = aiops("doctor", "--config", str(cfg), "--json")
        results = {r["check"]: r for r in json.loads(d.stdout)}
        assert d.returncode == 0, d.stdout
        for name in ("python", "database", "state_dir", "docker_cli", "docker_daemon", "workload",
                     "nvidia_driver", "gpu", "gpu_expectations", "cuda_compat", "gpu_headroom",
                     "vllm_endpoint", "vllm_metrics", "vllm_probe", "watchdog_identity",
                     "watchdog_sensors", "watchdog_state"):
            assert results[name]["status"] == "PASS", (name, results[name])
        assert "12.8" in results["cuda_compat"]["detail"]     # computed from the real image
        assert sorted(p.name for p in tmp_path.iterdir()) == before and not db.exists()

        # -- status before start: observed, control plane stopped ---------------------------
        s0 = aiops("status", "--config", str(cfg), "--json")
        st = json.loads(s0.stdout)
        assert s0.returncode == 3 and st["control_plane"]["state"] == "stopped"
        assert st["workload"]["observed"]["found"] and st["workload"]["observed"]["ready"]
        assert st["gpu"]["uuid"] == uuid and st["vllm"]["probe"]["ok"] is True
        id_before = provider.get_workload("vllm")["id"]

        # -- start: foreground process, idempotent ------------------------------------------
        log = open(tmp_path / "start.log", "w")
        server = subprocess.Popen([AIOPS, "start", "--config", str(cfg)], stdout=log, stderr=log)
        wait_for(lambda: state_file.exists(), 30, "state file")
        wait_for(lambda: http(port, "/health")["status"] == "HEALTHY", 30, "control plane")
        again = aiops("start", "--config", str(cfg))
        assert again.returncode == 0 and "already running" in again.stdout
        assert len(control_plane_processes(cfg)) == 1

        s1 = json.loads(aiops("status", "--config", str(cfg), "--json").stdout)
        assert s1["control_plane"]["state"] == "running" and s1["control_plane"]["pid"] == server.pid
        wait_for(lambda: json.loads(aiops("status", "--config", str(cfg), "--json").stdout)[
            "last_telemetry"]["available"], 30, "first real observation")
        time.sleep(3)
        assert http(port, "/api/v1/incidents")["incidents"] == []   # healthy: no incident

        # -- the watchdog is armed on the control plane, as its own independent process -------
        wd0 = json.loads(wd_file.read_text())
        assert wd0["status"] == "ARMED" and wd0["protected"]["pid"] == server.pid
        assert wd0["pid"] != server.pid and wd0["limits"]["max_gpu_memory_percent"] == 85
        assert s1["watchdog"]["state"] == "armed" and s1["watchdog"]["protected_pid"] == server.pid
        wd_pid = wd0["pid"]
        found = processes("watchdog.py", str(cfg))
        assert len(found) == 1 and found[0].split()[0] == str(wd_pid), found

        # -- the fault: pause the managed workload; the real probe fails --------------------
        docker("pause", NAME)
        incidents = wait_for(lambda: http(port, "/api/v1/incidents")["incidents"], 90, "incident")
        time.sleep(4)                                              # several more ticks
        incidents = http(port, "/api/v1/incidents")["incidents"]
        assert len(incidents) == 1                                 # no second incident
        inc = incidents[0]
        assert inc["category"] == "INFERENCE_UNRESPONSIVE" and inc["status"] == "POLICY_CHECK"
        evidence = {e["metric"]: e for e in inc["evidence"]}
        assert evidence["inference_probe"]["source"] == "vllm-probe"
        assert evidence["inference_probe"]["value"]["ok"] is False
        assert evidence["gpu_memory_used_bytes"]["source"] == "nvidia-smi"
        assert evidence["gpu_memory_used_bytes"]["resource"] == uuid
        assert inc["proposal"] == {"action": "restart_workload", "parameters": {"workload": "vllm"}}
        assert provider.get_workload("vllm")["id"] == id_before    # nothing restarted yet

        s2 = json.loads(aiops("status", "--config", str(cfg), "--json").stdout)
        assert [i["id"] for i in s2["active_incident"]] == [inc["incident_id"]]
        assert len(s2["pending_proposal"]) == 1 and s2["audit_integrity"].startswith("valid")
        assert s2["workload"]["observed"]["ready"] is False        # observed, not assumed

        # the fault (a hung workload) is not the watchdog's business: it stays armed and quiet
        assert json.loads(wd_file.read_text())["status"] == "ARMED" and server.poll() is None

        # -- explicit human approval is the only way a restart happens ----------------------
        done = http(port, f"/api/v1/incidents/{inc['incident_id']}/remediation/approve",
                    method="POST", timeout=200)
        assert done["status"] == "RESOLVED", done

        # -- independent verification of real recovery ---------------------------------------
        assert provider.get_workload("vllm")["id"] != id_before    # lifecycle identity changed
        assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True
        assert json.loads(docker("inspect", NAME))[0]["State"]["Paused"] is False
        assert read_gpu()["gpu_memory_used_bytes"] > 1.5e9         # model back on the real GPU
        assert len(http(port, "/api/v1/incidents")["incidents"]) == 1
        incidents_db, pending, audit = Store(db, readonly=True).load()
        assert pending == {} and audit.verify()
        checks = next(e["data"]["checks"] for e in reversed(audit.events)
                      if e["event"] == "verification_finished")
        assert checks == {"workload_restarted": True, "gpu_observable": True,
                          "vllm_metrics_readable": True, "inference_probe_stable": True}
        assert [i.status for i in incidents_db] == ["RESOLVED"]

        # -- the watchdog did not interfere with remediation, and is still protecting ---------
        wd1 = json.loads(wd_file.read_text())
        assert wd1["status"] == "ARMED" and wd1["pid"] == wd_pid and server.poll() is None
        assert wd1["last_check_at"] > wd0["last_check_at"]            # heartbeat kept running
        assert wd1["consecutive_sensor_failures"] == 0
        sample, limits = wd1["last_sample"], wd1["limits"]
        assert sample["gpu_memory_percent"] < limits["max_gpu_memory_percent"]
        assert sample["temperature_c"] < limits["max_temperature_c"]
        assert json.loads(aiops("status", "--config", str(cfg), "--json").stdout)[
            "watchdog"]["state"] == "armed"

        # -- stop: graceful, safe twice, leaves no process behind ---------------------------
        stopped = aiops("stop", "--config", str(cfg))
        assert stopped.returncode == 0 and server.wait(timeout=30) == 0
        assert not state_file.exists() and control_plane_processes(cfg) == []
        assert not wd_file.exists()                                  # the watchdog was disarmed
        wait_for(lambda: not Path(f"/proc/{wd_pid}").exists(), 15, "watchdog process to exit")
        assert processes("watchdog.py", str(cfg)) == []
        with pytest.raises(urllib.error.URLError):
            http(port, "/health", timeout=2)
        assert "already stopped" in aiops("stop", "--config", str(cfg)).stdout
        s3 = aiops("status", "--config", str(cfg), "--json")
        assert s3.returncode == 3 and json.loads(s3.stdout)["control_plane"]["state"] == "stopped"
        # status reports what Docker observes NOW; its health check lags real recovery, so the
        # workload converges to ready shortly after the restart (verification used real inference)
        wait_for(lambda: json.loads(aiops("status", "--config", str(cfg), "--json").stdout)[
            "workload"]["observed"]["ready"], 90, "docker to report the restarted workload ready")
        print("\nLIFECYCLE E2E checks:", checks)
    finally:
        docker("unpause", NAME, check_rc=False)
        if server is not None and server.poll() is None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
