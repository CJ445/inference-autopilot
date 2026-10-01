import json
import os
import subprocess
import sys

import pytest
from test_profile import DOCKER, KUBERNETES, write
from test_prometheus import prom  # noqa: F401  (fixture)
from test_vllm import REAL, Server

from aiops.doctor import (FAIL, NOT_APPLICABLE, PASS, WARN, ReadOnlyRun, ReadOnlyViolation,
                          blocking_failures, run_doctor)
from aiops.profile import load_profile
from aiops.runtime import QUERIES

SHORT = "0123456789ab"
FULL = SHORT + "c" * 52
UUID = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f"
LABELS = {"com.inference-autopilot.managed": "true", "com.inference-autopilot.workload": "vllm"}
SMI_HEADER = ("| NVIDIA-SMI 570.207     Driver Version: 570.207     CUDA Version: {cuda}     |\n")


class System:
    """Answers docker/nvidia-smi/kubectl from a table, records every argv, and fails the test
    on any command it does not know."""

    def __init__(self, **kw):
        self.calls = []
        self.ids = kw.get("ids", SHORT + "\n")
        self.labels = kw.get("labels", LABELS)
        self.full_id = kw.get("full_id", FULL)
        self.health = kw.get("health", "healthy")
        self.running = kw.get("running", True)
        self.image = kw.get("image", "vllm/vllm-openai:v0.10.0")
        self.image_env = kw.get("image_env", ["PATH=/usr/bin", "CUDA_VERSION=12.8.1"])
        self.driver_cuda = kw.get("driver_cuda", "12.8")
        self.gpu_line = kw.get("gpu_line", f"{UUID}, 2895, 8188, 59, 0\n")
        self.fail = kw.get("fail", {})           # {"docker": exc, "nvidia-smi": exc, "kubectl": exc}
        self.contexts = kw.get("contexts", "kind-aiops-test\n")

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        tool = argv[0]
        if tool in self.fail:
            raise self.fail[tool]

        class R:
            returncode = 0
            stdout = ""
        r = R()
        if tool == "docker":
            if argv[1] == "--version":
                r.stdout = "Docker version 29.5.2, build abc\n"
            elif argv[1] == "info":
                r.stdout = "29.5.2\n"
            elif argv[1] == "ps":
                if "{{.Image}}" in argv:
                    r.stdout = "".join(self.image + "\n" for _ in self.ids.split())  # one per container
                else:
                    r.stdout = self.ids
            elif argv[1] == "inspect":
                state = {"Running": self.running, "StartedAt": "2026-10-01T10:00:00Z"}
                if self.health:
                    state["Health"] = {"Status": self.health}
                r.stdout = json.dumps([{"Id": self.full_id, "State": state,
                                        "Config": {"Labels": self.labels,
                                                   "Image": self.image}}])
            elif argv[1:3] == ["image", "inspect"]:
                r.stdout = "\n".join(self.image_env) + "\n"
            else:
                raise AssertionError(f"unexpected docker command {argv}")
        elif tool == "nvidia-smi":
            if len(argv) == 1:
                r.stdout = SMI_HEADER.format(cuda=self.driver_cuda)
            elif "--query-gpu=driver_version" in argv:
                r.stdout = "570.207\n"
            elif any(a.startswith("--query-gpu=uuid") for a in argv):
                r.stdout = self.gpu_line
            elif any(a.startswith("--query-gpu=memory.used") for a in argv):
                r.stdout = "2895, 8188, 59, 0\n"            # the watchdog's own sensor query
            else:
                raise AssertionError(f"unexpected nvidia-smi command {argv}")
        elif tool == "kubectl":
            if "config" in argv and "get-contexts" in argv:
                r.stdout = self.contexts
            elif "get" in argv and "pod" in argv:
                r.stdout = json.dumps({"metadata": {"uid": "u1"}, "status": {"conditions": [
                    {"type": "Ready", "status": "True"}]}})
            else:
                raise AssertionError(f"unexpected kubectl command {argv}")
        else:
            raise AssertionError(f"unexpected tool {argv}")
        return r


def which_all(name):
    return f"/usr/bin/{name}"


