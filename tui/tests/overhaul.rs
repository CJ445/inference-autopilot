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
// Block titles and headings are small caps now (`DETECTED`), so these compare without case.
fn has(s: &str, needle: &str) {
    assert!(s.to_lowercase().contains(&needle.to_lowercase()), "missing {needle:?} in:\n{s}");
}
fn lacks(s: &str, needle: &str) {
    assert!(!s.to_lowercase().contains(&needle.to_lowercase()), "unexpected {needle:?} in:\n{s}");
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
fn every_stage_is_a_glyph_and_a_word_and_the_stream_fits_the_common_terminal_sizes() {
    for (w, h) in [(80, 24), (72, 20), (120, 40)] {
        let s = ui::render_to_string(&real(vec![incident(PENDING)]), w, h);
        for needle in ["✓ DETECTED", "✓ DIAGNOSED", "! RECOVERY READY", "○ RECOVERING", "○ VERIFYING"] {
            has(&s, needle);
        }
        // a glyph and a word per stage, never a bare glyph row
        assert!(!s.lines().any(|l| l.trim() == "✓ ✓ ✓ ✓ → ○ ○"), "{w}x{h}\n{s}");
    }
    // finished and failed stages are words too
    let done = ui::render_to_string(&testing("resolved", Some(incident(RESOLVED))), 100, 40);
    for needle in ["✓ VERIFIED", "✓ RECOVERED", "✓ APPROVED"] {
        has(&done, needle);
    }
    let failed = ui::render_to_string(&testing("unresolved", Some(incident(UNRESOLVED))), 100, 40);
    has(&failed, "✗ NOT VERIFIED");
}

// --- Home ---------------------------------------------------------------------------------------------

#[test]
fn the_footer_offers_the_actions_that_exist_and_only_those() {
    let foot = |a: &App| ui::render_to_string(a, 140, 40).lines().last().unwrap().to_string();
    let s = foot(&real(vec![]));
    for needle in ["R Run test", "I Incidents", "Ctrl+P Commands", "? Help"] {
        has(&s, needle);
    }
    lacks(&s, "F Inject");                                    // the server does not offer real faults here
    let offered = foot(&real_with_faults(serde_json::json!({"available": true, "active": null})));
    has(&offered, "F Inject fault");
    let busy = foot(&real_with_faults(serde_json::json!({"available": true, "active": {
        "fault_id": "f1", "status": "ACTIVE", "expires_at": 1_790_866_992.0, "workload": "vllm"}})));
    lacks(&busy, "F Inject fault");                           // one at a time
}

// --- the Lab ---------------------------------------------------------------------------------------------

#[test]
fn the_lab_lists_exactly_the_scenarios_the_backend_supports_in_two_labelled_groups() {
    let mut a = real_with_faults(serde_json::json!({"available": true, "active": null}));
    a.handle_key(key('3'));
    let s = screen(&a);
    for needle in ["RECOVERY LAB", "SAFE TESTS", "simulated · nothing real is touched",
                   "Model becomes unresponsive", "REAL INFRASTRUCTURE", "affects the running workload",
                   "Pause the running workload", "You confirm first.", "Enter runs the selected test."] {
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
    has(&s, "Pause the running workload");
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
    for needle in ["REAL FAULT IN PROGRESS", "LIVE", "FAULT INJECTED", "it resumes by itself in 1:00.",
                   "WAITING TO DETECT", "Real fault in progress"] {
        has(&s, needle);
    }
    lacks(&s, "Model becomes unresponsive");                   // the menu is replaced while a fault runs
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
    has(&s, "✓ TEST STARTED");
    has(&s, "INJECTING THE FAULT");
    for later in ["DETECTED", "DIAGNOSED", "RECOVERY READY", "VERIFIED"] {
        lacks(&s, later);
    }
    // fault injected, no incident yet
    let s = screen(&testing("detecting", None));
    has(&s, "✓ FAULT INJECTED");
    has(&s, "DETECTING");
    lacks(&s, "✓ DETECTED");
    // an incident awaiting the operator: evidence, why the action, what is needed
    let s = screen(&testing("awaiting_approval", Some(incident(PENDING))));
    for needle in ["✓ DETECTED", "15:02:11", "✓ DIAGNOSED", "Inference requests are failing or timing out",
                   "! RECOVERY READY", "Restart the model server", "• A test request to the model did not complete in time.",
                   "Restart is an allowlisted recovery action", "[ A ] Approve"] {
        has(&s, needle);
    }
    for not_yet in ["✓ APPROVED", "✓ RECOVERED", "✓ VERIFIED", "INCIDENT RESOLVED"] {
        lacks(&s, not_yet);
    }
}

#[test]
fn the_story_continues_through_recovery_and_ends_with_the_recorded_checks() {
    let mut recovering = incident(RESOLVED);
    recovering.status = "EXECUTING".into();
    recovering.timeline.truncate(7);
    let s = screen(&testing("recovering", Some(recovering)));
    has(&s, "✓ APPROVED");
    has(&s, "RECOVERING");
    has(&s, "The server is restarting the workload.");
    lacks(&s, "✓ VERIFIED");
    let mut verifying = incident(RESOLVED);
    verifying.status = "VERIFYING".into();
    verifying.timeline.truncate(8);
    verifying.verification = None;
    let s = screen(&testing("recovering", Some(verifying)));
    has(&s, "Waiting for the server's result.");
    lacks(&s, "INCIDENT RESOLVED");
    lacks(&s, "✓ VERIFIED");                                 // still verifying: not verified

    let s = screen(&testing("resolved", Some(incident(RESOLVED))));
    for needle in ["✓ RECOVERED", "The workload was restarted.", "✓ VERIFIED", "✓ The model server was restarted",
                   "✓ The model answers test requests again", "✓ INCIDENT RESOLVED", "Recovered and verified"] {
        has(&s, needle);
    }
    has(s.lines().last().unwrap(), "R Run again");
    let s = screen(&testing("unresolved", Some(incident(UNRESOLVED))));
    has(&s, "✗ NOT VERIFIED");
    has(&s, "✗ The model answers test requests again");
    has(&s, "✗ NOT RESOLVED");
    lacks(&s, "✓ INCIDENT RESOLVED");
}

#[test]
fn the_story_does_not_invent_a_diagnosis_or_a_verdict() {
    let s = screen(&testing("awaiting_approval", Some(incident(INSUFFICIENT))));
    has(&s, "NOT ENOUGH EVIDENCE");
    has(&s, "Autopilot cannot name a cause, so nothing is proposed.");
    lacks(&s, "RECOVERY READY");
    let mut declined = with_status(incident(PENDING), "REJECTED");
    declined.timeline.push(serde_json::from_value(serde_json::json!({"state": "REJECTED", "at": "2026-10-01T15:03:00+00:00"})).unwrap());
    let s = screen(&testing("rejected", Some(declined)));
    has(&s, "✗ DECLINED");
    has(&s, "Nothing was restarted.");
    let cleared = with_status(incident(PENDING), "CLEARED");
    let s = screen(&testing("cleared", Some(cleared)));
    has(&s, "NO LONGER NEEDED");
    lacks(&s, "✓ APPROVED");
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
    has(&s, "RECOVERY PROPOSED");
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
    let mut a = real(vec![]);
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
    for k in ["R Run test", "Ctrl+P Commands", "? Help"] {
        has(&home, k);
    }
    lacks(&home, "Approve");
    lacks(&home, "Review");                                  // nothing to review: the hint is not offered
    lacks(&home, "Quit");                                    // quitting is in the palette and Help
    let waiting = foot(&real(vec![incident(PENDING)]));
    has(&waiting, "Enter Open");
    let mut detail = real(vec![incident(PENDING)]);
    detail.handle_key(code(KeyCode::Enter));
    let d = foot(&detail);
    has(&d, "Esc Back");
    lacks(&d, "Run test");
    // the Lab menu has its own keys
    let mut lab = real(vec![]);
    lab.handle_key(key('3'));
    let l = foot(&lab);
    for k in ["Enter Run", "↑↓ Navigate", "Esc Back"] {
        has(&l, k);
    }
    lacks(&l, "Run test");
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

// --- the application shell ---------------------------------------------------------------------------------------

fn bar_text(a: &App, w: u16, h: u16) -> String {
    // the three rows above the footer
    let lines: Vec<String> = ui::render_to_string(a, w, h).lines().map(String::from).collect();
    lines[lines.len().saturating_sub(4)..lines.len() - 1].join("\n")
}

#[test]
fn the_anchored_bar_says_what_to_do_next_in_every_situation() {
    let loaded = |inc: Incident| {
        let mut snap = Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![inc.clone()]);
        snap.detail = Some(inc);
        let mut a = App::new("http://127.0.0.1:8080".into());
        a.wall = 1_790_866_932.0;
        a.apply(Msg::Poll(Ok(snap)));
        a
    };
    // idle
    has(&bar_text(&real(vec![]), 120, 40), "Autopilot is watching");
    has(&bar_text(&real(vec![]), 120, 40), "Run a recovery test to watch it detect, diagnose and recover.");
    // no workload: it does not pretend there is something to watch
    let mut st: serde_json::Value = serde_json::from_str(HEALTHY).unwrap();
    st["workload"] = serde_json::json!({"name": "vllm", "state": "absent"});
    st["last_observation"] = serde_json::Value::Null;
    let mut none = App::new("http://127.0.0.1:8080".into());
    none.apply(Msg::Poll(Ok(Snapshot::new(serde_json::from_value(st).unwrap(), vec![]))));
    has(&bar_text(&none, 120, 40), "Nothing to watch yet");
    // offline
    let mut off = real(vec![]);
    off.apply(Msg::Poll(Err(ApiError::Offline("refused".into()))));
    has(&bar_text(&off, 120, 40), "Cannot reach the control plane");
    // an incident waiting: loading, then the decision
    let waiting_unloaded = real(vec![incident(PENDING)]);
    has(&bar_text(&waiting_unloaded, 120, 40), "Loading the incident");
    let waiting = loaded(incident(PENDING));
    has(&bar_text(&waiting, 120, 40), "Autopilot needs your decision");
    has(&bar_text(&waiting, 120, 40), "Restart the model server? A approves, R rejects, E shows the evidence.");
    // working
    has(&bar_text(&loaded(with_status(incident(PENDING), "EXECUTING")), 120, 40), "Recovering");
    has(&bar_text(&loaded(with_status(incident(PENDING), "VERIFYING")), 120, 40), "Verifying recovery");
    // finished
    has(&bar_text(&loaded(incident(RESOLVED)), 120, 40), "Recovered and verified");
    has(&bar_text(&loaded(incident(UNRESOLVED)), 120, 40), "Recovery was not verified");
    // the spaces
    let mut lab = real(vec![]);
    lab.handle_key(key('3'));
    has(&bar_text(&lab, 120, 40), "Recovery Lab");
    lab.handle_key(key('2'));
    has(&bar_text(&lab, 120, 40), "Enter opens the selected incident");
    // a test that is starting
    has(&bar_text(&testing("healthy", None), 120, 40), "Starting the recovery test");
    has(&bar_text(&testing("detecting", None), 120, 40), "Detecting");
}

#[test]
fn the_bar_tone_is_a_colour_on_its_left_edge_and_never_the_only_signal() {
    let waiting = {
        let inc = incident(PENDING);
        let mut snap = Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![inc.clone()]);
        snap.detail = Some(inc);
        let mut a = App::new("http://127.0.0.1:8080".into());
        a.apply(Msg::Poll(Ok(snap)));
        a
    };
    let buf = ui::render_to_buffer(&waiting, 120, 40);
    let w = buf.area.width as usize;
    let bar_cell = &buf.content()[(buf.area.height as usize - 3) * w];
    assert_eq!((bar_cell.symbol(), bar_cell.fg), ("┃", aiops_tui::theme::WARNING));
    has(&bar_text(&waiting, 120, 40), "Autopilot needs your decision");   // and it is said in words
}

#[test]
fn the_workload_panel_is_context_beside_the_stream_not_a_space_of_its_own() {
    let wide = |a: &App| ui::render_to_string(a, 130, 40);
    // on the spaces that are about the workload, when the terminal is wide
    let mut a = real(vec![]);
    for k in ['1', '3'] {
        a.handle_key(key(k));
        let s = wide(&a);
        has(&s, "WORKLOAD");
        has(&s, "vram");
    }
    // not on the list spaces, where the width is better spent
    for k in ['2', '4', '5'] {
        a.handle_key(key(k));
        lacks(&wide(&a), "WORKLOAD\n");
        assert!(!wide(&a).lines().any(|l| l.trim_end().ends_with("WORKLOAD")), "panel on space {k}");
    }
    // closed below 110 columns, until asked for with W; W closes it again
    let mut n = real(vec![]);
    n.width = 100;                                             // what the event loop reports
    assert!(!ui::render_to_string(&n, 100, 40).contains("vram"));
    n.handle_key(key('w'));
    has(&ui::render_to_string(&n, 100, 40), "vram");
    n.handle_key(key('w'));
    assert!(!ui::render_to_string(&n, 100, 40).contains("vram"));
    // and on a wide terminal W closes it
    let mut c = real(vec![]);
    has(&wide(&c), "vram");
    c.handle_key(key('w'));
    assert!(!wide(&c).contains("vram"));
    // a test labels the panel: it shows simulated values
    has(&wide(&testing("healthy", None)), "WORKLOAD · SIMULATED");
}

#[test]
fn e_opens_the_evidence_under_every_stage_and_closes_it_again() {
    let inc = incident(RESOLVED);
    let mut a = testing("resolved", Some(inc));
    let before = screen(&a);
    a.handle_key(key('e'));
    let after = screen(&a);
    assert!(after.lines().count() == before.lines().count());
    assert!(after.contains("• A test request to the model did not complete in time.") || after.contains("A test request"), "{after}");
    assert!(!before.contains("• A test request to the model did not complete in time."), "{before}");
    a.handle_key(key('e'));
    assert!(!screen(&a).contains("• A test request to the model did not complete in time."));
}

#[test]
fn the_labs_list_moves_with_the_arrows_and_enter_runs_what_the_cursor_is_on() {
    let mut a = real_with_faults(serde_json::json!({"available": true, "active": null}));
    a.handle_key(key('3'));
    assert_eq!(a.lab_cursor, 0);
    // the first row runs the safe test
    let mut first = real_with_faults(serde_json::json!({"available": true, "active": null}));
    first.handle_key(key('3'));
    assert_eq!(first.handle_key(code(KeyCode::Enter)), vec![Effect::StartPractice]);
    // the second row only opens the real fault's confirmation: nothing is sent
    a.handle_key(code(KeyCode::Down));
    assert_eq!(a.lab_cursor, 1);
    has(&screen(&a), "Inject");
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty() && a.fault_confirm);
    a.handle_key(code(KeyCode::Esc));
    // it stays inside the list
    a.handle_key(code(KeyCode::Down));
    assert_eq!(a.lab_cursor, 1);
    a.handle_key(code(KeyCode::Up));
    a.handle_key(code(KeyCode::Up));
    assert_eq!(a.lab_cursor, 0);
    // where the real fault is not offered, Enter on its row says why and sends nothing
    let mut none = real(vec![]);
    none.handle_key(key('3'));
    none.handle_key(code(KeyCode::Down));
    assert!(none.handle_key(code(KeyCode::Enter)).is_empty() && !none.fault_confirm);
    assert!(none.active_notice().unwrap().is_error);
}

