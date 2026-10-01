"""The independent watchdog against the real RTX 4060 and the real vLLM. Opt in: AIOPS_GPU=1

Safety plan: nothing here pushes the host past the project's existing limits. Budget violations
are produced by (a) a short runtime budget, (b) a configured memory budget BELOW what a real,
bounded load reaches (the existing stressor, peak ~61% of VRAM against the 85% host limit), and
(c) a configured temperature budget BELOW the current temperature with no extra load at all.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from vllm_support import (AIOPS, BASE, MODEL, NAME, THRESHOLD, aiops, docker, free_port, processes, smi,
                          wait_for)

from aiops.docker import DockerProvider
from aiops.gpu import read_gpu
from aiops.gpu_fault import DEFAULT_LIMITS, run_gpu_pressure
from aiops.vllm import VllmClient
from aiops.watchdog import EXIT_ABORTED, identity_of

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")

WATCHDOG_FILE = Path(__file__).parent.parent.parent / "aiops" / "watchdog.py"


class ControlPlane:
    """`bin/aiops start` as a real foreground process, exactly as an operator runs it."""

    def __init__(self, tmp_path, safety="", watchdog="interval = 0.5\nterm_grace_seconds = 10\n"):
        self.dir, self.port, self.proc = tmp_path, free_port(), None
        self.cfg = tmp_path / "aiops.toml"
        self.cfg.write_text(f'''
profile = "docker-real-gpu"
[control_plane]
port = {self.port}
db = "aiops.db"
state_file = "aiops.state.json"
interval = 1
[workload]
name = "vllm"
vllm_url = "{BASE}"
model = "{MODEL}"
[gpu]
index = 0
[safety]
gpu_memory_threshold_bytes = {THRESHOLD}
{safety}
[verification]
timeout = 150
interval = 2
[watchdog]
{watchdog}
''')
        self.state = tmp_path / "aiops.state.json"
        self.wd_state = tmp_path / "aiops.state.json.watchdog"

    def start(self):
        self.log = open(self.dir / "start.log", "w")
        self.proc = subprocess.Popen([AIOPS, "start", "--config", str(self.cfg)],
                                     stdout=self.log, stderr=self.log)
        wait_for(lambda: self.state.exists(), 60, "control plane state file")
        wait_for(lambda: self.wd_record()["status"] == "ARMED", 30, "watchdog armed")

    def wd_record(self):
        return json.loads(self.wd_state.read_text())

    def status(self):
        return json.loads(aiops("status", "--config", str(self.cfg), "--json").stdout)

    def output(self):
        self.log.flush()
        return (self.dir / "start.log").read_text()

    def stop(self):
        return aiops("stop", "--config", str(self.cfg))

    def stray(self):
        # "aiops start --config" cannot match the watchdog's own "--proc-start" argument
        return (processes("watchdog.py", str(self.cfg))
                + processes("aiops start --config", str(self.cfg)))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        docker("unpause", NAME, check_rc=False)


def container_untouched(provider, id_before):
    state = json.loads(docker("inspect", NAME))[0]["State"]
    assert provider.get_workload("vllm")["id"] == id_before          # never restarted
    assert state["Running"] and not state["Paused"]                  # never paused or stopped
    assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True  # still serving inference


def test_normal_operation_stays_below_every_budget_and_the_sensors_match_the_real_gpu(
        vllm, tmp_path):
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        _normal_operation(tmp_path)
        assert bystander.poll() is None                              # nothing unrelated touched
    finally:
        bystander.kill()
        bystander.wait()


def _normal_operation(tmp_path):
    with ControlPlane(tmp_path) as c:
        c.start()
        assert len(c.stray()) == 2          # positive control: the control plane and its watchdog
        st = c.status()
        w = st["watchdog"]
        assert st["control_plane"]["state"] == "running" and w["state"] == "armed"
        assert w["protected_pid"] == c.proc.pid and w["pid"] != c.proc.pid

        used, total, temp = smi("memory.used,memory.total,temperature.gpu")
        sample = wait_for(lambda: c.wd_record()["last_sample"], 30, "first sample")
        # the watchdog's own observation agrees with an independent nvidia-smi reading
        assert abs(sample["gpu_memory_percent"] - 100 * used / total) <= 5
        assert abs(sample["temperature_c"] - temp) <= 6
        limits = c.wd_record()["limits"]
        assert sample["gpu_memory_percent"] < limits["max_gpu_memory_percent"]
        assert sample["temperature_c"] < limits["max_temperature_c"]
        assert sample["ram_percent"] < limits["max_ram_percent"]
        assert sample["runtime_seconds"] < limits["max_runtime_seconds"]

        first = c.wd_record()["last_check_at"]
        time.sleep(8)                                                # many heartbeats, no action
        assert c.proc.poll() is None and c.wd_record()["status"] == "ARMED"
        assert c.wd_record()["last_check_at"] != first
        assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True

        stopped = c.stop()
        assert stopped.returncode == 0 and c.proc.wait(timeout=30) == 0
        assert not c.wd_state.exists() and not c.state.exists()       # the watchdog is disarmed
        assert c.stray() == []


def test_the_runtime_budget_terminates_only_the_control_plane_and_nothing_resurrects_it(
        vllm, tmp_path):
    provider = DockerProvider("vllm")
    id_before = provider.get_workload("vllm")["id"]
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        with ControlPlane(tmp_path, safety="max_runtime_seconds = 8\n") as c:
            c.start()
            assert len(c.stray()) == 2      # positive control for the absence checks below
            assert c.proc.wait(timeout=60) == 4                      # stopped by the watchdog
            out = c.output()
            assert "stopped by the watchdog" in out and "runtime_seconds" in out
            wait_for(lambda: c.wd_record()["status"] == "ABORTED", 20, "final abort record")
            rec = c.wd_record()
            assert rec["reason"] == ["runtime_seconds"]
            assert rec["action"] == "SIGTERM" and rec["exited"] is True   # graceful, not killed
            assert rec["protected"]["pid"] == c.proc.pid
            assert not c.state.exists()                              # it released its own state
            wait_for(lambda: c.stray() == [], 15, "no watchdog or control plane left")

            time.sleep(3)                                            # no automatic resurrection
            assert c.stray() == [] and not c.state.exists()

            s = c.status()
            assert s["control_plane"]["state"] == "stopped"
            assert s["watchdog"]["state"] == "aborted" and s["watchdog"]["reason"] == [
                "runtime_seconds"]
        container_untouched(provider, id_before)                     # the workload is unaffected
        assert bystander.poll() is None                              # so is any unrelated process
    finally:
        bystander.kill()
        bystander.wait()


def test_a_real_gpu_memory_budget_violation_terminates_the_control_plane_not_the_workload(
        vllm, tmp_path):
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        _memory_violation(tmp_path)
        assert bystander.poll() is None                              # nothing unrelated touched
    finally:
        bystander.kill()
        bystander.wait()


def _memory_violation(tmp_path):
    provider = DockerProvider("vllm")
    id_before = provider.get_workload("vllm")["id"]
    baseline = read_gpu()["gpu_memory_used_bytes"]
    with ControlPlane(tmp_path, safety="max_gpu_memory_percent = 55\n") as c:
        c.start()
        assert c.wd_record()["limits"]["max_gpu_memory_percent"] == 55
        result = {}
        # The stressor only allocates memory; any GPU *utilization* it sees is vLLM answering the
        # control plane's once-a-second probes. Its own memory, temperature, RAM and time limits
        # stay in force; the utilization limit would only trip on that unrelated noise.
        limits = {k: v for k, v in DEFAULT_LIMITS.items() if k != "max_gpu_percent"}
        stressor = threading.Thread(target=lambda: result.update(
            run_gpu_pressure(2048, 25, tmp_path / "r.json", limits=limits)))
        stressor.start()
        try:
            assert c.proc.wait(timeout=120) == 4                      # the real load crossed 55%
            wait_for(lambda: c.wd_record()["status"] == "ABORTED", 20, "final abort record")
            rec = c.wd_record()
            assert rec["reason"] == ["gpu_memory_percent"]
            assert rec["sample"]["gpu_memory_percent"] > 55 and rec["action"] == "SIGTERM"
            assert rec["sample"]["gpu_memory_percent"] < 85            # still inside the host limit
            assert "gpu_memory_percent" in c.output()
        finally:
            stressor.join(timeout=150)
        # the bounded stressor ran to completion on its own: it was not the watchdog's target
        assert result["outcome"] == "COMPLETED", result
        assert result["report"]["oom_caught"] is True
        assert c.stray() == []
    container_untouched(provider, id_before)
    time.sleep(3)
    back = read_gpu()["gpu_memory_used_bytes"]                        # GPU back to vLLM-only level
    assert abs(back - baseline) < 0.04 * read_gpu()["gpu_memory_total_bytes"]


def test_a_temperature_budget_below_the_current_reading_is_detected_without_any_load(
        vllm, tmp_path):
    _, _, temp = smi("memory.used,memory.total,temperature.gpu")
    limit = max(1, int(temp) - 15)                    # an unmistakable violation, zero extra load
    cfg = tmp_path / "aiops.toml"
    cfg.write_text(f'''
profile = "docker-real-gpu"
[workload]
name = "vllm"
vllm_url = "{BASE}"
model = "{MODEL}"
[safety]
gpu_memory_threshold_bytes = {THRESHOLD}
max_temperature_c = {limit}
[watchdog]
interval = 0.2
term_grace_seconds = 5
''')
    protected = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        ident = identity_of(protected.pid)
        wd = subprocess.run(
            [sys.executable, "-I", str(WATCHDOG_FILE), "--config", str(cfg), "--pid",
             str(ident["pid"]), "--proc-start", ident["start"], "--boot-id", ident["boot_id"]],
            capture_output=True, text=True, timeout=60)
        assert wd.returncode == EXIT_ABORTED, wd.stderr
        rec = json.loads((tmp_path / "aiops.state.json.watchdog").read_text())
        assert rec["status"] == "ABORTED" and rec["reason"] == ["temperature_c"]
        assert abs(rec["sample"]["temperature_c"] - temp) <= 6        # a real observation
        assert rec["sample"]["temperature_c"] > limit
        assert protected.wait(timeout=10) == -signal.SIGTERM
        assert bystander.poll() is None                               # nothing unrelated touched
    finally:
        for p in (protected, bystander):
            if p.poll() is None:
                p.kill()
            p.wait()
    assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True   # the workload never noticed
