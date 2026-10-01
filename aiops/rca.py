MIN_SUPPORTING = 2

# category -> (signals that can support it, statement, signal that must be among the support)
RULES = {
    "GPU_MEMORY_PRESSURE": (
        {"gpu_memory_used_bytes", "vllm_allocation_failure", "inference_probe"},
        "Inference workload exhausted available GPU memory.", None),
    "INFERENCE_UNRESPONSIVE": (
        {"inference_probe", "vllm_metrics", "gpu_memory_used_bytes"},
        "Inference requests are failing or timing out while GPU telemetry is valid and "
        "GPU memory is within limits.", "inference_probe"),
}


def diagnose(evidence, category="GPU_MEMORY_PRESSURE"):
    if category not in RULES:
        raise ValueError(f"unknown category: {category!r}")
    signals, statement, required = RULES[category]
    support = [e for e in evidence
               if e["relation"] == "supports" and e["metric"] in signals]
    if len(support) < MIN_SUPPORTING or (
            required and required not in {e["metric"] for e in support}):
        return {"root_cause": None, "evidence_ids": [], "insufficient_evidence": True}
    return {
        "root_cause": {"category": category, "statement": statement},
        "evidence_ids": [e["evidence_id"] for e in support],
        "insufficient_evidence": False,
    }
