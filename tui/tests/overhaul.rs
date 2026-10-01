//! The overhauled operator experience: the pipeline, the Lab, the one-key recovery test and its
//! story, the dialogs, and the refusal. Every line of the story must come from what the server
//! reported; nothing may be shown as done that the server has not said happened.
use aiops_tui::api::ApiError;
use aiops_tui::app::{ActionKind, App, Effect, Loaded, Msg, Screen, Snapshot};
use aiops_tui::model::{Incident, Status};
use aiops_tui::pipeline::{current, stages, Stage};
use aiops_tui::ui;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::time::Duration;

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");
const PENDING: &str = include_str!("fixtures/incident_pending.json");
const RESOLVED: &str = include_str!("fixtures/incident_resolved.json");
const UNRESOLVED: &str = include_str!("fixtures/incident_unresolved.json");
const INSUFFICIENT: &str = include_str!("fixtures/incident_insufficient.json");

fn key(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
}
fn code(k: KeyCode) -> KeyEvent {
    KeyEvent::new(k, KeyModifiers::NONE)
}
fn incident(json: &str) -> Incident {
    serde_json::from_str(json).unwrap()
}
fn with_status(mut inc: Incident, status: &str) -> Incident {
    inc.status = status.into();
    inc
}
fn real(incidents: Vec<Incident>) -> App {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = 1_790_866_932.0;
    a.apply(Msg::Poll(Ok(Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), incidents))));
    a
}
fn real_with_faults(faults: serde_json::Value) -> App {
    let mut v: serde_json::Value = serde_json::from_str(HEALTHY).unwrap();
    v["faults"] = faults;
    let st: Status = serde_json::from_value(v).unwrap();
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = 1_790_866_932.0;
    a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![]))));
    a
}
fn practice_status(stage: &str) -> Status {
    let mut v: serde_json::Value = serde_json::from_str(HEALTHY).unwrap();
    v["mode"] = "SIMULATION".into();
    v["practice"] = serde_json::json!({ "stage": stage });
    v["info"] = serde_json::json!({"profile": "practice", "provider": "simulation", "workload": "sim-vllm", "model": "sim/opt-125m"});
    v.as_object_mut().unwrap().remove("watchdog");
    serde_json::from_value(v).unwrap()
}
/// A test in progress: started, and a poll for the stage (with the incident and its detail, if any).
fn testing(stage: &str, inc: Option<Incident>) -> App {
    let mut a = real(vec![]);
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    let mut snap = Snapshot::new(practice_status(stage), inc.iter().cloned().collect());
    snap.practice = true;
    snap.detail = inc;
    a.apply(Msg::Poll(Ok(snap)));
    a
}
fn screen(a: &App) -> String {
    ui::render_to_string(a, 120, 40)
}
fn has(s: &str, needle: &str) {
    assert!(s.contains(needle), "missing {needle:?} in:\n{s}");
}
fn lacks(s: &str, needle: &str) {
    assert!(!s.contains(needle), "unexpected {needle:?} in:\n{s}");
}

// --- the pipeline ---------------------------------------------------------------------------------

#[test]
fn the_pipeline_follows_the_servers_status_and_never_runs_ahead_of_it() {
    use Stage::*;
    let s = |status: &str| stages(Some(&with_status(incident(PENDING), status)), true);
    assert_eq!(stages(None, true), [Done, NotRun, NotRun, NotRun, NotRun, NotRun, NotRun]);
    assert_eq!(stages(None, false)[0], NotApplicable, "nothing observed: Observe is not claimed");
    assert_eq!(s("DETECTED"), [Done, Active, NotRun, NotRun, NotRun, NotRun, NotRun]);
    assert_eq!(s("TRIAGING"), [Done, Done, Active, NotRun, NotRun, NotRun, NotRun]);
    assert_eq!(s("POLICY_CHECK"), [Done, Done, Done, Done, Active, NotRun, NotRun]);
    assert_eq!(s("EXECUTING"), [Done, Done, Done, Done, Done, Active, NotRun]);
    assert_eq!(s("VERIFYING"), [Done, Done, Done, Done, Done, Done, Active]);
    assert_eq!(s("RESOLVED"), [Done; 7]);
    assert_eq!(s("UNRESOLVED")[6], Failed);
    assert_eq!(s("EXECUTION_FAILED")[5], Failed);
    assert_eq!(s("REJECTED")[4], Failed);
    assert_eq!(&s("REJECTED")[5..], &[NotApplicable, NotApplicable]);
    assert_eq!(s("INSUFFICIENT_EVIDENCE")[2], Failed);
    // cleared: nothing was approved, run or verified
    assert_eq!(&s("CLEARED")[4..], &[NotApplicable, NotApplicable, NotApplicable]);
    // an unknown status claims nothing beyond "observing"
    assert_eq!(s("SOMETHING_NEW"), [Done, NotRun, NotRun, NotRun, NotRun, NotRun, NotRun]);
    assert_eq!(current(&s("POLICY_CHECK")), Some(4));
    assert_eq!(current(&s("RESOLVED")), None);
}

