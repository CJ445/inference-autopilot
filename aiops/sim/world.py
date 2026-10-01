"""A deterministic synthetic workload: state, provider and telemetry for SIMULATION mode.

The state lives only in this object. The provider and the telemetry are thin views over it, and
the telemetry is the PRODUCTION `RealTelemetry` (same observation shape, same `metrics()`), so the
detector, evidence, RCA and the production verifier see exactly the shape they see for real.
"""
from aiops.kubectl import ClusterError
from aiops.prometheus import TelemetryError
from aiops.telemetry import RealTelemetry

WORKLOAD = "sim-vllm"
MODEL = "sim/opt-125m"
GPU_UUID = "SIM-GPU-0"
GPU_USED_BYTES = 3_035_000_000.0        # normal: below the 4.5 GB pressure threshold
GPU_TOTAL_BYTES = 8_585_740_288.0
HEALTHY_LATENCY_MS = 18.0
TIMEOUT_LATENCY_MS = 3000.0


class SimWorld:
    """The whole simulated environment. A restart changes the lifecycle generation."""

    def __init__(self, workload=WORKLOAD, model=MODEL, recovers=True):
        self.workload, self.model = workload, model
        self.recovers = recovers          # False: a restart that does not bring the model back
        self.running = True
        self.generation = 1
        self.restarts = 0
        self.inference_ok = True
        self.metrics_ok = True
        self.gpu_readable = True
        self.probe_override = None        # a malformed probe result (negative tests)
        self.probe_log = []               # every probe outcome, in order

    @property
    def identity(self):
        return f"sim:{self.workload}:generation-{self.generation}"

    def inject_unresponsive(self):
        """The fault: the model stops answering and its metrics endpoint goes dark."""
        self.inference_ok = False
        self.metrics_ok = False

    def restart(self):
        self.restarts += 1
        self.generation += 1
        if self.recovers:
            self.inference_ok = True
            self.metrics_ok = True

    def read_gpu(self):
        if not self.gpu_readable:
            raise TelemetryError("simulated GPU telemetry unavailable")
        return {"gpu_uuid": GPU_UUID, "gpu_memory_used_bytes": GPU_USED_BYTES,
                "gpu_memory_total_bytes": GPU_TOTAL_BYTES, "gpu_temperature_c": 59.0,
                "gpu_utilization_percent": 23.0}


class SimVllm:
    """What the telemetry and the verifier call: `probe()` and `metrics()`."""

    def __init__(self, world):
        self.world = world

    def probe(self):
        w = self.world
        if w.probe_override is not None:
            result = dict(w.probe_override)
        elif w.inference_ok:
            result = {"ok": True, "latency_ms": HEALTHY_LATENCY_MS, "error": None}
        else:
            result = {"ok": False, "latency_ms": TIMEOUT_LATENCY_MS,
                      "error": "timeout (simulated)"}
        w.probe_log.append(result["ok"])
        return result

    def metrics(self):
        if not self.world.metrics_ok:
            raise TelemetryError("simulated vLLM metrics unavailable")
        return {"vllm_requests_running": 0.0, "vllm_requests_waiting": 0.0,
                "vllm_kv_cache_usage": 0.0002}


class SimTelemetry(RealTelemetry):
    """Production observation shape; the evidence sources say plainly that they are simulated."""

    sources = {"gpu_memory_used_bytes": "simulated-gpu", "inference_probe": "simulated-probe",
               "vllm_metrics": "simulated-metrics"}

    def __init__(self, world):
        super().__init__(world.read_gpu, SimVllm(world))


class SimProvider:
    """Same contract and the same identity binding as the real provider, over a SimWorld."""

    def __init__(self, world):
        self._world = world

    def _check(self, name):
        if name != self._world.workload:
            raise ClusterError(f"{name!r} is not the configured workload")

    def get_workload(self, name):
        self._check(name)
        return {"id": self._world.identity, "ready": self._world.inference_ok}

    def restart_workload(self, name):
        self._check(name)
        self._world.restart()           # lifecycle identity changes; nothing else is touched
        return "ok"
