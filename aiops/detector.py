def detect_gpu_memory_pressure(observed, threshold):
    if observed <= threshold:
        return None
    return {
        "detector_id": "gpu_memory_pressure",
        "signal": "gpu_memory_used_bytes",
        "threshold": threshold,
        "observed": observed,
        "severity": "high",
    }