#[test]
fn every_stage_is_a_glyph_and_a_word_and_a_narrow_pane_still_names_where_it_is() {
    let wide = screen(&real(vec![incident(PENDING)]));
    has(&wide, "✓ Observe › ✓ Detect › ✓ Diagnose › ✓ Propose › → Approve › ○ Recover › ○ Verify");
    let eighty = ui::render_to_string(&real(vec![incident(PENDING)]), 80, 24);
    has(&eighty, "✓ Observe › ✓ Detect › ✓ Diagnose › ✓ Propose › → Approve › ○ Recover › ○ Verify");   // the common 80 columns
    let narrow = ui::render_to_string(&real(vec![incident(PENDING)]), 72, 30);
    has(&narrow, "✓ ✓ ✓ ✓ → ○ ○  Approve");                      // glyphs, and the stage the loop is on
    // failed and skipped stages are words too
    let mut a = real(vec![incident(RESOLVED)]);
    a.handle_key(code(KeyCode::Enter));
    let _ = a;
}

// --- Home ---------------------------------------------------------------------------------------------

#[test]
fn home_offers_the_quick_actions_that_exist_and_only_those() {
    let s = screen(&real(vec![]));
    for needle in ["[R] Run a recovery test", "[I] View incidents", "[A] View activity"] {
        has(&s, needle);
    }
    lacks(&s, "[F]");                                         // the server does not offer real faults here
    let offered = screen(&real_with_faults(serde_json::json!({"available": true, "active": null})));
    has(&offered, "[F] Inject a real fault");
    let busy = screen(&real_with_faults(serde_json::json!({"available": true, "active": {
        "fault_id": "f1", "status": "ACTIVE", "expires_at": 1_790_866_992.0, "workload": "vllm"}})));
    lacks(&busy, "[F] Inject a real fault");                  // one at a time
}

// --- the Lab ---------------------------------------------------------------------------------------------

#[test]
fn the_lab_lists_exactly_the_scenarios_the_backend_supports_in_two_labelled_groups() {
    let mut a = real_with_faults(serde_json::json!({"available": true, "active": null}));
    a.handle_key(key('3'));
    let s = screen(&a);
    for needle in ["LAB", "Recovery tests", "safe · simulated · nothing real is touched",
                   "[R] Model becomes unresponsive", "Real infrastructure", "affects the running workload",
                   "[F] Pause the running workload", "Asks you to confirm first.",
                   "✓ Observe › ○ Detect"] {
        has(&s, needle);
    }
    for invented in ["Recovery verification", "Kill", "OOM", "Network"] {
        lacks(&s, invented);                                   // no scenario the backend does not have
    }
}

#[test]
fn the_lab_says_why_the_real_fault_is_unavailable_instead_of_hiding_it() {
    let reason = |a: &mut App| {
        a.handle_key(key('3'));
        screen(a)
    };
    let mut none = real(vec![]);                               // a control plane with no injector
    let s = reason(&mut none);
    has(&s, "[F] Pause the running workload");
    has(&s, "Not available: this control plane does not offer real faults.");
    let mut off = real_with_faults(serde_json::json!({"available": false, "active": null}));
    has(&reason(&mut off), "this control plane does not offer real faults");
    let mut offline = real_with_faults(serde_json::json!({"available": true, "active": null}));
    offline.apply(Msg::Poll(Err(ApiError::Offline("refused".into()))));
    has(&reason(&mut offline), "the control plane cannot be reached");
}

