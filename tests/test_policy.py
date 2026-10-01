import pytest

from aiops.policy import evaluate, ProposalError, validate_proposal


def test_restart_workload_is_valid_and_requires_approval():
    p = validate_proposal({"action": "restart_workload", "parameters": {"workload": "vllm-0"}})
    assert evaluate(p, approved=False) == "APPROVAL_REQUIRED"


def test_approved_restart_workload_is_allowed():
    p = validate_proposal({"action": "restart_workload", "parameters": {"workload": "vllm-0"}})
    assert evaluate(p, approved=True) == "AUTO"


@pytest.mark.parametrize("bad", [
    {"action": "rm -rf /", "parameters": {}},
    {"action": "shell", "parameters": {"cmd": "kubectl delete ns kube-system"}},
    {"action": "restart_workload", "parameters": {}},
    {"action": "restart_workload", "parameters": {"workload": "vllm-0", "extra": "x"}},
    {"action": "restart_workload", "parameters": {"workload": "vllm-0; reboot"}},
    "restart_pod vllm-0",
])
def test_invalid_or_arbitrary_proposals_are_rejected(bad):
    with pytest.raises(ProposalError):
        validate_proposal(bad)