#[test]
fn the_palette_suggests_what_fits_the_moment_and_shows_the_shortcut_at_the_right() {
    let palette = |a: &mut App| {
        a.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
        screen(a)
    };
    let mut idle = real_with_faults(serde_json::json!({"available": true, "active": null}));
    let s = palette(&mut idle);
    for needle in ["Commands", "Search commands…", "Suggested", "Run a recovery test", "Inject a real fault",
                   "View incidents", "Go to", "Show system status", "System"] {
        has(&s, needle);
    }
    // the shortcut sits at the right of its row
    assert!(s.lines().any(|l| l.split("Run a recovery test").nth(1).is_some_and(|rest| rest.trim_start().starts_with("R ") && !rest.contains("watch"))), "{s}");
    // while a test runs, leaving it is suggested first
    let mut t = testing("healthy", None);
    has(&palette(&mut t), "Leave the recovery test");
    // typing filters and drops the groups
    let mut f = real(vec![]);
    palette(&mut f);
    for c in "activ".chars() {
        f.handle_key(key(c));
    }
    let s = screen(&f);
    has(&s, "View activity");
    lacks(&s, "Suggested");
    lacks(&s, "Stop control plane");
}

#[test]
fn the_active_stage_shows_a_spinner_that_moves_and_reduced_motion_makes_it_still() {
    let mut a = testing("recovering", Some({
        let mut i = incident(RESOLVED);
        i.status = "VERIFYING".into();
        i.timeline.truncate(8);
        i.verification = None;
        i
    }));
    let frame = |a: &App| ui::render_to_string(a, 120, 40).lines().find(|l| l.contains("VERIFYING")).unwrap().to_string();
    let first = frame(&a);
    assert!(first.contains('⠋'), "{first}");
    a.now += Duration::from_millis(170);
    let later = frame(&a);
    assert_ne!(first, later, "the spinner advances with time");
    a.reduce_motion = true;
    let still = frame(&a);
    assert!(still.contains('⋯') && !still.contains('⠋'), "{still}");
    a.now += Duration::from_secs(5);
    assert_eq!(frame(&a), still, "and it does not move");
}

