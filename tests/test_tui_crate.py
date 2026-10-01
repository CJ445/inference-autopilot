"""The Rust crate's own tests, so the single `pytest` gate covers the TUI as well."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MANIFEST = Path(__file__).parent.parent / "tui" / "Cargo.toml"


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_the_tui_crates_own_test_suite_passes():
    r = subprocess.run(["cargo", "test", "--locked", "--manifest-path", str(MANIFEST), "--quiet"],
                       capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", r.stdout))
    failed = sum(int(n) for n in re.findall(r"(\d+) failed", r.stdout))
    assert failed == 0 and passed >= 100, (passed, failed)


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_the_lockfile_is_present_and_satisfies_a_locked_offline_build_of_the_host():
    assert (MANIFEST.parent / "Cargo.lock").exists()
    r = subprocess.run(["cargo", "tree", "--locked", "--offline", "--manifest-path",
                        str(MANIFEST), "--depth", "1"], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    direct = {line.split()[1] for line in r.stdout.splitlines() if line[:1] in "├└"}
    assert direct == {"ratatui", "crossterm", "serde", "serde_json"}, direct
