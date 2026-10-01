from aiops.audit import AuditLog
from aiops.incident import Incident
from aiops.remediate import remediate

PROPOSAL = {"action": "restart_pod", "parameters": {"pod": "vllm-0"}}
LIMITS = {"gpu_memory_used_bytes": 7_500_000_000, "error_rate": 0.05}


class Cluster:
    """Stateful cluster: restart really replaces the pod; metrics recover or not."""

    def __init__(self, restart_works=True, recovers=True):
        self.uid, self.ready, self.restarts = "abc", True, 0
        self.restart_works, self.recovers = restart_works, recovers

    def get_pod(self, name):
        return {"uid": self.uid, "ready": self.ready}

    def restart_pod(self, name):
        self.restarts += 1
        if self.restart_works:
            self.uid = f"uid-{self.restarts}"
        return "ok"  # API success says nothing about real state

    def metrics(self):
        if self.recovers:
            return {"gpu_memory_used_bytes": 4_000_000_000, "error_rate": 0.0}
        return {"gpu_memory_used_bytes": 7_900_000_000, "error_rate": 0.4}


def diagnosed():
    inc = Incident("inc_001", "vllm", "GPU_MEMORY_PRESSURE")
    inc.transition("TRIAGING")
    inc.transition("DIAGNOSED")
    return inc


def test_unapproved_remediation_does_not_touch_cluster():
    inc, cluster = diagnosed(), Cluster()
    remediate(inc, PROPOSAL, cluster, approved=False, audit=AuditLog(), limits=LIMITS)
    assert inc.status == "POLICY_CHECK"
    assert cluster.restarts == 0


def test_verified_recovery_resolves_incident():
    inc, cluster = diagnosed(), Cluster()
    remediate(inc, PROPOSAL, cluster, approved=True, audit=AuditLog(), limits=LIMITS)
    assert inc.status == "RESOLVED"
    assert cluster.uid != "abc"


def test_api_ok_without_state_change_is_not_resolved():
    inc, cluster = diagnosed(), Cluster(restart_works=False)
    remediate(inc, PROPOSAL, cluster, approved=True, audit=AuditLog(), limits=LIMITS,
              timeout=0.1, interval=0.01)
    assert inc.status == "UNRESOLVED"


def test_pod_replaced_but_metrics_not_recovered_is_not_resolved():
    inc, cluster = diagnosed(), Cluster(recovers=False)
    remediate(inc, PROPOSAL, cluster, approved=True, audit=AuditLog(), limits=LIMITS,
              timeout=0.1, interval=0.01)
    assert inc.status == "UNRESOLVED"


def test_invalid_proposal_is_rejected_before_any_execution():
    inc, cluster = diagnosed(), Cluster()
    bad = {"action": "shell", "parameters": {"cmd": "reboot"}}
    remediate(inc, bad, cluster, approved=True, audit=AuditLog(), limits=LIMITS)
    assert cluster.restarts == 0
    assert inc.status == "DIAGNOSED"


def test_audit_records_policy_execution_and_verification():
    inc, audit = diagnosed(), AuditLog()
    remediate(inc, PROPOSAL, Cluster(), approved=True, audit=audit, limits=LIMITS)
    kinds = [e["event"] for e in audit.events]
    assert kinds == ["remediation_proposed", "policy_evaluated", "remediation_started",
                     "remediation_finished", "verification_finished"]
    assert audit.verify()


def test_audit_chain_detects_tampering():
    audit = AuditLog()
    audit.append("a", {"x": 1})
    audit.append("b", {"x": 2})
    audit.events[0]["data"]["x"] = 999
    assert audit.verify() is False


class SlowRecovery(Cluster):
    """Pod comes back Ready, and metrics recover, only after a few polls."""

    def __init__(self, polls_needed):
        super().__init__()
        self.polls_left = polls_needed

    def get_pod(self, name):
        if self.restarts and self.polls_left > 0:
            self.polls_left -= 1
            return {"uid": self.uid, "ready": False}
        return super().get_pod(name)


def test_verification_waits_for_slow_recovery():
    inc, cluster = diagnosed(), SlowRecovery(polls_needed=3)
    remediate(inc, PROPOSAL, cluster, approved=True, audit=AuditLog(), limits=LIMITS,
              timeout=2, interval=0.01)
    assert inc.status == "RESOLVED"


