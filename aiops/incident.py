from datetime import datetime, timezone

TRANSITIONS = {
    "DETECTED": {"TRIAGING"},
    "TRIAGING": {"DIAGNOSED"},
    "DIAGNOSED": {"PROPOSED", "INSUFFICIENT_EVIDENCE"},
    # re-diagnosed when new evidence arrives, or cleared when the condition disappears
    "INSUFFICIENT_EVIDENCE": {"TRIAGING", "CLEARED"},
    "PROPOSED": {"POLICY_CHECK"},
    "POLICY_CHECK": {"APPROVED", "REJECTED"},
    "APPROVED": {"EXECUTING"},
    "EXECUTING": {"VERIFYING", "EXECUTION_FAILED"},
    "VERIFYING": {"RESOLVED", "ROLLBACK_REQUIRED", "UNRESOLVED"},
    "ROLLBACK_REQUIRED": {"ROLLED_BACK"},
}


class InvalidTransition(Exception):
    pass


class Incident:
    def __init__(self, incident_id, service, category):
        self.incident_id = incident_id
        self.service = service
        self.category = category
        self.status = "DETECTED"
        self.evidence = []
        self.timeline = []
        self._record()

    def transition(self, state):
        if state not in TRANSITIONS.get(self.status, set()):
            raise InvalidTransition(f"{self.status} -> {state}")
        self.status = state
        self._record()

    def _record(self):
        self.timeline.append(
            {"state": self.status, "at": datetime.now(timezone.utc).isoformat()}
        )

    def to_dict(self):
        return {"incident_id": self.incident_id, "service": self.service,
                "category": self.category, "status": self.status,
                "evidence": self.evidence, "timeline": self.timeline}

    @classmethod
    def from_dict(cls, d):
        inc = cls.__new__(cls)
        inc.__dict__.update(d)
        return inc
