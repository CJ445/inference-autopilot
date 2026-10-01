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
            on_executing=None, verify=None):
    """Run an approved proposal from POLICY_CHECK and verify the real outcome.

    on_executing runs after the EXECUTING transition and before any mutation, so callers
    can persist that a change may be in flight.
    """
    incident.transition("APPROVED")
    incident.transition("EXECUTING")
    if on_executing:
        on_executing()
    workload = proposal["parameters"]["workload"]
    try:
        before = cluster.get_workload(workload)["id"]
    except ClusterError as e:
        audit.append("precondition_failed", {"workload": workload, "reason": str(e)})
        incident.transition("EXECUTION_FAILED")
        return
    audit.append("remediation_started", {"action": proposal["action"], "workload": workload})
    try:
        result = cluster.restart_workload(workload)
    except Exception as e:  # the outcome is unknown: close as failed, never verify or resolve
        audit.append("remediation_failed", {"workload": workload, "error": str(e)[:300]})
        incident.transition("EXECUTION_FAILED")
        return
    audit.append("remediation_finished", {"api_result": result})

    incident.transition("VERIFYING")
    verify = verify or (lambda w, b: _verify(cluster, w, b, limits))
    checks = _verify_until(verify, workload, before, timeout, interval)
    audit.append("verification_finished", {"checks": checks})
    incident.transition("RESOLVED" if _passed(checks) else "UNRESOLVED")


def remediate(incident, proposal, cluster, approved, audit, limits, timeout=60, interval=2):
    proposal = propose(incident, proposal, audit, approved)
    if proposal and evaluate(proposal, approved) == "AUTO":
        execute(incident, proposal, cluster, audit, limits, timeout, interval)


def _passed(checks):
    """Fail closed: resolved only by a non-empty set of checks that are ALL exactly True."""
    return isinstance(checks, dict) and bool(checks) and all(v is True for v in checks.values())


def _verify_until(verify, workload, id_before, timeout, interval):
    """Poll until recovery is observed or the timeout expires; return the last checks."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            checks = verify(workload, id_before)
        except Exception:  # a crashing verifier is a failed verification, not a stuck incident
            checks = {"verification_error": False}
        if _passed(checks) or time.monotonic() >= deadline:
            return checks
        time.sleep(interval)


def _verify(cluster, workload, id_before, limits):
    """Judge recovery from observed state, never from the executor's return value."""
    try:
        state, metrics = cluster.get_workload(workload), cluster.metrics()
    except TelemetryError:
        return {"telemetry_available": False}
    except ClusterError:
        return {"workload_available": False}
    return {
        "workload_restarted": state["id"] != id_before,
        "workload_ready": state["ready"],
        "gpu_memory_ok": metrics["gpu_memory_used_bytes"] < limits["gpu_memory_used_bytes"],
        "error_rate_ok": metrics["error_rate"] < limits["error_rate"],
    }
