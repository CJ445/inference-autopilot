import time

from aiops.kubectl import ClusterError
from aiops.policy import ProposalError, evaluate, validate_proposal
from aiops.prometheus import TelemetryError


def propose(incident, proposal, audit, approved=False):
    """Validate and policy-check a proposal. Returns it, or None if invalid."""
    try:
        proposal = validate_proposal(proposal)
    except ProposalError as e:
        audit.append("remediation_rejected", {"reason": str(e)})
        return None

    audit.append("remediation_proposed", proposal)
    incident.transition("PROPOSED")
    incident.transition("POLICY_CHECK")
    audit.append("policy_evaluated", {"decision": evaluate(proposal, approved)})
    return proposal


def execute(incident, proposal, cluster, audit, limits, timeout=60, interval=2,
            on_executing=None):
    """Run an approved proposal from POLICY_CHECK and verify the real outcome.

    on_executing runs after the EXECUTING transition and before any mutation, so callers
    can persist that a change may be in flight.
    """
    incident.transition("APPROVED")
    incident.transition("EXECUTING")
    if on_executing:
        on_executing()
    pod = proposal["parameters"]["pod"]
    try:
        before = cluster.get_pod(pod)["uid"]
    except ClusterError as e:
        audit.append("precondition_failed", {"pod": pod, "reason": str(e)})
        incident.transition("EXECUTION_FAILED")
        return
    audit.append("remediation_started", {"action": proposal["action"], "pod": pod})
    result = cluster.restart_pod(pod)
    audit.append("remediation_finished", {"api_result": result})

    incident.transition("VERIFYING")
    checks = _verify_until(cluster, pod, before, limits, timeout, interval)
    audit.append("verification_finished", {"checks": checks})
    incident.transition("RESOLVED" if all(checks.values()) else "UNRESOLVED")


def remediate(incident, proposal, cluster, approved, audit, limits, timeout=60, interval=2):
    proposal = propose(incident, proposal, audit, approved)
    if proposal and evaluate(proposal, approved) == "AUTO":
        execute(incident, proposal, cluster, audit, limits, timeout, interval)


def _verify_until(cluster, pod, uid_before, limits, timeout, interval):
    """Poll until recovery is observed or the timeout expires; return the last checks."""
    deadline = time.monotonic() + timeout
    while True:
        checks = _verify(cluster, pod, uid_before, limits)
        if all(checks.values()) or time.monotonic() >= deadline:
            return checks
        time.sleep(interval)


def _verify(cluster, pod, uid_before, limits):
    """Judge recovery from observed state, never from the executor's return value."""
    try:
        state, metrics = cluster.get_pod(pod), cluster.metrics()
    except TelemetryError:
        return {"telemetry_available": False}
    except ClusterError:
        return {"pod_available": False}
    return {
        "pod_replaced": state["uid"] != uid_before,
        "pod_ready": state["ready"],
        "gpu_memory_ok": metrics["gpu_memory_used_bytes"] < limits["gpu_memory_used_bytes"],
        "error_rate_ok": metrics["error_rate"] < limits["error_rate"],
    }
