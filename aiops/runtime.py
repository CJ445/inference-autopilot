"""Build the one real engine from a validated profile. No demo engine, no silent fallbacks."""
import functools
import subprocess
from pathlib import Path

from aiops.docker import DockerProvider
from aiops.engine import Engine
from aiops.gpu import read_gpu
from aiops.kubectl import KubernetesProvider
from aiops.prometheus import PrometheusAdapter
from aiops.store import Store
from aiops.telemetry import RealTelemetry
from aiops.vllm import VllmClient

VLLM_TIMEOUT_SECONDS = 3

# PromQL for the signals the Kubernetes profile reads (the CPU stand-in's labels).
QUERIES = {
    "gpu_memory_used_bytes": 'gpu_memory_used_bytes{job="vllm"}',
    "error_rate": 'error_rate{job="vllm"}',
    "allocation_failures_total": 'allocation_failures_total{job="vllm"}',
}


def build_engine(profile, run=subprocess.run):
    w, safety, ver = profile["workload"], profile["safety"], profile["verification"]
    config = {"service": "vllm", "workload": w["name"],
              "gpu_threshold": safety["gpu_memory_threshold_bytes"],
              "timeout": ver["timeout"], "interval": ver["interval"]}
    db = Path(profile["control_plane"]["db"])
    db.parent.mkdir(parents=True, exist_ok=True)  # the profile's own state directory
    store = Store(db)

    if profile["provider"] == "docker":
        telemetry = RealTelemetry(
            functools.partial(read_gpu, run=run, gpu_index=profile["gpu"]["index"]),
            VllmClient(w["vllm_url"], w["model"], timeout=VLLM_TIMEOUT_SECONDS))
        provider = DockerProvider(w["name"], run=run)
        config.update(stable_probes=ver["stable_probes"], probe_interval=ver["probe_interval"])
    elif profile["provider"] == "kubernetes":
        telemetry = PrometheusAdapter(w["prometheus_url"], QUERIES)
        provider = KubernetesProvider(w["namespace"], run=run, context=w["context"],
                                      metrics_source=telemetry)
        config["error_rate_limit"] = safety["error_rate_limit"]
    else:
        raise ValueError(f"unknown provider {profile['provider']!r}")
    return Engine(telemetry, provider, config, store=store)
