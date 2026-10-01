import pytest
from test_profile import DOCKER, KUBERNETES, write

from aiops.docker import DockerProvider
from aiops.kubectl import ClusterError, KubernetesProvider
from aiops.profile import load_profile
from aiops.prometheus import PrometheusAdapter
from aiops.runtime import build_engine
from aiops.telemetry import RealTelemetry
from aiops.vllm import VllmClient


def docker_profile(tmp_path, edit=lambda t: t):
    return load_profile(write(tmp_path, edit(DOCKER)))


class Recorder:
    """Stands in for subprocess.run; answers nvidia-smi, records every argv."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self.fail:
            raise self.fail

        class R:
            stdout = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f, 2895, 8188, 59, 0\n"
        return R()


def test_docker_profile_builds_the_real_engine_not_a_demo_one(tmp_path):
    engine = build_engine(docker_profile(tmp_path))
    assert isinstance(engine.cluster, DockerProvider)
    assert isinstance(engine.telemetry, RealTelemetry)
    assert isinstance(engine.telemetry.vllm, VllmClient)
    assert (engine.telemetry.vllm.base_url, engine.telemetry.vllm.model) == (
        "http://127.0.0.1:8001", "facebook/opt-125m")
    assert engine.config["workload"] == "vllm" and engine.config["service"] == "vllm"
    assert engine.config["gpu_threshold"] == 4_500_000_000
    assert (engine.config["timeout"], engine.config["stable_probes"]) == (150, 3)
    assert not isinstance(engine.telemetry, PrometheusAdapter)
    assert engine.store is not None                       # same persistence path as ever


def test_the_docker_provider_is_bound_to_the_profile_workload_only(tmp_path):
    engine = build_engine(docker_profile(tmp_path))
    for call in (engine.cluster.get_workload, engine.cluster.restart_workload):
        with pytest.raises(ClusterError, match="not the configured workload"):
            call("some-other-container")


def test_gpu_telemetry_uses_the_profile_gpu_index_and_real_nvidia_smi(tmp_path):
    run = Recorder()
    profile = docker_profile(tmp_path, lambda t: t.replace("index = 0", "index = 1"))
    engine = build_engine(profile, run=run)
    engine.tick()
    smi = [c for c in run.calls if c[0] == "nvidia-smi"]
    assert smi and smi[0][:3] == ["nvidia-smi", "-i", "1"]


def test_the_real_profile_never_falls_back_to_other_telemetry_when_the_gpu_fails(tmp_path):
    run = Recorder(fail=FileNotFoundError("nvidia-smi"))
    engine = build_engine(docker_profile(tmp_path), run=run)
    assert engine.tick() is None
    assert engine.health == "DEGRADED" and engine.incidents == []
    assert isinstance(engine.telemetry, RealTelemetry)    # still the real source, no substitute


def test_kubernetes_profile_builds_the_existing_kubernetes_path(tmp_path):
    engine = build_engine(load_profile(write(tmp_path, KUBERNETES)))
    assert isinstance(engine.cluster, KubernetesProvider)
    assert engine.cluster.context == "kind-aiops-test" and engine.cluster.namespace == "default"
    assert isinstance(engine.telemetry, PrometheusAdapter)
    assert engine.telemetry.base_url == "http://127.0.0.1:19090"
    assert engine.config["workload"] == "vllm-0" and engine.config["gpu_threshold"] == 7_500_000_000
    assert engine.config["error_rate_limit"] == 0.05
    assert not isinstance(engine.cluster, DockerProvider)


def test_a_profile_cannot_produce_the_other_providers_machinery(tmp_path):
    docker = build_engine(docker_profile(tmp_path))
    k8s = build_engine(load_profile(write(tmp_path, KUBERNETES, name="k.toml")))
    assert type(docker.cluster) is not type(k8s.cluster)
    assert type(docker.telemetry) is not type(k8s.telemetry)
