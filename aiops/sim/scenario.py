"""The golden simulation scenario, runnable without the TUI.

Every step is written from the state of the real objects (engine, audit chain, world) after the
production code has acted, never from a script of what "should" have happened. If the loop does
not behave as the scenario expects, the run raises instead of narrating success.
"""
GOLDEN = "model-unresponsive-recovery"
HEALTHY_TICKS = 2


class ScenarioError(Exception):
    """The production loop did not do what the scenario requires."""


class ScenarioResult:
    def __init__(self, session, steps, outcome):
        self.session, self.steps, self.outcome = session, steps, outcome

    @property
    def incident(self):
        return self.session.incident


def _last(audit, event):
    return next((e["data"] for e in reversed(audit.events) if e["event"] == event), None)


def _checks_text(checks):
    return ", ".join(f"{k}={'ok' if v is True else 'FAILED'}" for k, v in sorted(checks.items()))


def model_unresponsive_recovery(session, decide, emit=lambda step: None):
    """healthy -> fault -> 2 failed probes -> incident -> evidence -> RCA -> proposal -> policy ->
    decision -> (simulated restart -> verification -> RESOLVED) -> audit."""
    steps, engine, world = [], session.engine, session.world

    def step(title, detail=""):
        s = {"n": len(steps) + 1, "title": title, "detail": detail}
        steps.append(s)
        emit(s)

    def need(condition, what):
        if not condition:
            raise ScenarioError(what)

    for _ in range(HEALTHY_TICKS):
        need(session.tick() is None and not engine.incidents, "an incident opened while healthy")
    obs = engine.last_observation
    need(obs is not None, "the engine produced no observation of the workload")
    step("The model is answering",
         f"probe ok in {obs['inference_probe_latency_ms']:.0f} ms; workload {world.identity}")

    session.inject_fault()
    step("Fault injected (simulated)", "the model stops answering and its metrics go dark")

    need(session.tick() is None and not engine.incidents, "one failed probe must not open an incident")
    step("Detector (1st failed probe)", "no incident yet: two in a row are required")

    incident = session.tick()
    need(incident is not None, "the second consecutive failed probe did not open an incident")
    step("Detector (2nd consecutive failed probe)", f"incident {incident.incident_id} opened "
         f"({incident.category})")

    step("Evidence collected", "; ".join(
        f"{e['metric']} ({e['source']}, {e['relation']})" for e in incident.evidence))
    rca = _last(engine.audit, "rca_generated")["rca"]
    need(rca["root_cause"] is not None, "no root cause from the evidence")
    step("Root cause", rca["root_cause"]["statement"])
    proposal = engine.pending.get(incident.incident_id)
    need(proposal is not None, "no remediation proposal")
    step("Proposal", f"{proposal['action']}({proposal['parameters']['workload']})")
    step("Policy", _last(engine.audit, "policy_evaluated")["decision"])
    need(incident.status == "POLICY_CHECK" and world.restarts == 0,
         "nothing may be restarted before approval")

    choice = decide(incident)
    if choice is None:
        step("Waiting for the operator", "no decision was given; nothing was restarted")
        return ScenarioResult(session, steps, "AWAITING_APPROVAL")
    if choice == "reject":
        session.reject()
        need(world.restarts == 0, "a rejected proposal restarted something")
        step("Operator decision (rejected)", f"nothing was restarted ({world.identity})")
    else:
        before = world.identity
        step("Operator decision (approved)", "the simulated restart now runs")
        session.approve()
        step("Simulated restart", f"lifecycle {before} -> {world.identity}")
        checks = _last(engine.audit, "verification_finished")["checks"]
        step("Verification (observed state)", _checks_text(checks))
        step(f"Result ({incident.status})", "recovered and verified (simulated)"
             if incident.status == "RESOLVED" else "recovery was not verified")
    need(engine.audit.verify(), "the audit chain does not verify")
    step("Audit", f"{len(engine.audit.events)} events, hash chain intact, every event marked "
         f"{engine.mode}")
    return ScenarioResult(session, steps, incident.status)


SCENARIOS = {GOLDEN: model_unresponsive_recovery}
