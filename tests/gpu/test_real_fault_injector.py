"""The guarded real fault injector on the real machine. Opt in: AIOPS_GPU=1

The properties that matter, against the real vLLM container:
  * a fault ends BY ITSELF even if the control plane is killed while the workload is paused
    (an independent reaper process resumes it, and only that workload);
  * the golden scenario can be driven by the injector instead of a hand-typed `docker pause`;
  * unsafe requests are refused and nothing is paused.
"""
import json
import os
import shutil
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from vllm_support import BASE, MODEL, NAME, docker, http, wait_for

sys.path.insert(0, str(Path(__file__).parent.parent))
from golden_acceptance import assert_golden  # noqa: E402
from test_real_watchdog import ControlPlane  # noqa: E402

from aiops.docker import DockerProvider  # noqa: E402
from aiops.faults.core import read_lease  # noqa: E402
from aiops.vllm import VllmClient  # noqa: E402
from aiops.watchdog import proc_start  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOPS_GPU") != "1" or not shutil.which("nvidia-smi"),
    reason="set AIOPS_GPU=1 on a machine with an NVIDIA GPU and the vLLM image")


def paused():
    return json.loads(docker("inspect", NAME))[0]["State"]["Paused"]


def post(port, path, body=None, confirm=True, timeout=30):
    headers = {"Content-Type": "application/json"}
    if confirm:
        headers["X-Aiops-Confirm"] = "inject-fault"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                 data=json.dumps(body).encode() if body is not None else b"",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def lease_file(c):
    return Path(str(c.state) + ".fault.json")


def inject(c, seconds):
    return post(c.port, "/api/v1/faults", {"type": "pause_workload", "target": "vllm",
                                          "duration_seconds": seconds})


def test_a_fault_ends_by_itself_even_if_the_control_plane_is_killed(vllm, tmp_path):
    provider = DockerProvider("vllm")
    with ControlPlane(tmp_path) as c:
        c.start()
        wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"], 60, "an observation")
        before = provider.get_workload("vllm")["id"]
        code, body = inject(c, 15)
        assert code == 202 and body["active"]["status"] == "ACTIVE"
        assert paused()
        lease = read_lease(lease_file(c))
        assert lease["status"] == "ACTIVE" and lease["expected_identity"] == before
        reaper = lease["reaper"]
        assert os.getsid(reaper["pid"]) == reaper["pid"]                  # independent of the control plane
        assert proc_start(reaper["pid"]) == reaper["proc_start"]

        c.proc.send_signal(signal.SIGKILL)                                # the control plane crashes
        c.proc.wait(timeout=10)
        time.sleep(1)
        assert paused()                                                   # nothing has resumed it yet ...

        wait_for(lambda: not paused(), 60, "the reaper to resume the workload")   # ... but it does
        lease = read_lease(lease_file(c))
        assert lease["status"] == "RECOVERED" and lease["recovery"]["by"] == "reaper"
        assert lease["recovery"]["action"] == "unpaused"
        assert time.time() >= lease["expires_at"]                         # not before the deadline
        assert provider.get_workload("vllm")["id"] == before              # the SAME run: never restarted
        wait_for(lambda: all(VllmClient(BASE, MODEL, timeout=5).probe()["ok"] for _ in range(3)),
                 60, "inference to answer again")
        wait_for(lambda: not os.path.exists(f"/proc/{reaper['pid']}"), 15, "the reaper to exit")
        wait_for(lambda: not c.stray(), 20, "the watchdog to exit with its control plane")


