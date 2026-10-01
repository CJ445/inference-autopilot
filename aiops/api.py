import json
import re
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from aiops.engine import CLOSED, NothingToApprove, WorkloadBusy
from aiops.store import StoreUnavailable

ROUTE = re.compile(
    r"^/api/v1/incidents(?:/(?P<id>[^/]+)(?P<action>/remediation/(?:approve|reject))?)?$")


AUDIT_DEFAULT_LIMIT, AUDIT_MAX_LIMIT = 200, 1000
CONTROL_STOP = "/api/v1/control/stop"
# A browser cannot attach a custom header to a cross-origin request without a preflight this API
# never answers, so requiring it keeps a web page from stopping the control plane.
STOP_CONFIRM_HEADER, STOP_CONFIRM_VALUE = "X-Aiops-Confirm", "stop-control-plane"


def make_server(engine, host="127.0.0.1", port=8080, lock=None, info=None, status_extra=None,
                system=None):
    """Local control API (PRD §55).

    One lock serialises WRITES to the engine. Reads (GET) do not take it: a tick or a remediation
    can hold it for seconds to minutes, and an operator must be able to see live state exactly
    then. Reads only look at in-memory state and retry if it changes under them.

    `system` (optional, `aiops.system.System`) adds read-only version/config/diagnostics and the
    one confirmed control-plane stop. Without it (the legacy `serve`) those paths do not exist.
    """
    lock = lock or threading.Lock()

    def view(incident):
        return {**incident.to_dict(), "proposal": engine.pending.get(incident.incident_id)}

    def detail(incident):
        """The list view plus what the audit chain recorded: RCA, remediation and verification."""
        events = list(engine.audit.events)
        iid = incident.incident_id
        rca = next((e["data"].get("rca") for e in reversed(events)
                    if e["event"] == "rca_generated" and e["data"].get("incident_id") == iid), None)
        approvals = [n for n, e in enumerate(events)
                     if e["event"] == "approval_granted" and e["data"].get("incident_id") == iid]
        remediation, checks = {"state": "NOT_STARTED"}, None
        if approvals:
            remediation = {"state": "APPROVED"}
            for e in events[approvals[-1] + 1:]:
                if e["event"] in ("approval_granted", "incident_created"):
                    break                      # the next incident's records begin here
                if e["event"] == "remediation_started":
                    remediation = {"state": "EXECUTING"}
                elif e["event"] == "remediation_finished":
                    remediation = {"state": "EXECUTED", "api_result": e["data"].get("api_result")}
                elif e["event"] in ("remediation_failed", "precondition_failed"):
                    remediation = {"state": "FAILED", "error": e["data"].get("error")
                                   or e["data"].get("reason")}
                elif e["event"] == "verification_finished":
                    checks = e["data"].get("checks")
        return {**view(incident), "rca": rca, "remediation": remediation,
                "verification": {"checks": checks}}

    def audit_view(query):
        raw = parse_qs(query).get("limit", [str(AUDIT_DEFAULT_LIMIT)])[0]
        if not raw.isdigit() or not 1 <= int(raw) <= AUDIT_MAX_LIMIT:
            return None
        events = list(engine.audit.events)
        first = max(0, len(events) - int(raw))
        return {"valid": engine.audit.verify(), "total": len(events),
                "events": [{"seq": first + n, "event": e["event"], "data": e["data"],
                            "hash": e["hash"][:12]} for n, e in enumerate(events[first:])]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            if method == "GET" or urlsplit(self.path).path == CONTROL_STOP:
                self._guarded(method)          # reads, and a stop request, never wait for the
            else:                              # engine lock (a remediation may hold it for minutes)
                with lock:                     # writes (approve/reject) stay serialised
                    self._guarded(method)

        def _guarded(self, method):
            try:
                # Only a READ may be retried (its state changed under it). A write is NEVER
                # re-run: approve/reject execute at most once per request.
                for attempt in range(5 if method == "GET" else 1):
                    try:
                        return self._dispatch(method)
                    except RuntimeError:
                        if method != "GET" or attempt == 4:
                            raise
            except StoreUnavailable:
                traceback.print_exc()
                self._error(503, "DEPENDENCY_ERROR", "Persistence unavailable; no change made.")
            except Exception:  # no internals or stack traces to the client (PRD §139)
                traceback.print_exc()  # server-side log only
                self._error(500, "INTERNAL_ERROR", "Internal error.")

        def _dispatch(self, method):
            parts = urlsplit(self.path)
            path = parts.path
            if method == "GET" and path == "/health":
                return self._send(200, {"status": engine.health})
            if method == "GET" and path == "/api/v1/status":
                body = {"health": engine.health, "last_observed_at": engine.last_observed_at,
                        "last_observation": engine.last_observation,
                        "incidents": {"active": [i.incident_id for i in engine.incidents
                                                 if i.status not in CLOSED],
                                      "pending": list(engine.pending)},
                        "audit": {"valid": engine.audit.verify(),
                                  "events": len(engine.audit.events)},
                        "info": info or {}, "mode": engine.mode}
                if engine.workload_state is not None:      # only where the engine can tell
                    body["workload"] = {"name": engine.config.get("workload"),
                                        "state": engine.workload_state}
                if status_extra is not None:
                    try:
                        body.update(status_extra())
                    except Exception:
                        traceback.print_exc()
                        body["extra_error"] = "status provider failed"   # no internals leaked
                return self._send(200, body)
            if method == "GET" and path == "/api/v1/audit":
                body = audit_view(parts.query)
                if body is None:
                    return self._error(400, "INVALID_REQUEST",
                                       f"limit must be an integer from 1 to {AUDIT_MAX_LIMIT}")
                return self._send(200, body)
            if path in ("/api/v1/version", "/api/v1/config", "/api/v1/diagnostics", CONTROL_STOP):
                return self._system(method, path)
            m = ROUTE.match(path)
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
                return self._send(200, detail(incident))
            if method != "POST":
                return self._error(404, "NOT_FOUND", "unknown path")
            try:
                (engine.approve if action.endswith("approve") else engine.reject)(incident_id)
            except NothingToApprove:
                return self._error(409, "POLICY_DENIED",
                                   "No pending remediation for this incident.")
            except WorkloadBusy:
                return self._error(409, "POLICY_DENIED",
                                   "Another remediation holds this workload.")
            self._send(200, detail(incident))

        def _system(self, method, path):
            if system is None:
                return self._error(404, "NOT_AVAILABLE", "not available on this control plane")
            if path == CONTROL_STOP:
                if method != "POST":
                    return self._error(404, "NOT_FOUND", "unknown path")
                if self.headers.get(STOP_CONFIRM_HEADER) != STOP_CONFIRM_VALUE:
                    return self._error(400, "CONFIRMATION_REQUIRED",
                                       "stopping the control plane needs an explicit confirmation")
                self._send(202, {"stopping": True, "pid": system.pid})
                system.request_stop()          # the same graceful path as `aiops stop` (SIGTERM)
                return
            if method != "GET":
                return self._error(404, "NOT_FOUND", "unknown path")
            if path == "/api/v1/version":
                return self._send(200, system.version())
            if path == "/api/v1/config":
                return self._send(200, system.config())
            body = system.diagnostics()
            if body is None:
                return self._error(409, "BUSY", "a diagnostics run is already in progress")
            self._send(200, body)

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
