import json
import re
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiops.engine import NothingToApprove
from aiops.store import StoreUnavailable

ROUTE = re.compile(
    r"^/api/v1/incidents(?:/(?P<id>[^/]+)(?P<action>/remediation/(?:approve|reject))?)?$")


def make_server(engine, host="127.0.0.1", port=8080, lock=None):
    """Local control API (PRD §55). One lock serialises access to the engine."""
    lock = lock or threading.Lock()

    def view(incident):
        return {**incident.to_dict(), "proposal": engine.pending.get(incident.incident_id)}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            with lock:
                try:
                    self._dispatch(method)
                except StoreUnavailable:
                    traceback.print_exc()
                    self._error(503, "DEPENDENCY_ERROR", "Persistence unavailable; no change made.")
                except Exception:  # no internals or stack traces to the client (PRD §139)
                    traceback.print_exc()  # server-side log only
                    self._error(500, "INTERNAL_ERROR", "Internal error.")

        def _dispatch(self, method):
            if method == "GET" and self.path == "/health":
                return self._send(200, {"status": engine.health})
            m = ROUTE.match(self.path)
            if not m:
                return self._error(404, "NOT_FOUND", "unknown path")
            incident_id, action = m.group("id"), m.group("action")
            if incident_id is None:
                if method != "GET":
                    return self._error(404, "NOT_FOUND", "unknown path")
                return self._send(200, {"incidents": [view(i) for i in engine.incidents]})
            incident = engine.get(incident_id)
            if incident is None:
                return self._error(404, "NOT_FOUND", "incident not found")
            if action is None:
                if method != "GET":
                    return self._error(404, "NOT_FOUND", "unknown path")
                return self._send(200, view(incident))
            if method != "POST":
                return self._error(404, "NOT_FOUND", "unknown path")
            try:
                (engine.approve if action.endswith("approve") else engine.reject)(incident_id)
            except NothingToApprove:
                return self._error(409, "POLICY_DENIED",
                                   "No pending remediation for this incident.")
            self._send(200, view(incident))

        def _error(self, status, code, message):
            self._send(status, {"error": {"code": code, "message": message,
                                          "request_id": f"req_{uuid.uuid4().hex[:12]}"}})

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
