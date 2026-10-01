import re

# action -> required parameter names. Only restart_pod for the first slice.
CATALOG = {"restart_pod": {"pod"}}
K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


class ProposalError(Exception):
    pass


def validate_proposal(proposal):
    if not isinstance(proposal, dict):
        raise ProposalError("proposal must be an object")
    action = proposal.get("action")
    params = proposal.get("parameters")
    if action not in CATALOG:
        raise ProposalError(f"action not in catalog: {action!r}")
    if not isinstance(params, dict) or set(params) != CATALOG[action]:
        raise ProposalError("parameters do not match catalog")
    for value in params.values():
        if not isinstance(value, str) or not K8S_NAME.match(value):
            raise ProposalError(f"invalid parameter value: {value!r}")
    return {"action": action, "parameters": dict(params)}


def evaluate(proposal, approved):
    """Mutating actions need explicit approval (PRD §32 default)."""
    return "AUTO" if approved else "APPROVAL_REQUIRED"
