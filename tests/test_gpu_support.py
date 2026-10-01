"""The real-GPU tests prove 'nothing is left behind' with a process-listing helper. If that
helper silently misses long command lines, every such assertion passes vacuously."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "gpu"))
from vllm_support import processes  # noqa: E402


def test_the_process_helper_sees_processes_with_very_long_command_lines():
    padding = ["x" * 120] * 4                                  # pushes the marker far past col 80
    marker = "unique-marker-for-the-process-helper-test"
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *padding, marker])
    try:
        found = []
        for _ in range(50):
            found = processes(marker)
            if found:
                break
            subprocess.run(["sleep", "0.05"])
        assert len(found) == 1 and marker in found[0]
    finally:
        p.kill()
        p.wait()


def test_the_process_helper_reports_nothing_when_nothing_matches():
    assert processes("no-such-process-marker-4f6a2c91") == []