@pytest.fixture
def vllm_server():
    s = Server()
    yield s
    s.httpd.shutdown()


def docker_profile(tmp_path, url, edit=lambda t: t):
    text = edit(DOCKER.replace("http://127.0.0.1:8001", url))
    return load_profile(write(tmp_path, text))


def doctor(profile, system=None, which=which_all):
    return {r["check"]: r for r in run_doctor(profile, run=system or System(), which=which)}


def test_a_healthy_docker_real_gpu_setup_passes_every_applicable_check(tmp_path, vllm_server):
    results = doctor(docker_profile(tmp_path, vllm_server.url))
    statuses = {k: v["status"] for k, v in results.items()}
    assert statuses == {
        "python": PASS, "database": PASS, "state_dir": PASS, "safety_limits": PASS,
        "docker_cli": PASS, "docker_daemon": PASS, "workload": PASS, "nvidia_driver": PASS,
        "gpu": PASS, "gpu_expectations": PASS, "cuda_compat": PASS, "gpu_headroom": PASS,
        "vllm_endpoint": PASS, "vllm_metrics": PASS, "vllm_probe": PASS,
        "watchdog_identity": PASS, "watchdog_sensors": PASS, "watchdog_state": PASS,
        "kubectl": NOT_APPLICABLE, "kube_context": NOT_APPLICABLE, "prometheus": NOT_APPLICABLE}
    assert blocking_failures(results.values()) == []


def test_every_result_has_the_documented_shape(tmp_path, vllm_server):
    for r in doctor(docker_profile(tmp_path, vllm_server.url)).values():
        assert set(r) == {"check", "status", "detail", "blocking"}
        assert r["status"] in {PASS, WARN, FAIL, NOT_APPLICABLE} and r["detail"]


# --- the managed workload ---------------------------------------------------------------

def test_a_missing_managed_workload_fails_and_blocks(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(ids=""))
    assert r["workload"]["status"] == FAIL and "no managed workload" in r["workload"]["detail"]
    assert r["workload"]["blocking"] is True
    assert r["cuda_compat"]["status"] == NOT_APPLICABLE


def test_an_ambiguous_workload_fails_and_blocks(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(ids=SHORT + "\nba9876543210\n"))
    assert r["workload"]["status"] == FAIL and "ambiguous" in r["workload"]["detail"]
    assert r["workload"]["blocking"] is True


@pytest.mark.parametrize("system", [
    System(full_id="f" * 64),                                          # inspect != ps
    System(labels={"com.inference-autopilot.managed": "true",
                   "com.inference-autopilot.workload": "other"}),      # labels mismatch
    System(labels={}),                                                 # unlabeled
])
def test_identity_mismatches_fail_and_block(tmp_path, vllm_server, system):
    r = doctor(docker_profile(tmp_path, vllm_server.url), system)
    assert r["workload"]["status"] == FAIL and r["workload"]["blocking"] is True


def test_a_workload_that_is_found_but_not_ready_is_a_warning_not_a_failure(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(health="starting"))
    assert r["workload"]["status"] == WARN and "not ready" in r["workload"]["detail"]


# --- docker ----------------------------------------------------------------------------

def test_missing_docker_cli_fails_everything_that_needs_it(tmp_path, vllm_server):
    system = System(fail={"docker": FileNotFoundError("docker")})
    r = doctor(docker_profile(tmp_path, vllm_server.url), system,
               which=lambda n: None if n == "docker" else f"/usr/bin/{n}")
    assert r["docker_cli"]["status"] == FAIL and r["docker_daemon"]["status"] == FAIL
    assert r["workload"]["status"] == FAIL
    assert all(r[k]["blocking"] for k in ("docker_cli", "docker_daemon", "workload"))


def test_an_unreachable_docker_daemon_fails_and_blocks(tmp_path, vllm_server):
    err = subprocess.CalledProcessError(1, ["docker"], stderr="Cannot connect to the Docker daemon")
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(fail={"docker": err}))
    assert r["docker_cli"]["status"] == PASS
    assert r["docker_daemon"]["status"] == FAIL and "daemon" in r["docker_daemon"]["detail"]
    assert r["workload"]["status"] == FAIL


