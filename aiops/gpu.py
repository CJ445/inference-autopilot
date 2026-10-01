"""Real GPU telemetry from nvidia-smi. Raises TelemetryError rather than guessing."""
import subprocess

from aiops.prometheus import TelemetryError

MIB = 2**20


def read_gpu(run=subprocess.run, gpu_index=0):
    try:
        out = run(["nvidia-smi", "-i", str(gpu_index),
                   "--query-gpu=uuid,memory.used,memory.total,temperature.gpu,utilization.gpu",
                   "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, check=True, timeout=10).stdout
        uuid, used, total, temp, util = (v.strip() for v in out.strip().split(","))
        return {"gpu_uuid": uuid,
                "gpu_memory_used_bytes": float(used) * MIB,
                "gpu_memory_total_bytes": float(total) * MIB,
                "gpu_temperature_c": float(temp), "gpu_utilization_percent": float(util)}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise TelemetryError(f"gpu telemetry unavailable: {e}") from e
