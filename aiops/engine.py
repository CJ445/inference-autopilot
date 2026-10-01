from datetime import datetime, timezone

from aiops.audit import AuditLog
from aiops.detector import detect_gpu_memory_pressure, detect_inference_unresponsive
from aiops.incident import Incident
from aiops.kubectl import ClusterError
from aiops.prometheus import TelemetryError
from aiops.rca import diagnose
from aiops.remediate import execute, propose
from aiops.store import StoreUnavailable
from aiops.verify import real_verifier

CLOSED = {"RESOLVED", "UNRESOLVED", "EXECUTION_FAILED", "REJECTED", "CLEARED"}


# A workload is "held" while an incident has a proposal waiting or a remediation running.
INFLIGHT = {"APPROVED", "EXECUTING", "VERIFYING"}
HOLDING = {"PROPOSED", "POLICY_CHECK"} | INFLIGHT


class NothingToApprove(Exception):
    pass


class ConditionGone(Exception):
    """The problem was no longer present when the operator approved; nothing was restarted."""


class WorkloadBusy(Exception):
    """Another incident holds this workload; at most one remediation per workload (PRD §130)."""


# A single failed inference probe is not an incident: it must fail on this many CONSECUTIVE ticks
# (the transition when a workload comes up, or one dropped request, otherwise opens a restart
# proposal for a workload that is fine). At the 5 s default tick that is about one extra tick of
# detection latency. GPU memory pressure is unaffected.
UNRESPONSIVE_TICKS = 2


