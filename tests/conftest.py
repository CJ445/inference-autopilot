"""Suite markers, assigned by where a test lives, so a suite is selected with `-m` and a new test
cannot silently join the wrong one.

    pytest -m "unit or simulation_golden or tui"   # portable: no GPU, Docker or Kubernetes (CI)
    pytest -m real_local                           # AIOPS_INTEGRATION=1, Docker / kind
    pytest -m real_gpu                             # AIOPS_GPU=1, NVIDIA GPU
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SIMULATION_GOLDEN = {"test_simulation.py", "test_golden_parity.py", "test_practice_api.py"}


def pytest_collection_modifyitems(items):
    for item in items:
        path = Path(str(item.fspath))
        parts = path.parts
        if "gpu" in parts:
            item.add_marker("real_gpu")
        elif "integration" in parts:
            item.add_marker("real_local")
        elif path.name.startswith("test_tui_"):
            item.add_marker("tui")
        elif path.name in SIMULATION_GOLDEN:
            item.add_marker("simulation_golden")
        else:
            item.add_marker("unit")


@pytest.fixture(scope="session")
def tui_bin():
    """The operator UI binary, built now. A stale binary once made these tests exercise an old UI,
    so with a toolchain it is always (re)built (a no-op when current). Without cargo a binary that
    somebody built is used, else the test is skipped; CI has cargo and fails on any skip."""
    exe = ROOT / "tui" / "target" / "release" / "aiops-tui"
    if shutil.which("cargo") is not None:
        subprocess.run(["cargo", "build", "--release", "--locked", "--manifest-path",
                        str(ROOT / "tui" / "Cargo.toml")], check=True, capture_output=True, timeout=900)
    elif not exe.exists():
        pytest.skip("cargo is not installed and the TUI is not built")
    return str(exe)


def pytest_sessionfinish(session, exitstatus):
    """CI sets AIOPS_FAIL_ON_SKIP=1: in a portable suite a skip is a hole, not a pass."""
    if os.environ.get("AIOPS_FAIL_ON_SKIP") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    if skipped:
        print(f"\nAIOPS_FAIL_ON_SKIP: {len(skipped)} test(s) were skipped; failing the run")
        session.exitstatus = 1