# --- GPU -------------------------------------------------------------------------------

def test_an_unavailable_gpu_fails_every_gpu_check_and_blocks(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url),
               System(fail={"nvidia-smi": FileNotFoundError("nvidia-smi")}))
    for name in ("nvidia_driver", "gpu", "gpu_expectations", "gpu_headroom", "watchdog_sensors"):
        assert r[name]["status"] == FAIL and r[name]["blocking"] is True, name


def test_a_pinned_gpu_uuid_that_does_not_match_fails(tmp_path, vllm_server):
    other = "GPU-ffffffff-0000-0000-0000-000000000000, 2895, 8188, 59, 0\n"
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(gpu_line=other))
    assert r["gpu_expectations"]["status"] == FAIL and "uuid" in r["gpu_expectations"]["detail"]


def test_a_gpu_smaller_than_the_expected_minimum_fails(tmp_path, vllm_server):
    small = f"{UUID}, 100, 4096, 59, 0\n"
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(gpu_line=small))
    assert r["gpu_expectations"]["status"] == FAIL and "memory" in r["gpu_expectations"]["detail"]


def test_unpinned_gpu_expectations_are_not_applicable(tmp_path, vllm_server):
    p = docker_profile(tmp_path, vllm_server.url, lambda t: t.replace(
        'uuid = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f"\n', "").replace(
        "min_memory_mib = 6000\n", ""))
    assert doctor(p)["gpu_expectations"]["status"] == NOT_APPLICABLE


@pytest.mark.parametrize("image_cuda, driver_cuda, status", [
    ("13.0.2", "12.8", FAIL),     # the real failure seen on this machine (Error 804)
    ("12.9.1", "12.8", WARN),     # same major, newer minor: minor-version compatibility, unproven
    ("12.8.1", "12.8", PASS),
    ("12.4.0", "12.8", PASS),
    ("11.8.0", "12.8", PASS),
])
def test_cuda_compatibility_is_computed_from_the_actual_image_and_driver(
        tmp_path, vllm_server, image_cuda, driver_cuda, status):
    system = System(image_env=[f"CUDA_VERSION={image_cuda}"], driver_cuda=driver_cuda)
    r = doctor(docker_profile(tmp_path, vllm_server.url), system)["cuda_compat"]
    assert r["status"] == status
    assert image_cuda in r["detail"] and driver_cuda in r["detail"]
    assert r["blocking"] is True


def test_the_cuda_check_inspects_the_running_containers_own_image(tmp_path, vllm_server):
    system = System(image="registry.local/custom-vllm:7")
    doctor(docker_profile(tmp_path, vllm_server.url), system)
    assert any(c[:3] == ["docker", "image", "inspect"] and "registry.local/custom-vllm:7" in c
               for c in system.calls)


def test_an_image_that_declares_no_cuda_version_is_a_warning(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(image_env=["PATH=/usr/bin"]))
    assert r["cuda_compat"]["status"] == WARN


def test_an_unparseable_driver_cuda_version_is_a_warning(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(driver_cuda="unknown"))
    assert r["cuda_compat"]["status"] == WARN


def test_a_gpu_already_over_the_configured_limits_blocks(tmp_path, vllm_server):
    hot = f"{UUID}, 2895, 8188, 91, 0\n"
    r = doctor(docker_profile(tmp_path, vllm_server.url), System(gpu_line=hot))
    assert r["gpu_headroom"]["status"] == FAIL and "temperature" in r["gpu_headroom"]["detail"]
    assert r["gpu_headroom"]["blocking"] is True


