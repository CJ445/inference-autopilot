import hashlib
import json


class AuditLog:
    """Append-only hash chain; detects accidental or unauthorized edits (PRD §60)."""

    def __init__(self):
        self.events = []

    def append(self, event, data):
        prev = self.events[-1]["hash"] if self.events else ""
        self.events.append({"event": event, "data": data, "prev": prev,
                            "hash": self._hash(event, data, prev)})

    def verify(self):
        prev = ""
        for e in self.events:
            if e["prev"] != prev or e["hash"] != self._hash(e["event"], e["data"], prev):
                return False
            prev = e["hash"]
        return True

    @staticmethod
    def _hash(event, data, prev):
        payload = json.dumps([event, data, prev], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()