def test_the_golden_scenario_driven_by_the_guarded_injector(vllm, tmp_path):
    provider = DockerProvider("vllm")
    with ControlPlane(tmp_path) as c:
        c.start()
        wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"], 60, "an observation")
        time.sleep(4)
        assert http(c.port, "/api/v1/incidents")["incidents"] == []
        assert http(c.port, "/api/v1/status")["faults"] == {"available": True, "active": None}
        identity_before = provider.get_workload("vllm")["id"]

        code, _ = inject(c, 120)                                          # instead of `docker pause`
        assert code == 202 and paused()
        assert http(c.port, "/api/v1/status")["faults"]["active"]["workload"] == "vllm"
        incidents = wait_for(lambda: http(c.port, "/api/v1/incidents")["incidents"], 90, "an incident")
        iid = incidents[0]["incident_id"]
        pending = http(c.port, f"/api/v1/incidents/{iid}")
        assert pending["category"] == "INFERENCE_UNRESPONSIVE" and pending["status"] == "POLICY_CHECK"
        assert provider.get_workload("vllm")["id"] == identity_before and paused()   # not restarted yet

        done = http(c.port, f"/api/v1/incidents/{iid}/remediation/approve", method="POST", timeout=240)
        assert done["status"] == "RESOLVED", done
        identity_after = provider.get_workload("vllm")["id"]
        assert identity_after != identity_before and not paused()
        for _ in range(3):
            assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True
            time.sleep(1)

        # the restart ended the fault's pause; ending the lease must NOT touch the new run
        code, ended = post(c.port, "/api/v1/faults/cancel")
        assert code == 200 and ended["last"]["status"] == "IDENTITY_CHANGED"
        assert ended["last"]["recovery"]["action"] == "none"
        assert not paused() and json.loads(docker("inspect", NAME))[0]["State"]["Running"]

        detail = http(c.port, f"/api/v1/incidents/{iid}")
        wait_for(lambda: [e["event"] for e in http(c.port, "/api/v1/audit")["events"]
                          if e["event"].startswith("fault_")] == ["fault_injected", "fault_ended"],
                 20, "the fault to be audited")
        events = http(c.port, "/api/v1/audit")["events"]
        assert_golden(detail, events, identity_before, identity_after, mode="REAL")
        ended_event = next(e for e in events if e["event"] == "fault_ended")["data"]
        assert ended_event["status"] == "IDENTITY_CHANGED" and ended_event["workload"] == "vllm"
        assert c.wd_record()["status"] == "ARMED"                         # a fault is not the watchdog's business


def test_approving_after_the_fault_has_ended_restarts_nothing_on_the_real_machine(vllm, tmp_path):
    """The stale-proposal case (ADR-019): the incident waits for the operator, the lease expires and
    the model answers again; approving must not restart the healthy workload."""
    provider = DockerProvider("vllm")
    with ControlPlane(tmp_path) as c:
        c.start()
        wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"], 60, "an observation")
        identity = provider.get_workload("vllm")["id"]
        assert inject(c, 30)[0] == 202 and paused()
        iid = wait_for(lambda: http(c.port, "/api/v1/incidents")["incidents"], 90, "an incident")[0]["incident_id"]
        assert http(c.port, f"/api/v1/incidents/{iid}")["status"] == "POLICY_CHECK"
        wait_for(lambda: not paused(), 90, "the lease to resume the workload")
        wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"]["inference_probe_ok"] is True,
                 60, "the model to answer again")
        code, body = post(c.port, f"/api/v1/incidents/{iid}/remediation/approve")
        assert code == 409 and body["error"]["code"] == "POLICY_DENIED", body
        assert "no longer present" in body["error"]["message"]
        assert provider.get_workload("vllm")["id"] == identity          # NOT restarted
        assert VllmClient(BASE, MODEL, timeout=5).probe()["ok"] is True
        detail = http(c.port, f"/api/v1/incidents/{iid}")
        assert detail["status"] == "CLEARED"
        names = [e["event"] for e in http(c.port, "/api/v1/audit")["events"]]
        assert "approval_refused" in names and "approval_granted" not in names \
            and "remediation_started" not in names


def test_unsafe_requests_are_refused_on_the_real_machine_and_nothing_is_paused(vllm, tmp_path):
    provider = DockerProvider("vllm")
    with ControlPlane(tmp_path) as c:
        c.start()
        wait_for(lambda: http(c.port, "/api/v1/status")["last_observation"], 60, "an observation")
        before = provider.get_workload("vllm")["id"]
        ok = {"type": "pause_workload", "target": "vllm", "duration_seconds": 60}
        assert post(c.port, "/api/v1/faults", ok, confirm=False)[1]["error"]["code"] == "CONFIRMATION_REQUIRED"
        for bad in ({**ok, "type": "kill_workload"}, {**ok, "target": "someone-else"},
                    {**ok, "duration_seconds": 5}, {**ok, "duration_seconds": 100000}):
            assert post(c.port, "/api/v1/faults", bad)[0] == 400
        assert not paused() and read_lease(lease_file(c)) is None

        docker("pause", NAME)                                             # an operator already paused it
        code, body = post(c.port, "/api/v1/faults", ok)
        assert code == 409 and "already paused" in body["error"]["message"]
        docker("unpause", NAME)

        assert inject(c, 60)[0] == 202
        assert post(c.port, "/api/v1/faults", ok)[1]["error"]["code"] == "FAULT_ACTIVE"
        code, ended = post(c.port, "/api/v1/faults/cancel")
        assert code == 200 and ended["last"]["status"] == "RECOVERED" and not paused()
        assert provider.get_workload("vllm")["id"] == before              # nothing was ever restarted
        assert post(c.port, "/api/v1/faults/cancel")[0] == 409            # nothing left to end