#[test]
fn a_decision_can_be_started_from_the_lab_stream_and_from_home_only_when_it_is_waiting_and_loaded() {
    let mut lab = testing("awaiting_approval", Some(incident(PENDING)));
    lab.handle_key(key('3'));
    assert!(lab.handle_key(key('a')).is_empty() && lab.confirm.is_some(), "the Lab's stream decides");
    lab.handle_key(code(KeyCode::Esc));
    // a finished incident is not decidable anywhere
    let mut done = testing("resolved", Some(incident(RESOLVED)));
    assert!(done.handle_key(key('r')).len() == 1 && done.confirm.is_none(), "R runs the test again; it never rejects a finished incident");
}

#[test]
fn a_toast_sits_just_above_the_bar_and_never_covers_the_header_or_the_incident() {
    let mut a = real(vec![incident(PENDING)]);
    a.handle_key(key('2'));
    a.handle_key(key('a'));                                   // refused: a toast
    let lines: Vec<String> = ui::render_to_string(&a, 120, 40).lines().map(String::from).collect();
    let toast_row = lines.iter().position(|l| l.contains("open the incident")).expect("the toast");
    assert!(toast_row >= lines.len() - 8, "row {toast_row} of {}: it belongs at the bottom", lines.len());
    assert!(lines[0].contains("INFERENCE AUTOPILOT") && lines[1].contains("Incidents"), "the header is untouched");
    assert!(lines.iter().any(|l| l.contains("inc_001")), "and the incident is still readable");
}

#[test]
fn the_labs_menu_is_not_about_any_incident_so_r_there_never_rejects_one() {
    // a real incident is waiting, but the Lab's menu is on screen
    let inc = incident(PENDING);
    let mut snap = Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(key('3'));
    assert_eq!(a.handle_key(key('r')), vec![Effect::StartPractice], "R runs a test here");
    assert!(a.confirm.is_none());
    assert!(a.handle_key(key('a')).is_empty() && a.confirm.is_none(), "and A never approves from the menu");
    assert_eq!(a.screen, Screen::Audit);
}