def test_a_threshold_above_total_gpu_memory_can_never_fire_and_fails(tmp_path, vllm_server):
    p = docker_profile(tmp_path, vllm_server.url, lambda t: t.replace(
        "gpu_memory_threshold_bytes = 4500000000", "gpu_memory_threshold_bytes = 9000000000"))
    r = doctor(p)["gpu_headroom"]
    assert r["status"] == FAIL and "never" in r["detail"]


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("[safety]", "[safety]\nmax_temperature_c = 95"),
    lambda t: t.replace("[safety]", "[safety]\nmax_gpu_memory_percent = 95"),
    lambda t: t.replace("[safety]", "[safety]\nmax_ram_percent = 99"),
])
def test_looser_than_recommended_safety_limits_are_a_warning(tmp_path, vllm_server, edit):
    r = doctor(docker_profile(tmp_path, vllm_server.url, edit))
    assert r["safety_limits"]["status"] == WARN and r["safety_limits"]["blocking"] is False


# --- vLLM: honest FAIL, but workload health never blocks start ----------------------------

def test_an_unavailable_vllm_fails_its_checks_without_blocking_start(tmp_path):
    r = doctor(docker_profile(tmp_path, "http://127.0.0.1:1"))
    for name in ("vllm_endpoint", "vllm_metrics", "vllm_probe"):
        assert r[name]["status"] == FAIL and r[name]["blocking"] is False, name


def test_unavailable_metrics_fail_only_the_metrics_check(tmp_path, vllm_server):
    vllm_server.metrics_text = "\n".join(
        l for l in REAL.splitlines() if "num_requests_waiting" not in l)
    r = doctor(docker_profile(tmp_path, vllm_server.url))
    assert r["vllm_metrics"]["status"] == FAIL
    assert "num_requests_waiting" in r["vllm_metrics"]["detail"]
    assert r["vllm_endpoint"]["status"] == PASS and r["vllm_probe"]["status"] == PASS


def test_a_failing_inference_probe_fails_only_the_probe_check(tmp_path, vllm_server):
    vllm_server.completion = (500, {"error": "boom"})
    r = doctor(docker_profile(tmp_path, vllm_server.url))
    assert r["vllm_probe"]["status"] == FAIL and "http 500" in r["vllm_probe"]["detail"]
    assert r["vllm_metrics"]["status"] == PASS


# --- persistence -----------------------------------------------------------------------

needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def test_a_corrupt_database_fails_and_blocks(tmp_path, vllm_server):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "aiops.db").write_bytes(b"this is not an sqlite database " * 20)
    r = doctor(docker_profile(tmp_path, vllm_server.url))["database"]
    assert r["status"] == FAIL and r["blocking"] is True


def test_a_tampered_audit_chain_fails_and_blocks(tmp_path, vllm_server):
    from test_store import CONFIG, faulted_world

    from aiops.engine import Engine
    from aiops.store import Store
    import sqlite3

    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "aiops.db"
    w = faulted_world()
    Engine(w, w, CONFIG, store=Store(db)).tick()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit SET data = '{\"incident_id\": \"forged\"}' WHERE seq = 0")
    r = doctor(docker_profile(tmp_path, vllm_server.url))["database"]
    assert r["status"] == FAIL and "audit" in r["detail"] and r["blocking"] is True


def test_a_valid_existing_database_passes_and_reports_its_audit_chain(tmp_path, vllm_server):
    from test_store import CONFIG, faulted_world

    from aiops.engine import Engine
    from aiops.store import Store

    (tmp_path / "data").mkdir()
    w = faulted_world()
    Engine(w, w, CONFIG, store=Store(tmp_path / "data" / "aiops.db")).tick()
    r = doctor(docker_profile(tmp_path, vllm_server.url))["database"]
    assert r["status"] == PASS and "audit" in r["detail"]


@needs_nonroot
def test_a_read_only_database_file_fails_because_start_could_not_persist(tmp_path, vllm_server):
    from test_store import CONFIG, faulted_world

    from aiops.engine import Engine
    from aiops.store import Store

    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "aiops.db"
    w = faulted_world()
    Engine(w, w, CONFIG, store=Store(db)).tick()
    os.chmod(db, 0o444)
    try:
        r = doctor(docker_profile(tmp_path, vllm_server.url))["database"]
    finally:
        os.chmod(db, 0o644)
    assert r["status"] == FAIL and "writable" in r["detail"]


