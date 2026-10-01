GPU_MEMORY_SIGNALS = {"gpu_memory_used_bytes", "vllm_allocation_failure"}
MIN_SUPPORTING = 2


def diagnose(evidence):
    support = [e for e in evidence
               if e["relation"] == "supports" and e["metric"] in GPU_MEMORY_SIGNALS]
    if len(support) < MIN_SUPPORTING:
        return {"root_cause": None, "evidence_ids": [], "insufficient_evidence": True}
    return {
        "root_cause": {
            "category": "GPU_MEMORY_PRESSURE",
            "statement": "Inference workload exhausted available GPU memory.",
        },
        "evidence_ids": [e["evidence_id"] for e in support],
        "insufficient_evidence": False,
    }
