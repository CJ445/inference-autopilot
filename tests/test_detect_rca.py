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


# --- INFERENCE_UNRESPONSIVE ------------------------------------------------------------

import pytest  # noqa: E402

from aiops.detector import detect_inference_unresponsive  # noqa: E402


def test_failed_probe_fires_the_inference_unresponsive_detector():
    d = detect_inference_unresponsive({"inference_probe_ok": False,
                                       "inference_probe_error": "timeout"})
    assert d["detector_id"] == "inference_unresponsive"
    assert d["signal"] == "inference_probe" and d["observed"] == "timeout"
    assert d["severity"] == "high"


def test_successful_probe_does_not_fire_it():
    assert detect_inference_unresponsive({"inference_probe_ok": True,
                                          "inference_probe_error": None}) is None


def test_observation_without_a_probe_never_fires_it():
    # the CPU stand-in / Prometheus observation has no inference probe at all
    assert detect_inference_unresponsive({"gpu_memory_used_bytes": 9e9}) is None


def ev2(metric, relation="supports"):
    return {"evidence_id": f"e_{metric}", "metric": metric, "relation": relation}


def test_unresponsive_rca_needs_the_failed_probe_plus_corroboration():
    rca = diagnose([ev2("inference_probe"), ev2("gpu_memory_used_bytes")],
                   "INFERENCE_UNRESPONSIVE")
    assert rca["root_cause"]["category"] == "INFERENCE_UNRESPONSIVE"
    assert rca["evidence_ids"] == ["e_inference_probe", "e_gpu_memory_used_bytes"]
    assert rca["insufficient_evidence"] is False


def test_unresponsive_rca_never_claims_memory_pressure_as_the_cause():
    rca = diagnose([ev2("inference_probe"), ev2("vllm_metrics")], "INFERENCE_UNRESPONSIVE")
    statement = rca["root_cause"]["statement"].lower()
    assert "exhaust" not in statement and "caused" not in statement
    assert rca["root_cause"]["category"] != "GPU_MEMORY_PRESSURE"


def test_unresponsive_rca_without_the_probe_signal_is_insufficient():
    rca = diagnose([ev2("gpu_memory_used_bytes"), ev2("vllm_metrics")],
                   "INFERENCE_UNRESPONSIVE")
    assert rca["insufficient_evidence"] is True


def test_unresponsive_rca_ignores_a_probe_that_contradicts():
    rca = diagnose([ev2("inference_probe", "contradicts"), ev2("gpu_memory_used_bytes"),
                    ev2("vllm_metrics")], "INFERENCE_UNRESPONSIVE")
    assert rca["insufficient_evidence"] is True


def test_default_category_keeps_the_existing_gpu_memory_pressure_rule():
    assert diagnose([ev2("gpu_memory_used_bytes"), ev2("vllm_allocation_failure")])[
        "root_cause"]["category"] == "GPU_MEMORY_PRESSURE"


def test_unknown_category_is_rejected():
    with pytest.raises(ValueError):
        diagnose([], "SOMETHING_ELSE")