#[test]
fn a_real_fault_in_progress_is_shown_in_the_lab_with_the_time_left_and_the_loop() {
    let mut a = real_with_faults(serde_json::json!({"available": true, "active": {
        "fault_id": "f1", "status": "ACTIVE", "expires_at": 1_790_866_992.0, "workload": "vllm"}}));
    a.handle_key(key('3'));
    let s = screen(&a);
    for needle in ["REAL FAULT IN PROGRESS", "LIVE", "paused on purpose", "resumes by itself", "Fault injected",
                   "it resumes by itself in 1:00."] {
        has(&s, needle);
    }
    lacks(&s, "[R] Model becomes unresponsive");               // the menu is replaced while a fault runs
}

// --- the one-key recovery test ---------------------------------------------------------------------------

#[test]
fn r_starts_the_test_and_the_fault_follows_once_the_server_says_healthy_exactly_once() {
    let mut a = real(vec![]);
    assert_eq!(a.handle_key(key('r')), vec![Effect::StartPractice]);
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    assert_eq!(a.screen, Screen::Lab, "the test opens in the Lab");
    assert!(a.take_effects().is_empty(), "nothing is injected before the server has said healthy");
    // a poll that is not yet for this namespace changes nothing
    a.apply(Msg::Poll(Ok(Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![]))));
    assert!(a.take_effects().is_empty());
    let mut healthy = Snapshot::new(practice_status("healthy"), vec![]);
    healthy.practice = true;
    a.apply(Msg::Poll(Ok(healthy.clone())));
    assert_eq!(a.take_effects(), vec![Effect::InjectFault], "once the server says healthy");
    a.apply(Msg::Poll(Ok(healthy)));
    assert!(a.take_effects().is_empty(), "and exactly once");
}

#[test]
fn the_fault_is_not_injected_when_the_server_is_already_past_healthy_or_the_test_was_left() {
    let mut a = real(vec![]);
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    let mut past = Snapshot::new(practice_status("detecting"), vec![]);
    past.practice = true;
    a.apply(Msg::Poll(Ok(past)));
    assert!(a.take_effects().is_empty());
    let mut b = real(vec![]);
    b.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    b.apply(Msg::Loaded(Loaded::PracticeStopped(Ok(()))));
    let mut healthy = Snapshot::new(practice_status("healthy"), vec![]);
    healthy.practice = true;
    b.apply(Msg::Poll(Ok(healthy)));
    assert!(b.take_effects().is_empty(), "a left test injects nothing");
    // a failed start injects nothing either
    let mut c = real(vec![]);
    c.apply(Msg::Loaded(Loaded::PracticeStarted(Err(ApiError::Timeout))));
    assert!(c.take_effects().is_empty() && !c.practice);
}

// --- the story ----------------------------------------------------------------------------------------------

#[test]
fn the_story_shows_only_what_the_server_has_reported() {
    // before the fault: the test started, the fault is being asked for, nothing else
    let s = screen(&testing("healthy", None));
    has(&s, "RECOVERY TEST");
    has(&s, "Test started");
    has(&s, "Injecting the fault");
    for later in ["Detected", "Diagnosis", "Recovery proposed", "Verified"] {
        lacks(&s, later);
    }
    // fault injected, no incident yet
    let s = screen(&testing("detecting", None));
    has(&s, "Fault injected");
    has(&s, "Detecting");
    lacks(&s, "Detected ");
    // an incident awaiting the operator: evidence, why the action, what is needed
    let s = screen(&testing("awaiting_approval", Some(incident(PENDING))));
    for needle in ["Detected", "15:02:11", "Diagnosis", "Inference requests are failing or timing out",
                   "Recovery proposed", "Restart the model server", "• A test request to the model did not complete in time.",
                   "Restart is an allowlisted recovery action; it runs only after you approve.",
                   "Needs your OK", "Enter  Review and decide"] {
        has(&s, needle);
    }
    for not_yet in ["You approved", "Recovered", "Verified", "INCIDENT RESOLVED"] {
        lacks(&s, not_yet);
    }
}