class Engine:
    """One tick: observe -> detect -> incident -> evidence -> RCA -> pending proposal."""

    def __init__(self, telemetry, cluster, config, store=None, presence=None, mode="REAL"):
        self.telemetry, self.cluster, self.config, self.store = telemetry, cluster, config, store
        self.mode = mode              # "REAL" or "SIMULATION": stamped on audit, incidents, evidence
        self.presence = presence      # optional read-only "is the workload there?" (docker only)
        self.workload_state = None    # 'absent'|'stopped'|'starting'|'running'|'unknown'|None
        self._unresponsive_streak = 0  # consecutive ticks on which the inference probe failed
        self.incidents, self.pending = [], {}
        self.audit, self.health = AuditLog(None if mode == "REAL" else mode), "HEALTHY"
        self.last_observation, self.last_observed_at = None, None
        if store:
            self.incidents, self.pending, self.audit = store.load()
            self._close_interrupted()

    def _close_interrupted(self):
        """A crash mid-remediation leaves an outcome nobody verified; never call it resolved."""
        interrupted = [i for i in self.incidents if i.status == "EXECUTING"]
        for incident in interrupted:
            self.audit.append("interrupted_by_restart", {"incident_id": incident.incident_id})
            incident.transition("EXECUTION_FAILED")
        if interrupted:
            self._persist()

    def _persist(self):
        if self.store:
            self.store.save(self.incidents, self.pending, self.audit)

    def _snapshot(self):
        return len(self.incidents), len(self.audit.events), dict(self.pending)

    def _persist_or_rollback(self, snapshot, undo=lambda: None):
        """Fail closed: if state cannot be made durable, leave no trace of the attempt."""
        try:
            self._persist()
        except StoreUnavailable:
            n_incidents, n_events, pending = snapshot
            del self.incidents[n_incidents:]
            del self.audit.events[n_events:]
            self.pending = pending
            undo()
            raise

    def record(self, event, data):
        """Append an externally observed event (for example a fault lease change) to the audit
        chain, durably; nothing is left half-recorded if persistence fails. The caller holds the
        engine's write lock."""
        snapshot = self._snapshot()
        self.audit.append(event, data)
        self._persist_or_rollback(snapshot)

    def _workload_state(self):
        if self.presence is None:
            return None
        try:
            return self.presence()
        except ClusterError:
            return "unknown"          # ambiguous/mismatched/docker down: not "absent"; fail closed below

    def tick(self):
        self.workload_state = self._workload_state()
        if self.workload_state in ("absent", "stopped"):
            # The workload is operator-controlled and not there: nothing to observe, detect or
            # remediate. No incident, no proposal, and no stale reading kept as if it were current.
            self.health = "HEALTHY"
            self.last_observation = self.last_observed_at = None
            self._unresponsive_streak = 0     # the workload is gone: nothing carries over
            return None
        try:
            observed = self.telemetry.metrics()
        except TelemetryError:
            self.health = "DEGRADED"  # never fabricate metrics
            self._unresponsive_streak = 0     # no valid observation: the streak is broken
            return None
        self.health = "HEALTHY"
        self.last_observation = observed
        self.last_observed_at = datetime.now(timezone.utc).isoformat()
        if self.workload_state == "starting":
            # Inside Docker's own start period (the model is loading): observed and shown, but
            # not yet expected to answer, so a failing probe is not an incident. Detection resumes
            # the moment Docker reports it healthy or unhealthy.
            self._unresponsive_streak = 0
            return None

        category = self._detect(observed)
        self._clear_gone_conditions(category)
        if category is None:
            return None

        holder = self._holder()
        if holder and holder.category != category:
            # a second condition on a held workload never opens a second incident or proposal
            self._note_suppressed(holder, category)
            return holder

        active = self._active(self.config["service"], category)
        if active:
            if active.status == "INSUFFICIENT_EVIDENCE":
                self._rediagnose(active, observed)
            return active
        snapshot = self._snapshot()
        incident = self._open_incident(observed, category)
        self._persist_or_rollback(snapshot)
        return incident

    def _detect(self, observed):
        """Memory pressure keeps precedence; an unresponsive probe is its own condition, and only
        once it has failed on UNRESPONSIVE_TICKS consecutive ticks (a success resets the count)."""
        failing = bool(detect_inference_unresponsive(observed))
        self._unresponsive_streak = self._unresponsive_streak + 1 if failing else 0
        if detect_gpu_memory_pressure(observed["gpu_memory_used_bytes"],
                                      self.config["gpu_threshold"]):
            return "GPU_MEMORY_PRESSURE"
        if failing and self._unresponsive_streak >= UNRESPONSIVE_TICKS:
            return "INFERENCE_UNRESPONSIVE"
        return None

    def _clear_gone_conditions(self, current):
        """An incident stuck at INSUFFICIENT_EVIDENCE whose condition is gone closes as CLEARED.

        CLEARED is not RESOLVED: nothing was remediated or verified.
        """
        stale = [i for i in self.incidents
                 if i.status == "INSUFFICIENT_EVIDENCE" and i.category != current]
        if not stale:
            return
        snapshot = self._snapshot()
        marks = [(i, i.status, len(i.timeline)) for i in stale]
        for incident in stale:
            self.audit.append("incident_cleared", {"incident_id": incident.incident_id})
            incident.transition("CLEARED")

        def undo():
            for incident, status, n_timeline in marks:
                incident.status = status
                del incident.timeline[n_timeline:]

        self._persist_or_rollback(snapshot, undo)

    def _holder(self):
        return next((i for i in self.incidents
                     if i.service == self.config["service"] and i.status in HOLDING), None)

    def _note_suppressed(self, holder, category):
        data = {"incident_id": holder.incident_id, "category": category}
        if any(e["event"] == "condition_suppressed" and e["data"] == data
               for e in self.audit.events):
            return  # visible once, never spammed
        snapshot = self._snapshot()
        self.audit.append("condition_suppressed", data)
        self._persist_or_rollback(snapshot)

    def _conflicts(self, incident, workload):
        """Other incidents that make a restart of this workload ambiguous or unsafe."""
        return [i for i in self.incidents
                if i.incident_id != incident.incident_id and (
                    (i.service == incident.service and i.status in INFLIGHT)
                    or (i.incident_id in self.pending and
                        self.pending[i.incident_id]["parameters"]["workload"] == workload))]

    def approve(self, incident_id):
        if incident_id not in self.pending:
            raise NothingToApprove(incident_id)
        incident = self.get(incident_id)
        if self._conflicts(incident, self.pending[incident_id]["parameters"]["workload"]):
            raise WorkloadBusy(incident_id)  # fail closed: nothing is consumed or changed
        gone = self._condition_gone(incident)
        if gone:
            self._refuse_stale(incident_id, incident, gone)
            raise ConditionGone(incident_id)
        c = self.config
        snapshot = self._snapshot()
        proposal = self.pending.pop(incident_id)
        self.audit.append("approval_granted", {"incident_id": incident_id})
        # consume the proposal durably before mutating: no replay after a crash, and no
        # mutation at all if the state cannot be saved (PRD §86)
        self._persist_or_rollback(snapshot)
        verify = None
        if hasattr(self.telemetry, "vllm"):  # real vLLM path: judge recovery by real inference
            verify = real_verifier(
                self.cluster, self.telemetry, c["gpu_threshold"],
                check_memory=incident.category == "GPU_MEMORY_PRESSURE",
                stable_probes=c.get("stable_probes", 3),
                probe_interval=c.get("probe_interval", 1.0))
        execute(incident, proposal, self.cluster, self.audit,
                limits={"gpu_memory_used_bytes": c["gpu_threshold"],
                        "error_rate": c.get("error_rate_limit", 0.05)},
                timeout=c.get("timeout", 60), interval=c.get("interval", 2),
                on_executing=self._persist, verify=verify)
        self._unresponsive_streak = 0         # a restart makes it a new instance: count afresh
        self._persist()

    def _condition_gone(self, incident):
        """Why a proposal made earlier must NOT be executed now, or None if the problem persists.

        Judged from the latest observation (a tick old at most). A restart interrupts inference, so
        it is never run against a model that has answered since, a memory reading that is back
        under its limit, or a workload that is not there (the stale-proposal case: the fault ended
        while the incident waited for the operator).
        """
        if self.workload_state in ("absent", "stopped"):
            return f"the workload is {self.workload_state}"
        observed = self.last_observation
        if observed is None:
            return "there is no current observation"
        if incident.category == "INFERENCE_UNRESPONSIVE" and not detect_inference_unresponsive(observed):
            return "the model is answering again"
        if incident.category == "GPU_MEMORY_PRESSURE" and not detect_gpu_memory_pressure(
                observed["gpu_memory_used_bytes"], self.config["gpu_threshold"]):
            return "GPU memory is back under its limit"
        return None

    def _refuse_stale(self, incident_id, incident, reason):
        snapshot, status, n_timeline = self._snapshot(), incident.status, len(incident.timeline)
        proposal = self.pending.pop(incident_id)
        self.audit.append("approval_refused", {"incident_id": incident_id, "reason": reason})
        incident.transition("CLEARED")

        def undo():
            self.pending[incident_id] = proposal
            incident.status = status
            del incident.timeline[n_timeline:]

        self._persist_or_rollback(snapshot, undo)

    def reject(self, incident_id):
        if incident_id not in self.pending:
            raise NothingToApprove(incident_id)
        incident = self.get(incident_id)
        snapshot, status, n_timeline = self._snapshot(), incident.status, len(incident.timeline)
        del self.pending[incident_id]
        self.audit.append("approval_denied", {"incident_id": incident_id})
        incident.transition("REJECTED")

        def undo():
            incident.status = status
            del incident.timeline[n_timeline:]

        self._persist_or_rollback(snapshot, undo)

    def get(self, incident_id):
        return next((i for i in self.incidents if i.incident_id == incident_id), None)

    def _active(self, service, category):
        return next((i for i in self.incidents
                     if i.service == service and i.category == category
                     and i.status not in CLOSED), None)

    def _open_incident(self, observed, category):
        incident = Incident(f"inc_{len(self.incidents) + 1:03d}", self.config["service"],
                            category, None if self.mode == "REAL" else self.mode)
        self.incidents.append(incident)
        self.audit.append("incident_created", {"incident_id": incident.incident_id})
        incident.transition("TRIAGING")
        self._diagnose(incident, observed)
        return incident

    def _diagnose(self, incident, observed):
        incident.evidence = self._evidence(incident, observed)
        rca = diagnose(incident.evidence, incident.category)
        self.audit.append("rca_generated", {"incident_id": incident.incident_id,
                                            "rca": rca})
        incident.transition("DIAGNOSED")
        if rca["insufficient_evidence"]:
            incident.transition("INSUFFICIENT_EVIDENCE")
        else:
            proposal = propose(incident, {"action": "restart_workload",
                                          "parameters": {"workload": self.config["workload"]}},
                               self.audit)
            if proposal:
                self.pending[incident.incident_id] = proposal

    def _rediagnose(self, incident, observed):
        """New evidence may now support a diagnosis; unchanged evidence changes nothing."""
        if diagnose(self._evidence(incident, observed), incident.category)[
                "insufficient_evidence"]:
            return
        snapshot = self._snapshot()
        status, evidence, n_timeline = incident.status, incident.evidence, len(incident.timeline)
        incident.transition("TRIAGING")
        self._diagnose(incident, observed)

        def undo():
            incident.status, incident.evidence = status, evidence
            del incident.timeline[n_timeline:]

        self._persist_or_rollback(snapshot, undo)

    def _evidence(self, incident, observed):
        queries = getattr(self.telemetry, "queries", {})
        sources = getattr(self.telemetry, "sources", {})

        def ev(n, metric, value, supports, query_key=None):
            e = {"evidence_id": f"{incident.incident_id}_ev_{n}",
                 "source": sources.get(metric, "prometheus"), "metric": metric,
                 "query": queries.get(query_key or metric), "value": value,
                 "relation": "supports" if supports else "contradicts",
                 "source_type": "real" if self.mode == "REAL" else self.mode.lower(),
                 "incident_id": incident.incident_id}
            if "gpu_uuid" in observed:
                e["resource"] = observed["gpu_uuid"]
            return e

        if incident.category == "INFERENCE_UNRESPONSIVE":
            used = observed["gpu_memory_used_bytes"]
            return [  # each is a real observation; relation is relative to this category
                ev(1, "gpu_memory_used_bytes", used, used <= self.config["gpu_threshold"]),
                ev(2, "inference_probe",
                   {"ok": observed["inference_probe_ok"],
                    "latency_ms": observed["inference_probe_latency_ms"],
                    "error": observed["inference_probe_error"]},
                   not observed["inference_probe_ok"]),
                ev(3, "vllm_metrics", {"available": observed["vllm_metrics_available"]},
                   not observed["vllm_metrics_available"]),
            ]

        evidence = [ev(1, "gpu_memory_used_bytes", observed["gpu_memory_used_bytes"], True)]
        if "inference_probe_ok" in observed:  # real vLLM path: a live inference probe
            evidence.append(ev(2, "inference_probe",
                               {"ok": observed["inference_probe_ok"],
                                "latency_ms": observed["inference_probe_latency_ms"],
                                "error": observed["inference_probe_error"]},
                               not observed["inference_probe_ok"]))
        elif "allocation_failures_total" in observed:  # CPU stand-in path
            evidence.append(ev(2, "vllm_allocation_failure",
                               observed["allocation_failures_total"],
                               observed["allocation_failures_total"] > 0,
                               query_key="allocation_failures_total"))
        return evidence
