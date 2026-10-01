"""PRD §72 golden-scenario acceptance criteria, on API-shaped data.

The SAME function judges the simulated scenario (portable, no hardware) and the real Docker-pause
scenario (GPU), so both paths are held to identical control-loop semantics. It is deliberately
strict about the anti-fake rule (PRD §73): "RESOLVED" counts only if the lifecycle identity was
observed to change by something other than the engine, and the verification checks are the ones
the audit chain recorded and all of them are exactly True.
"""

ORDER = ["incident_created", "rca_generated", "remediation_proposed", "policy_evaluated",
         "approval_granted", "remediation_started", "remediation_finished",
         "verification_finished"]
CRITERIA = ["incident_created", "evidence_count_at_least_2", "root_cause_present",
            "root_cause_has_evidence", "remediation_proposed", "policy_decision_recorded",
            "remediation_executed", "infrastructure_state_changed", "verification_started",
            "verification_passed", "incident_state_resolved", "audit_in_order", "mode_marking"]


def evaluate(detail, events, identity_before, identity_after, mode="REAL"):
    """Returns {criterion: bool}. `detail` is GET .../incidents/{id}; `events` is the audit's
    `events`; the two identities are observed INDEPENDENTLY of the engine (docker inspect, or the
    simulated world)."""
    names = [e["event"] for e in events]
    iid = detail["incident_id"]
    first = {n: names.index(n) for n in ORDER if n in names}
    checks = (detail.get("verification") or {}).get("checks")
    recorded = next((e["data"].get("checks") for e in reversed(events)
                     if e["event"] == "verification_finished"), None)
    rca = detail.get("rca") or {}
    evidence_ids = {e["evidence_id"] for e in detail.get("evidence", [])}
    policy = next((e["data"] for e in events if e["event"] == "policy_evaluated"), {})
    if mode == "SIMULATION":
        marked = (detail.get("mode") == "SIMULATION"
                  and all(e["data"].get("mode") == "SIMULATION" for e in events)
                  and all(ev.get("source_type") == "simulation" for ev in detail["evidence"]))
    else:
        marked = ("mode" not in detail and all("mode" not in e["data"] for e in events)
                  and all(ev.get("source_type") == "real" for ev in detail["evidence"]))
    return {
        "incident_created": any(e["event"] == "incident_created"
                                and e["data"].get("incident_id") == iid for e in events),
        "evidence_count_at_least_2": len(detail.get("evidence", [])) >= 2,
        "root_cause_present": rca.get("root_cause") is not None,
        "root_cause_has_evidence": bool(rca.get("evidence_ids"))
                                   and set(rca["evidence_ids"]) <= evidence_ids,
        "remediation_proposed": "remediation_proposed" in names,
        "policy_decision_recorded": policy.get("decision") == "APPROVAL_REQUIRED"
                                    and first.get("policy_evaluated", 10**9)
                                    < first.get("approval_granted", -1),
        "remediation_executed": "remediation_started" in names and "remediation_finished" in names
                                and (detail.get("remediation") or {}).get("state") == "EXECUTED",
        "infrastructure_state_changed": identity_before is not None
                                        and identity_after is not None
                                        and identity_before != identity_after,
        "verification_started": "VERIFYING" in [t["state"] for t in detail.get("timeline", [])],
        "verification_passed": isinstance(checks, dict) and bool(checks)
                               and all(v is True for v in checks.values())
                               and checks == recorded
                               and checks.get("workload_restarted") is True
                               and checks.get("inference_probe_stable") is True,
        "incident_state_resolved": detail.get("status") == "RESOLVED",
        "audit_in_order": len(first) == len(ORDER)
                          and [first[n] for n in ORDER] == sorted(first[n] for n in ORDER),
        "mode_marking": marked,
    }


def assert_golden(detail, events, identity_before, identity_after, mode="REAL"):
    result = evaluate(detail, events, identity_before, identity_after, mode)
    failed = [name for name, ok in result.items() if not ok]
    assert not failed, f"golden scenario criteria not met: {failed}"
    return result