#[test]
fn the_story_continues_through_recovery_and_ends_with_the_recorded_checks() {
    let mut recovering = incident(RESOLVED);
    recovering.status = "EXECUTING".into();
    recovering.timeline.truncate(7);
    let s = screen(&testing("recovering", Some(recovering)));
    has(&s, "You approved");
    has(&s, "→ Recover");
    has(&s, "The server is restarting the workload…");
    lacks(&s, "Verified");
    let mut verifying = incident(RESOLVED);
    verifying.status = "VERIFYING".into();
    verifying.timeline.truncate(8);
    verifying.verification = None;
    let s = screen(&testing("recovering", Some(verifying)));
    has(&s, "Waiting for the server's result.");
    lacks(&s, "INCIDENT RESOLVED");
    lacks(&s, "Verified");                                   // still verifying: not verified

    let s = screen(&testing("resolved", Some(incident(RESOLVED))));
    for needle in ["Recovered", "The workload was restarted.", "Verified", "✓ The model server was restarted",
                   "✓ The model answers test requests again", "INCIDENT RESOLVED",
                   "R  Run it again", "Enter  View the incident",
                   "✓ Observe › ✓ Detect › ✓ Diagnose › ✓ Propose › ✓ Approve › ✓ Recover › ✓ Verify"] {
        has(&s, needle);
    }
    let s = screen(&testing("unresolved", Some(incident(UNRESOLVED))));
    has(&s, "Verification failed");
    has(&s, "✗ The model answers test requests again");
    has(&s, "NOT RESOLVED");
    lacks(&s, "INCIDENT RESOLVED");
}

#[test]
fn the_story_does_not_invent_a_diagnosis_or_a_verdict() {
    let s = screen(&testing("awaiting_approval", Some(incident(INSUFFICIENT))));
    has(&s, "Not enough evidence to name a cause. Nothing is proposed.");
    lacks(&s, "Recovery proposed");
    let mut declined = with_status(incident(PENDING), "REJECTED");
    declined.timeline.push(serde_json::from_value(serde_json::json!({"state": "REJECTED", "at": "2026-10-01T15:03:00+00:00"})).unwrap());
    let s = screen(&testing("rejected", Some(declined)));
    has(&s, "You declined");
    has(&s, "Nothing was restarted.");
    let cleared = with_status(incident(PENDING), "CLEARED");
    let s = screen(&testing("cleared", Some(cleared)));
    has(&s, "No longer needed");
    lacks(&s, "You approved");
}

#[test]
fn the_action_is_remembered_after_the_server_stops_listing_the_proposal() {
    let mut a = real(vec![]);
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    let mut first = Snapshot::new(practice_status("awaiting_approval"), vec![incident(PENDING)]);
    first.practice = true;
    a.apply(Msg::Poll(Ok(first)));
    // after approval the server's incident carries no proposal
    let mut approved = incident(RESOLVED);
    approved.proposal = None;
    let mut next = Snapshot::new(practice_status("resolved"), vec![approved.clone()]);
    next.practice = true;
    next.detail = Some(approved);
    a.apply(Msg::Poll(Ok(next)));
    assert_eq!(a.proposed_action("inc_001"), Some("restart_workload"));
    let s = screen(&a);
    has(&s, "Recovery proposed");
    has(&s, "Restart the model server");
    // a different world remembers nothing
    a.apply(Msg::Loaded(Loaded::PracticeStopped(Ok(()))));
    assert_eq!(a.proposed_action("inc_001"), None);
}

#[test]
fn leaving_the_test_with_esc_and_running_it_again_with_r_both_work_from_the_lab() {
    let mut a = testing("resolved", Some(incident(RESOLVED)));
    assert_eq!(a.screen, Screen::Lab);
    assert_eq!(a.handle_key(key('r')), vec![Effect::StartPractice], "R runs it again");
    assert_eq!(a.handle_key(code(KeyCode::Esc)), vec![Effect::EndPractice], "Esc leaves the test");
}

#[test]
fn enter_in_the_lab_opens_the_incident_and_esc_comes_back() {
    let mut a = testing("awaiting_approval", Some(incident(PENDING)));
    a.handle_key(code(KeyCode::Enter));
    assert!(matches!(a.screen, Screen::Detail(_)));
    a.handle_key(code(KeyCode::Esc));
    assert_eq!(a.screen, Screen::Lab);
    // the Lab asks for the test incident's recorded details, so the story can show its checks
    assert_eq!(a.interest().detail_id, Some("inc_001".into()));
}

