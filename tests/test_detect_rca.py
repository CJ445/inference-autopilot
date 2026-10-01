from aiops.detector import detect_gpu_memory_pressure
from aiops.rca import diagnose


def test_detector_fires_above_threshold_and_reports_observation():
    d = detect_gpu_memory_pressure(observed=7_812_345_678, threshold=7_500_000_000)
    assert d["detector_id"] == "gpu_memory_pressure"
    assert d["observed"] == 7_812_345_678
    assert d["threshold"] == 7_500_000_000
    assert d["severity"] == "high"


def test_detector_silent_below_threshold():
    assert detect_gpu_memory_pressure(observed=1, threshold=100) is None


def ev(eid, metric, relation="supports", source_type="real"):
    return {"evidence_id": eid, "metric": metric, "relation": relation,
            "source_type": source_type}


def test_rca_with_two_supporting_evidence_cites_them():
    rca = diagnose([ev("ev_1", "gpu_memory_used_bytes"),
                    ev("ev_2", "vllm_allocation_failure")])
    assert rca["root_cause"]["category"] == "GPU_MEMORY_PRESSURE"
    assert rca["evidence_ids"] == ["ev_1", "ev_2"]
    assert rca["insufficient_evidence"] is False


def test_rca_prefers_uncertainty_with_single_signal():
    rca = diagnose([ev("ev_1", "gpu_memory_used_bytes")])
    assert rca["insufficient_evidence"] is True
    assert rca["root_cause"] is None


def test_rca_ignores_contradicting_evidence():
    rca = diagnose([ev("ev_1", "gpu_memory_used_bytes"),
                    ev("ev_2", "vllm_allocation_failure", relation="contradicts")])
    assert rca["insufficient_evidence"] is True
