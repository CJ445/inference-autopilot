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


def detect_inference_unresponsive(observed):
    """A real inference probe failed. Needs no GPU pressure; absent probe never fires."""
    if observed.get("inference_probe_ok") is not False:
        return None
    return {
        "detector_id": "inference_unresponsive",
        "signal": "inference_probe",
        "observed": observed.get("inference_probe_error"),
        "severity": "high",
    }