@needs_nonroot
def test_an_unwritable_state_location_fails_and_blocks(tmp_path, vllm_server):
    locked = tmp_path / "locked"
    locked.mkdir()
    p = docker_profile(tmp_path, vllm_server.url, lambda t: t.replace(
        'db = "data/aiops.db"', 'db = "locked/sub/aiops.db"').replace(
        'state_file = "run/aiops.state.json"', 'state_file = "locked/sub/state.json"'))
    os.chmod(locked, 0o555)
    try:
        r = doctor(p)
    finally:
        os.chmod(locked, 0o755)
    assert r["database"]["status"] == FAIL and r["state_dir"]["status"] == FAIL


# --- doctor never mutates --------------------------------------------------------------

def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def test_doctor_creates_no_files_and_issues_only_read_only_commands(tmp_path, vllm_server):
    system = System()
    before = tree(tmp_path)
    profile = docker_profile(tmp_path, vllm_server.url)
    after_profile = tree(tmp_path)               # the profile file itself
    doctor(profile, system)
    assert tree(tmp_path) == after_profile and before != after_profile
    assert not (tmp_path / "data").exists() and not (tmp_path / "run").exists()
    verbs = {(c[0], c[1] if c[0] == "docker" else "") for c in system.calls}
    assert verbs <= {("docker", "--version"), ("docker", "info"), ("docker", "ps"),
                     ("docker", "inspect"), ("docker", "image"), ("nvidia-smi", "")}


@pytest.mark.parametrize("argv", [
    ["docker", "restart", "-t", "30", FULL], ["docker", "exec", FULL, "sh"],
    ["docker", "rm", "-f", FULL], ["docker", "run", "x"], ["docker", "kill", FULL],
    ["docker", "stop", FULL], ["docker", "pause", FULL], ["docker", "pull", "x"],
    ["docker", "image", "rm", "x"], ["docker", "update", FULL],
    ["kubectl", "--context", "c", "-n", "d", "delete", "pod", "p"],
    ["kubectl", "--context", "c", "apply", "-f", "x"], ["kubectl", "exec", "p"],
    ["kubectl", "--context", "c", "-n", "d", "patch", "pod", "p"],
    ["bash", "-c", "echo"], ["rm", "-rf", "/"], ["nvidia-smi", "-r"], ["nvidia-smi", "-pm", "1"],
])
def test_the_read_only_runner_refuses_anything_that_could_mutate(argv):
    ro = ReadOnlyRun(lambda a, **kw: pytest.fail(f"must not run: {a}"))
    with pytest.raises(ReadOnlyViolation):
        ro(argv)


# --- kubernetes profile ----------------------------------------------------------------

def k8s_profile(tmp_path, url):
    return load_profile(write(tmp_path, KUBERNETES.replace("http://127.0.0.1:19090", url)))


def seed_prometheus(data):
    data[QUERIES["gpu_memory_used_bytes"]] = 4e9
    data[QUERIES["error_rate"]] = 0.0
    data[QUERIES["allocation_failures_total"]] = 0


def test_a_healthy_kubernetes_profile_passes_and_marks_docker_gpu_checks_not_applicable(
        tmp_path, prom):
    url, data, _ = prom
    seed_prometheus(data)
    r = doctor(k8s_profile(tmp_path, url))
    for name in ("python", "database", "state_dir", "safety_limits", "kubectl",
                 "kube_context", "workload", "prometheus"):
        assert r[name]["status"] == PASS, (name, r[name])
    for name in ("docker_cli", "docker_daemon", "nvidia_driver", "gpu", "gpu_expectations",
                 "cuda_compat", "gpu_headroom", "vllm_endpoint", "vllm_metrics", "vllm_probe"):
        assert r[name]["status"] == NOT_APPLICABLE, name


def test_kubernetes_failures_block_with_clear_details(tmp_path):
    system = System(contexts="some-other-context\n")
    r = doctor(k8s_profile(tmp_path, "http://127.0.0.1:1"), system)
    assert r["kube_context"]["status"] == FAIL and "kind-aiops-test" in r["kube_context"]["detail"]
    assert r["prometheus"]["status"] == FAIL and r["prometheus"]["blocking"] is True
    assert r["kubectl"]["status"] == PASS