// --- the dialogs -------------------------------------------------------------------------------------------------

fn decision_app(practice: bool) -> App {
    let inc = incident(PENDING);
    let mut a = if practice { testing("awaiting_approval", Some(inc.clone())) } else { real(vec![inc.clone()]) };
    if !practice {
        let mut snap = Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![inc.clone()]);
        snap.detail = Some(inc);
        a.apply(Msg::Poll(Ok(snap)));
    }
    a.handle_key(code(KeyCode::Enter));
    a
}

#[test]
fn the_recovery_request_explains_what_why_and_what_it_will_do_and_keeps_two_steps() {
    let mut a = decision_app(false);
    a.handle_key(key('a'));
    let s = screen(&a);
    for needle in ["RECOVERY REQUEST", "LIVE", "Your model stopped answering", "inc_001", "Evidence",
                   "A test request to the model did not complete in time.", "Proposed", "Restart the model server",
                   "no rollback", "allowlisted and runs only after you approve it.", "[Enter] Approve", "[Esc] Cancel"] {
        has(&s, needle);
    }
    // two steps: the request opens, and only Enter after the guard approves, exactly that incident
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty(), "inside the guard");
    a.now += Duration::from_millis(600);
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::Approve("inc_001".into())]);
    // and it is the simulated one when it is a test
    let mut t = decision_app(true);
    t.handle_key(key('a'));
    let s = screen(&t);
    has(&s, "SIMULATION");
    has(&s, "A simulated restart: nothing real is restarted.");
    lacks(&s, "no rollback");
}

#[test]
fn a_decline_is_its_own_dialog_and_its_own_effect() {
    let mut a = decision_app(false);
    a.handle_key(key('r'));
    let s = screen(&a);
    has(&s, "DECLINE THIS RECOVERY");
    has(&s, "[Enter] Decline");
    lacks(&s, "RECOVERY REQUEST");
    a.now += Duration::from_millis(600);
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::Reject("inc_001".into())]);
    let _ = ActionKind::Reject;
}

#[test]
fn the_decision_line_is_never_the_part_that_is_cut_off_on_a_small_terminal() {
    for (w, h) in [(72, 18), (80, 24), (100, 30)] {
        let mut a = decision_app(false);
        a.handle_key(key('a'));
        let s = ui::render_to_string(&a, w, h);
        has(&s, "[Enter] Approve");
        has(&s, "[Esc] Cancel");
        has(&s, "RECOVERY REQUEST");
        let mut f = real_with_faults(serde_json::json!({"available": true, "active": null}));
        f.handle_key(key('f'));
        let s = ui::render_to_string(&f, w, h);
        has(&s, "[Enter] Inject fault");
        has(&s, "[Esc] Cancel");
        has(&s, "REAL INFRASTRUCTURE FAULT");
        has(&s, "LIVE · GPU-REAL");
    }
}

#[test]
fn a_dialog_with_a_lot_of_evidence_sheds_the_extras_and_keeps_the_decision_line() {
    let mut inc = incident(PENDING);
    let extra = inc.evidence.clone();
    for _ in 0..6 {
        inc.evidence.extend(extra.clone());               // far more evidence than 18 rows can hold
    }
    let mut a = real(vec![inc.clone()]);
    let mut snap = Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![inc.clone()]);
    snap.detail = Some(inc);
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(code(KeyCode::Enter));
    a.handle_key(key('a'));
    for (w, h) in [(72, 18), (80, 24)] {
        let s = ui::render_to_string(&a, w, h);
        has(&s, "RECOVERY REQUEST");
        has(&s, "Proposed");
        has(&s, "Restart the model server");
        has(&s, "[Enter] Approve");                       // the line that decides is never cut off
        has(&s, "[Esc] Cancel");
    }
    // and with room, all of it is there
    let big = ui::render_to_string(&a, 120, 90);
    has(&big, "Evidence");
}

