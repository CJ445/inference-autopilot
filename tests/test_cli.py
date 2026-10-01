import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from test_profile import DOCKER, write

ROOT = Path(__file__).parent.parent


def aiops(*args, launcher=False, timeout=60):
    cmd = [str(ROOT / "bin" / "aiops")] if launcher else [sys.executable, "-m", "aiops"]
    return subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT)


def valid_config(tmp_path, workload=None):
    text = DOCKER
    if workload:
        text = text.replace('name = "vllm"', f'name = "{workload}"')
    return write(tmp_path, text)


def test_a_command_is_required():
    r = aiops()
    assert r.returncode != 0 and "usage" in (r.stdout + r.stderr).lower()


def test_unknown_commands_are_rejected():
    assert aiops("frobnicate").returncode != 0


def test_start_stop_status_reject_an_invalid_configuration_with_exit_code_2(tmp_path):
    bad = tmp_path / "aiops.toml"
    bad.write_text('profile = "fake-gpu"\n')
    for command in ("start", "stop", "status"):
        r = aiops(command, "--config", str(bad))
        assert r.returncode == 2, (command, r.stdout, r.stderr)
        assert "configuration error" in r.stdout


def test_a_missing_config_file_is_a_configuration_error_not_a_crash(tmp_path):
    r = aiops("start", "--config", str(tmp_path / "nope.toml"))
    assert r.returncode == 2 and "configuration error" in r.stdout and "Traceback" not in r.stderr


def test_stop_is_safe_when_nothing_is_running(tmp_path):
    r = aiops("stop", "--config", str(valid_config(tmp_path)))
    assert r.returncode == 0 and "already stopped" in r.stdout
    assert aiops("stop", "--config", str(valid_config(tmp_path))).returncode == 0   # again


def test_start_refuses_and_leaves_nothing_behind_when_the_real_doctor_fails(tmp_path):
    cfg = valid_config(tmp_path, workload=f"aiops-nonexistent-{uuid.uuid4().hex[:8]}")
    r = aiops("start", "--config", str(cfg))
    assert r.returncode == 1 and "refusing to start" in r.stdout
    assert "workload" in r.stdout                       # the failing prerequisite is named
    assert not (tmp_path / "run").exists() and not (tmp_path / "data").exists()


def test_doctor_json_output_has_the_documented_shape_and_exit_code_reflects_failures(tmp_path):
    cfg = valid_config(tmp_path, workload=f"aiops-nonexistent-{uuid.uuid4().hex[:8]}")
    r = aiops("doctor", "--config", str(cfg), "--json")
    results = json.loads(r.stdout)
    assert {x["check"] for x in results} >= {"python", "database", "workload", "gpu"}
    assert all(set(x) == {"check", "status", "detail", "blocking"} for x in results)
    assert any(x["status"] == "FAIL" for x in results) and r.returncode == 1
    assert not (tmp_path / "data").exists()               # doctor is read-only


def test_the_launcher_script_runs_the_same_commands():
    assert os.access(ROOT / "bin" / "aiops", os.X_OK)
    r = aiops("start", "--config", "/nonexistent/aiops.toml", launcher=True)
    assert r.returncode == 2 and "configuration error" in r.stdout


def test_the_existing_serve_command_is_still_available():
    r = aiops("serve", "--help")
    assert r.returncode == 0 and "--prometheus-url" in r.stdout
