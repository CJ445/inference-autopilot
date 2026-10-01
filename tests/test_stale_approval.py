"""An approval is only as good as the picture it was made from. If the problem is gone by the time
the operator approves, nothing is restarted: a restart interrupts inference and has no rollback."""
import pytest

from aiops.engine import ConditionGone, Engine
from aiops.store import Store, StoreUnavailable
from test_engine import CONFIG, World

HANG = {"inference_probe_ok": False, "inference_probe_error": "timeout"}


class Hung(World):
    """The vLLM-shaped observation: an inference probe that fails, then answers again."""

    def __init__(self):
        super().__init__()
        self.hung = True

    def metrics(self):
        obs = super().metrics()
        obs.update({"inference_probe_ok": not self.hung, "inference_probe_error": "timeout" if self.hung else None,
                    "inference_probe_latency_ms": None if self.hung else 18.0,
                    "vllm_metrics_available": not self.hung})
        return obs


def hung_engine(**kw):
    w = Hung()
    e = Engine(w, w, {**CONFIG, "timeout": 1}, **kw)
    e.tick()                                         # one failed probe: not yet (debounce)
    inc = e.tick()                                   # the second one: incident, proposal pending
    assert inc.category == "INFERENCE_UNRESPONSIVE" and e.pending
    return w, e, inc


def events(e):
    return [x["event"] for x in e.audit.events]


def test_approving_after_the_model_answers_again_restarts_nothing_and_clears_the_incident():
    w, e, inc = hung_engine()
    w.hung = False                                   # the fault ended while the incident waited
    e.tick()
    with pytest.raises(ConditionGone):
        e.approve(inc.incident_id)
    assert w.restarts == 0                           # NOTHING was restarted
    assert inc.status == "CLEARED" and e.pending == {}
    assert "approval_granted" not in events(e)       # an approval that was refused is not recorded as granted
    refused = next(x for x in e.audit.events if x["event"] == "approval_refused")
    assert refused["data"] == {"incident_id": inc.incident_id, "reason": "the model is answering again"}
    assert e.audit.verify()


def test_approving_while_the_problem_persists_still_runs_the_remediation():
    w, e, inc = hung_engine()
    e.tick()
    w.hung = False                                   # a restart (below) is what makes it answer
    w2 = w
    w2.hung = True
    orig = w2.restart_workload
    w2.restart_workload = lambda n: (setattr(w2, "hung", False), orig(n))[1]
    e.approve(inc.incident_id)
    assert w.restarts == 1 and inc.status == "RESOLVED"
    assert "approval_granted" in events(e) and "approval_refused" not in events(e)


def test_memory_pressure_that_has_dropped_below_the_limit_is_not_restarted():
    w = World()
    w.fault, w.failures = True, 5
    e = Engine(w, w, CONFIG)
    inc = e.tick()
    assert inc.category == "GPU_MEMORY_PRESSURE" and e.pending
    w.fault = False
    e.tick()
    with pytest.raises(ConditionGone):
        e.approve(inc.incident_id)
    assert w.restarts == 0 and inc.status == "CLEARED"
    assert next(x for x in e.audit.events if x["event"] == "approval_refused")["data"]["reason"] == \
        "GPU memory is back under its limit"


def test_a_workload_that_has_gone_away_is_not_restarted_or_started():
    w, e, inc = hung_engine(presence=lambda: "running")
    e.presence = lambda: "stopped"                   # the operator stopped it while the incident waited
    e.tick()
    with pytest.raises(ConditionGone):
        e.approve(inc.incident_id)
    assert w.restarts == 0 and inc.status == "CLEARED"
    assert next(x for x in e.audit.events if x["event"] == "approval_refused")["data"]["reason"] == \
        "the workload is stopped"


def test_a_pending_incident_with_no_current_observation_is_not_approvable():
    w, e, inc = hung_engine()
    e.last_observation = None                        # fail closed: no picture, no restart
    with pytest.raises(ConditionGone):
        e.approve(inc.incident_id)
    assert w.restarts == 0


def test_a_refusal_that_cannot_be_saved_changes_nothing(tmp_path):
    w = Hung()
    store = Store(tmp_path / "a.db")
    e = Engine(w, w, CONFIG, store=store)
    e.tick()
    inc = e.tick()
    w.hung = False
    e.tick()
    store.save = lambda *a, **k: (_ for _ in ()).throw(StoreUnavailable("disk full"))
    with pytest.raises(StoreUnavailable):
        e.approve(inc.incident_id)
    assert w.restarts == 0
    assert inc.status == "POLICY_CHECK" and inc.incident_id in e.pending     # rolled back: still pending
    assert "approval_refused" not in events(e)


def test_the_api_tells_the_operator_the_problem_is_gone(tmp_path):
    import json, threading, urllib.request, urllib.error
    from aiops.api import make_server
    w, e, inc = hung_engine()
    w.hung = False
    e.tick()
    srv = make_server(e, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/v1/incidents/{inc.incident_id}/remediation/approve",
            method="POST", data=b"")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        body = json.loads(err.value.read())
        assert err.value.code == 409
        assert body["error"]["code"] == "POLICY_DENIED"
        assert "no longer present" in body["error"]["message"] and "nothing was restarted" in body["error"]["message"]
    finally:
        srv.shutdown()
    assert w.restarts == 0


def test_the_operator_ui_recognises_this_exact_refusal():
    """The TUI shows 'RECOVERY NO LONGER NEEDED' when the server's refusal contains
    `REFUSED_STALE` (tui/src/app.rs). The API has no dedicated error code for it, so the text is the
    contract: if either side changes it, this fails instead of the dialog silently never appearing."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "tui" / "src" / "app.rs").read_text()
    pinned = re.search(r'pub const REFUSED_STALE: &str = "([^"]+)";', src).group(1)
    api = (Path(__file__).resolve().parent.parent / "aiops" / "api.py").read_text()
    message = re.search(r'except ConditionGone:\s+return self\._error\(409, "POLICY_DENIED",\s+"([^"]+)"', api).group(1)
    assert pinned in message, (pinned, message)
    # and the other 409 refusals must NOT match, or they would show the wrong dialog
    for other in ("No pending remediation for this incident.", "Another remediation holds this workload."):
        assert pinned not in other