def test_verification_times_out_when_recovery_never_happens():
    inc, cluster = diagnosed(), SlowRecovery(polls_needed=10**9)
    remediate(inc, PROPOSAL, cluster, approved=True, audit=AuditLog(), limits=LIMITS,
              timeout=0.1, interval=0.01)
    assert inc.status == "UNRESOLVED"


def test_telemetry_gap_during_recovery_is_retried_not_fatal():
    from aiops.prometheus import TelemetryError

    class Gap(Cluster):
        gaps = 2

        def metrics(self):
            if self.gaps:
                self.gaps -= 1
                raise TelemetryError("no data for gpu_memory_used_bytes")
            return super().metrics()

    inc = diagnosed()
    remediate(inc, PROPOSAL, Gap(), approved=True, audit=AuditLog(), limits=LIMITS,
              timeout=2, interval=0.01)
    assert inc.status == "RESOLVED"


def test_pod_briefly_missing_while_statefulset_recreates_it_is_retried():
    from aiops.kubectl import ClusterError

    class Recreating(Cluster):
        missing = 3

        def get_pod(self, name):
            if self.restarts and self.missing:
                self.missing -= 1
                raise ClusterError('pods "vllm-0" not found')
            return super().get_pod(name)

    inc = diagnosed()
    remediate(inc, PROPOSAL, Recreating(), approved=True, audit=AuditLog(), limits=LIMITS,
              timeout=2, interval=0.01)
    assert inc.status == "RESOLVED"


def test_pod_that_never_comes_back_is_unresolved_not_a_crash():
    from aiops.kubectl import ClusterError

    class Gone(Cluster):
        def get_pod(self, name):
            if self.restarts:
                raise ClusterError("not found")
            return super().get_pod(name)

    inc = diagnosed()
    remediate(inc, PROPOSAL, Gone(), approved=True, audit=AuditLog(), limits=LIMITS,
              timeout=0.1, interval=0.01)
    assert inc.status == "UNRESOLVED"


def test_missing_target_before_execution_fails_without_restarting():
    from aiops.kubectl import ClusterError

    class NoPod(Cluster):
        def get_pod(self, name):
            raise ClusterError('pods "vllm-0" not found')

    inc, cluster, audit = diagnosed(), NoPod(), AuditLog()
    remediate(inc, PROPOSAL, cluster, approved=True, audit=audit, limits=LIMITS)
    assert cluster.restarts == 0
    assert inc.status == "EXECUTION_FAILED"
    assert audit.events[-1]["event"] == "precondition_failed"


def test_propose_stops_at_policy_check_and_touches_nothing():
    from aiops.remediate import propose

    inc, cluster, audit = diagnosed(), Cluster(), AuditLog()
    proposal = propose(inc, PROPOSAL, audit)
    assert inc.status == "POLICY_CHECK"
    assert proposal == PROPOSAL
    assert cluster.restarts == 0
    assert [e["event"] for e in audit.events] == ["remediation_proposed", "policy_evaluated"]


def test_propose_rejects_invalid_proposal_and_leaves_incident_diagnosed():
    from aiops.remediate import propose

    inc, audit = diagnosed(), AuditLog()
    assert propose(inc, {"action": "shell", "parameters": {}}, audit) is None
    assert inc.status == "DIAGNOSED"


def test_execute_resumes_a_policy_checked_incident_to_resolved():
    from aiops.remediate import execute, propose

    inc, cluster, audit = diagnosed(), Cluster(), AuditLog()
    proposal = propose(inc, PROPOSAL, audit)
    execute(inc, proposal, cluster, audit, LIMITS, timeout=1, interval=0.01)
    assert inc.status == "RESOLVED"
    assert cluster.uid != "abc"


def test_execute_signals_executing_before_it_mutates_anything():
    from aiops.remediate import execute, propose

    inc, cluster, audit = diagnosed(), Cluster(), AuditLog()
    seen = []
    proposal = propose(inc, PROPOSAL, audit)
    execute(inc, proposal, cluster, audit, LIMITS, timeout=1, interval=0.01,
            on_executing=lambda: seen.append((inc.status, cluster.restarts)))
    assert seen == [("EXECUTING", 0)]
