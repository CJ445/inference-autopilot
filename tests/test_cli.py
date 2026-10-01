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


def blocked_config(tmp_path):
    """A profile whose GPU pin cannot match: a BLOCKING prerequisite fails on any machine
    (no GPU at all, or a different one). A missing workload no longer blocks start."""
    return write(tmp_path, DOCKER.replace("GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f",
                                          "GPU-00000000-0000-0000-0000-000000000000"))


def valid_config(tmp_path, workload=None):
    text = DOCKER
    if workload:
        text = text.replace('name = "vllm"', f'name = "{workload}"')
    return write(tmp_path, text)


def test_bare_aiops_opens_the_ui_so_without_a_terminal_it_refuses_instead_of_guessing():
    r = aiops()                                  # captured output: not a terminal
    assert r.returncode == 2 and "not a terminal" in r.stdout and "aiops start" in r.stdout


def test_help_lists_the_commands_and_the_bare_form():
    r = aiops("--help")
    assert r.returncode == 0 and "start" in r.stdout and "no command" in r.stdout
    assert "usage: usage:" not in r.stdout


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
    cfg = blocked_config(tmp_path)
    r = aiops("start", "--config", str(cfg))
    assert r.returncode == 1 and "refusing to start" in r.stdout
    assert "gpu" in r.stdout.lower()                    # the failing prerequisite is named
    assert not (tmp_path / "run").exists() and not (tmp_path / "data").exists()


def test_doctor_json_output_has_the_documented_shape_and_exit_code_reflects_failures(tmp_path):
    cfg = blocked_config(tmp_path)
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


# --- `aiops tui`: locate the Rust binary and hand the terminal over to it ----------------------

import stat  # noqa: E402


def fake_tui(tmp_path):
    """An executable that echoes the arguments it was started with."""
    script = tmp_path / "aiops-tui"
    script.write_text('#!/bin/sh\necho "TUI-ARGS: $@"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def tui(tmp_path, *args, binary="auto", **env):
    environ = dict(os.environ)
    if binary == "auto":
        binary = fake_tui(tmp_path)
    environ["AIOPS_TUI_BIN"] = str(binary)
    environ.update(env)
    return subprocess.run([sys.executable, "-m", "aiops", "tui", *args], capture_output=True,
                          text=True, timeout=30, cwd=ROOT, env=environ)


def test_tui_points_the_binary_at_the_profiles_control_plane_port(tmp_path):
    cfg = valid_config(tmp_path)
    r = tui(tmp_path, "--config", str(cfg))
    assert r.returncode == 0 and "TUI-ARGS: --url http://127.0.0.1:8123" in r.stdout


def test_tui_passes_extra_arguments_through_to_the_binary(tmp_path):
    cfg = valid_config(tmp_path)
    r = tui(tmp_path, "--config", str(cfg), "--once", "--screen", "audit")
    assert "TUI-ARGS: --url http://127.0.0.1:8123 --once --screen audit" in r.stdout


def test_an_explicit_url_overrides_the_profile(tmp_path):
    r = tui(tmp_path, "--url", "http://127.0.0.1:9999")
    assert r.returncode == 0 and "--url http://127.0.0.1:9999" in r.stdout


def test_tui_refuses_an_invalid_profile_before_launching_anything(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('profile = "fake-gpu"\n')
    r = tui(tmp_path, "--config", str(bad))
    assert r.returncode == 2 and "configuration error" in r.stdout and "TUI-ARGS" not in r.stdout


def test_a_missing_binary_says_exactly_how_to_build_it(tmp_path):
    r = tui(tmp_path, "--url", "http://127.0.0.1:8080", binary=tmp_path / "nope")
    assert r.returncode == 1
    assert "cargo build --release --manifest-path tui/Cargo.toml" in r.stdout + r.stderr


def test_a_binary_that_is_not_executable_is_reported(tmp_path):
    plain = tmp_path / "aiops-tui"
    plain.write_text("not executable")
    r = tui(tmp_path, "--url", "http://127.0.0.1:8080", binary=plain)
    assert r.returncode == 1 and "not executable" in (r.stdout + r.stderr).lower()


def test_tui_never_creates_state_or_touches_the_database(tmp_path):
    cfg = valid_config(tmp_path)
    binary = fake_tui(tmp_path)                    # part of the setup, not of what is measured
    before = sorted(p.name for p in tmp_path.iterdir())
    tui(tmp_path, "--config", str(cfg), binary=binary)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_usage_mentions_the_tui():
    assert "tui" in aiops("--help").stdout.lower()
