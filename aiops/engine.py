from aiops.audit import AuditLog
from aiops.detector import detect_gpu_memory_pressure
from aiops.incident import Incident
from aiops.prometheus import TelemetryError
from aiops.rca import diagnose
from aiops.remediate import execute, propose
from aiops.store import StoreUnavailable

CLOSED = {"RESOLVED", "UNRESOLVED", "EXECUTION_FAILED", "REJECTED"}


class NothingToApprove(Exception):
    pass


class Engine:
    """One tick: observe -> detect -> incident -> evidence -> RCA -> pending proposal."""

    def __init__(self, telemetry, cluster, config, store=None):
        self.telemetry, self.cluster, self.config, self.store = telemetry, cluster, config, store
        self.incidents, self.pending = [], {}
        self.audit, self.health = AuditLog(), "HEALTHY"
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

    def tick(self):
        try:
            observed = self.telemetry.metrics()
        except TelemetryError:
            self.health = "DEGRADED"  # never fabricate metrics
            return None
        self.health = "HEALTHY"

        detection = detect_gpu_memory_pressure(
            observed["gpu_memory_used_bytes"], self.config["gpu_threshold"])
        if detection is None:
            return None

        active = self._active(self.config["service"], "GPU_MEMORY_PRESSURE")
        if active:
            if active.status == "INSUFFICIENT_EVIDENCE":
                self._rediagnose(active, observed)
            return active
        snapshot = self._snapshot()
        incident = self._open_incident(observed)
        self._persist_or_rollback(snapshot)
        return incident

    def approve(self, incident_id):
        if incident_id not in self.pending:
            raise NothingToApprove(incident_id)
        incident = self.get(incident_id)
        c = self.config
        snapshot = self._snapshot()
        proposal = self.pending.pop(incident_id)
        self.audit.append("approval_granted", {"incident_id": incident_id})
        # consume the proposal durably before mutating: no replay after a crash, and no
        # mutation at all if the state cannot be saved (PRD §86)
        self._persist_or_rollback(snapshot)
        execute(incident, proposal, self.cluster, self.audit,
                limits={"gpu_memory_used_bytes": c["gpu_threshold"],
                        "error_rate": c["error_rate_limit"]},
                timeout=c.get("timeout", 60), interval=c.get("interval", 2),
                on_executing=self._persist)
        self._persist()

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

    def _open_incident(self, observed):
        incident = Incident(f"inc_{len(self.incidents) + 1:03d}", self.config["service"],
                            "GPU_MEMORY_PRESSURE")
        self.incidents.append(incident)
        self.audit.append("incident_created", {"incident_id": incident.incident_id})
        incident.transition("TRIAGING")
        self._diagnose(incident, observed)
        return incident

    def _diagnose(self, incident, observed):
        incident.evidence = self._evidence(incident, observed)
        rca = diagnose(incident.evidence)
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
        if diagnose(self._evidence(incident, observed))["insufficient_evidence"]:
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
                 "source_type": "real", "incident_id": incident.incident_id}
            if "gpu_uuid" in observed:
                e["resource"] = observed["gpu_uuid"]
            return e

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
