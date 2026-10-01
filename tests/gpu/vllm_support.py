"""Shared constants and helpers for the real-GPU tests (one managed vLLM container)."""
import json
import subprocess

NAME, IMAGE, MODEL = "aiops-vllm", "vllm/vllm-openai:v0.10.0", "facebook/opt-125m"
BASE = "http://127.0.0.1:8001"
GUARD = {"max_gpu_memory_percent": 85, "max_temperature_c": 80, "max_ram_percent": 90}
THRESHOLD = 4_500_000_000  # vLLM alone holds ~2.9 GB; the 2 GiB stressor lifts it to ~5.0 GB


def docker(*args, check_rc=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          check=check_rc).stdout.strip()


def state():
    s = json.loads(docker("inspect", NAME))[0]["State"]
    return s["Running"] and not s["Paused"] and s.get("Health", {}).get("Status") == "healthy"


# --- helpers for tests that drive the product through bin/aiops --------------------------------
import socket  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

AIOPS = str(Path(__file__).parent.parent.parent / "bin" / "aiops")


def aiops(*args, timeout=120):
    return subprocess.run([AIOPS, *args], capture_output=True, text=True, timeout=timeout)


def http(port, path, method="GET", timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wait_for(fn, timeout, what):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, KeyError) as e:
            last = e
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what} (last error: {last})")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def processes(*needles):
    """Live processes whose command line contains every needle (never this ps itself)."""
    # -ww: unlimited width. Without it `ps` truncates at ~80 columns in some contexts (e.g. under
    # pytest), so a long command line never matched and "nothing is left behind" passed vacuously.
    out = subprocess.run(["ps", "-ww", "-eo", "pid,args"], capture_output=True, text=True).stdout
    return [l.strip() for l in out.splitlines()
            if all(n in l for n in needles) and "ps -ww" not in l]


def smi(query):
    out = subprocess.run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return [float(v) for v in out.strip().split(",")]