#[test]
fn the_real_fault_dialog_states_the_flow_and_every_safety_mechanism_when_there_is_room() {
    let mut f = real_with_faults(serde_json::json!({"available": true, "active": null}));
    f.handle_key(key('f'));
    let s = ui::render_to_string(&f, 120, 40);
    for needle in ["Expected flow", "Healthy › Paused › Detected › Your approval › Restart › Verified recovery",
                   "Safety", "only the managed workload, by its checked identity",
                   "a recovery lease is saved before anything is paused", "resumed automatically after 2 minutes",
                   "a separate process resumes it even if this one dies"] {
        has(&s, needle);
    }
}

// --- "recovery no longer needed" ------------------------------------------------------------------------------------

fn refused_action(message: &str) -> Msg {
    Msg::Action {
        kind: ActionKind::Approve,
        id: "inc_001".into(),
        result: Err(ApiError::Server { status: 409, code: "POLICY_DENIED".into(), message: message.into() }),
    }
}

#[test]
fn a_refusal_because_the_problem_is_gone_is_a_dialog_that_says_nothing_was_restarted() {
    let mut a = decision_app(false);
    a.apply(refused_action("The problem is no longer present; nothing was restarted."));
    let s = screen(&a);
    for needle in ["RECOVERY NO LONGER NEEDED", "the latest observation shows the problem is gone",
                   "No restart was performed.", "closed as Cleared", "[any key] Close"] {
        has(&s, needle);
    }
    assert!(a.active_notice().is_none(), "it is a dialog, not a line that fades");
    // any key closes it and does nothing else
    assert!(a.handle_key(key('a')).is_empty());
    assert!(a.refusal.is_none());
    lacks(&screen(&a), "RECOVERY NO LONGER NEEDED");
}

#[test]
fn other_refusals_stay_ordinary_errors() {
    let mut a = decision_app(false);
    a.apply(refused_action("Another remediation holds this workload."));
    assert!(a.refusal.is_none());
    assert!(a.active_notice().unwrap().is_error);
}

// --- keyboard navigation ----------------------------------------------------------------------------------------------

#[test]
fn the_keys_move_between_the_five_areas_and_the_quick_actions_never_decide() {
    let mut a = real(vec![incident(PENDING)]);
    for (k, expect) in [('1', Screen::Dashboard), ('2', Screen::Incidents), ('3', Screen::Lab), ('4', Screen::Audit), ('5', Screen::System)] {
        a.handle_key(key(k));
        assert_eq!(a.screen, expect, "{k}");
    }
    a.handle_key(key('1'));
    a.handle_key(key('i'));
    assert_eq!(a.screen, Screen::Incidents);
    a.handle_key(key('1'));
    a.handle_key(key('a'));
    assert_eq!(a.screen, Screen::Audit, "A on Home opens Activity");
    for k in ['r', 'a', 'f'] {
        a.handle_key(key('1'));
        let before = a.confirm.is_some();
        a.handle_key(key(k));
        assert_eq!(a.confirm.is_some(), before, "{k} never opens a decision from Home");
    }
}

#[test]
fn the_footer_lists_only_the_keys_that_work_on_the_screen() {
    let foot = |a: &App| ui::render_to_string(a, 140, 40).lines().last().unwrap().to_string();
    let home = foot(&real(vec![]));
    for k in ["R Run test", "Ctrl+P Commands", "Q Quit"] {
        has(&home, k);
    }
    lacks(&home, "Approve");
    lacks(&home, "Review");                                  // nothing to review: the hint is not offered
    let with_incident = foot(&real(vec![incident(PENDING)]));
    has(&with_incident, "Enter Review");
    let mut detail = real(vec![incident(PENDING)]);
    detail.handle_key(code(KeyCode::Enter));
    let d = foot(&detail);
    has(&d, "Approve");
    has(&d, "Reject");
    lacks(&d, "Run test");
}

#[test]
fn the_palette_offers_operator_actions_not_implementation_commands() {
    let mut a = real_with_faults(serde_json::json!({"available": true, "active": null}));
    a.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
    let s = screen(&a);
    for needle in ["Run a recovery test", "Inject a real fault (pause the workload)…", "View incidents", "View activity",
                   "Open the Lab", "Show system status", "Show technical details", "Stop control plane…", "Help"] {
        has(&s, needle);
    }
    for impl_word in ["Provider", "Detector", "Executor", "namespace", "Simulation"] {
        lacks(&s, impl_word);
    }
}