def test_doctor_of_a_broken_config_is_reported_by_the_cli_not_silently_ignored(tmp_path):
    bad = tmp_path / "aiops.toml"
    bad.write_text('profile = "fake-gpu"\n')
    out = subprocess.run([sys.executable, "-m", "aiops", "doctor", "--config", str(bad),
                          "--json"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 2
    results = json.loads(out.stdout)
    assert results[0]["check"] == "config" and results[0]["status"] == FAIL


# --- the watchdog's prerequisites ----------------------------------------------------------

def wd_record(tmp_path, **fields):
    from datetime import datetime, timezone

    from aiops.watchdog import boot_id, proc_start

    path = tmp_path / "run" / "aiops.state.json.watchdog"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    record = {"status": "ARMED", "pid": os.getpid(), "proc_start": proc_start(os.getpid()),
              "protected": {"pid": os.getpid(), "start": proc_start(os.getpid()),
                            "boot_id": boot_id()}, "last_check_at": now, "interval": 1.0}
    record.update(fields)
    path.write_text(json.dumps(record))
    return path


def test_the_watchdog_can_establish_an_identity_and_a_stable_handle_here(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url))["watchdog_identity"]
    assert r["status"] == PASS and "pidfd" in r["detail"] and r["blocking"] is True


def test_a_host_without_pidfd_fails_and_blocks_because_the_watchdog_could_not_arm(
        tmp_path, vllm_server, monkeypatch):
    monkeypatch.setattr("aiops.doctor.pidfd_supported", lambda: (False, "pidfd unavailable (x)"))
    r = doctor(docker_profile(tmp_path, vllm_server.url))["watchdog_identity"]
    assert r["status"] == FAIL and r["blocking"] is True


def test_the_watchdogs_own_gpu_sensor_path_is_checked_independently_of_the_engines(
        tmp_path, vllm_server):
    system = System()
    r = doctor(docker_profile(tmp_path, vllm_server.url), system)["watchdog_sensors"]
    assert r["status"] == PASS and r["blocking"] is True
    # the watchdog's query is the 4-field memory/temperature/utilization one, not the engine's
    assert any(any(a.startswith("--query-gpu=memory.used") for a in c) for c in system.calls)


def test_the_kubernetes_profile_checks_only_the_ram_sensor_for_the_watchdog(tmp_path, prom):
    url, data, _ = prom
    seed_prometheus(data)
    r = doctor(k8s_profile(tmp_path, url))
    assert r["watchdog_sensors"]["status"] == PASS and "RAM" in r["watchdog_sensors"]["detail"]
    assert r["watchdog_identity"]["status"] == PASS


def test_no_watchdog_record_is_fine(tmp_path, vllm_server):
    r = doctor(docker_profile(tmp_path, vllm_server.url))["watchdog_state"]
    assert r["status"] == PASS and "no watchdog record" in r["detail"]


def test_a_live_armed_watchdog_is_reported_not_flagged(tmp_path, vllm_server):
    wd_record(tmp_path)
    r = doctor(docker_profile(tmp_path, vllm_server.url))["watchdog_state"]
    assert r["status"] == PASS and "armed" in r["detail"]


def test_a_stale_watchdog_record_warns_without_blocking(tmp_path, vllm_server):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    wd_record(tmp_path, pid=dead.pid, proc_start="1")
    r = doctor(docker_profile(tmp_path, vllm_server.url))["watchdog_state"]
    assert r["status"] == WARN and "stale" in r["detail"] and r["blocking"] is False


def test_a_previous_abort_by_the_watchdog_is_surfaced(tmp_path, vllm_server):
    wd_record(tmp_path, status="ABORTED", reason=["temperature_c"], action="SIGTERM")
    r = doctor(docker_profile(tmp_path, vllm_server.url))["watchdog_state"]
    assert r["status"] == WARN and "temperature_c" in r["detail"] and r["blocking"] is False


def test_an_unreadable_watchdog_record_warns(tmp_path, vllm_server):
    path = wd_record(tmp_path)
    path.write_text("{ nope")
    assert doctor(docker_profile(tmp_path, vllm_server.url))["watchdog_state"]["status"] == WARN
