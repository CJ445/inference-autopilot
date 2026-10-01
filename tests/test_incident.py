import pytest

from aiops.incident import Incident, InvalidTransition


def test_incident_follows_happy_path_to_resolved():
    inc = Incident("inc_001", service="vllm", category="GPU_MEMORY_PRESSURE")
    for state in ["TRIAGING", "DIAGNOSED", "PROPOSED", "POLICY_CHECK",
                  "APPROVED", "EXECUTING", "VERIFYING", "RESOLVED"]:
        inc.transition(state)
    assert inc.status == "RESOLVED"


def test_incident_cannot_skip_to_resolved():
    inc = Incident("inc_001", service="vllm", category="GPU_MEMORY_PRESSURE")
    with pytest.raises(InvalidTransition):
        inc.transition("RESOLVED")
    assert inc.status == "DETECTED"


def test_transitions_are_recorded_in_timeline():
    inc = Incident("inc_001", service="vllm", category="GPU_MEMORY_PRESSURE")
    inc.transition("TRIAGING")
    assert [e["state"] for e in inc.timeline] == ["DETECTED", "TRIAGING"]


def test_policy_check_can_be_rejected():
    inc = Incident("inc_001", service="vllm", category="GPU_MEMORY_PRESSURE")
    for state in ["TRIAGING", "DIAGNOSED", "PROPOSED", "POLICY_CHECK", "REJECTED"]:
        inc.transition(state)
    assert inc.status == "REJECTED"
