import pytest
from test_vllm import Server

from aiops.gpu import read_gpu
from aiops.prometheus import TelemetryError
from aiops.telemetry import RealTelemetry
from aiops.vllm import VllmClient

NVIDIA_SMI_LINE = "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f, 2895, 8188, 59, 7\n"


class Out:
    def __init__(self, stdout):
        self.stdout = stdout


def test_gpu_reading_is_parsed_with_uuid_and_bytes():
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return Out(NVIDIA_SMI_LINE)

    g = read_gpu(run=run)
    assert g == {"gpu_uuid": "GPU-1e5dd8d1-7112-3d8d-c5cc-4f56f97fa24f",
                 "gpu_memory_used_bytes": 2895 * 2**20,
                 "gpu_memory_total_bytes": 8188 * 2**20,
                 "gpu_temperature_c": 59.0, "gpu_utilization_percent": 7.0}
    assert seen[0][:3] == ["nvidia-smi", "-i", "0"] and "shell" not in seen[0]


@pytest.mark.parametrize("failure", [
    FileNotFoundError("nvidia-smi"),
    OSError("driver not loaded"),
])
def test_gpu_unreadable_raises_telemetry_error(failure):
    def run(argv, **kw):
        raise failure

    with pytest.raises(TelemetryError):
        read_gpu(run=run)


@pytest.mark.parametrize("bad", ["", "GPU-x, [N/A], 8188, 59, 7\n", "GPU-x, 1, 2\n", "nonsense"])
def test_gpu_malformed_output_raises_telemetry_error(bad):
    with pytest.raises(TelemetryError):
        read_gpu(run=lambda argv, **kw: Out(bad))


GPU = {"gpu_uuid": "GPU-x", "gpu_memory_used_bytes": 2.9e9, "gpu_memory_total_bytes": 8.6e9,
       "gpu_temperature_c": 59.0, "gpu_utilization_percent": 0.0}


def test_observation_merges_real_gpu_probe_and_vllm_metrics():
    s = Server()
    try:
        t = RealTelemetry(lambda: dict(GPU), VllmClient(s.url, "facebook/opt-125m", timeout=2))
        obs = t.metrics()
    finally:
        s.httpd.shutdown()
    assert obs["gpu_memory_used_bytes"] == 2.9e9 and obs["gpu_uuid"] == "GPU-x"
    assert obs["inference_probe_ok"] is True and obs["inference_probe_error"] is None
    assert obs["vllm_metrics_available"] is True and obs["vllm_requests_waiting"] == 0.0


def test_vllm_down_is_data_not_an_outage_and_no_vllm_value_is_invented():
    t = RealTelemetry(lambda: dict(GPU), VllmClient("http://127.0.0.1:1", "m", timeout=0.3))
    obs = t.metrics()
    assert obs["gpu_memory_used_bytes"] == 2.9e9           # GPU still observed
    assert obs["inference_probe_ok"] is False and obs["inference_probe_error"]
    assert obs["vllm_metrics_available"] is False
    assert not any(k.startswith("vllm_requests") for k in obs)


def test_gpu_failure_is_a_telemetry_outage_so_the_engine_degrades():
    def broken():
        raise TelemetryError("nvidia-smi unavailable")

    with pytest.raises(TelemetryError):
        RealTelemetry(broken, VllmClient("http://127.0.0.1:1", "m", timeout=0.3)).metrics()


def test_evidence_sources_name_the_real_origins():
    t = RealTelemetry(lambda: dict(GPU), VllmClient("http://127.0.0.1:1", "m"))
    assert t.sources == {"gpu_memory_used_bytes": "nvidia-smi", "inference_probe": "vllm-probe",
                         "vllm_metrics": "vllm-metrics"}
