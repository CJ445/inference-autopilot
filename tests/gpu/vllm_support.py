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
