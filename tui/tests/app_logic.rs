//! The operator TUI's behaviour: navigation, confirmation, offline handling, honesty about state.
use aiops_tui::api::ApiError;
use aiops_tui::app::{ActionKind, App, Conn, Effect, Msg, Screen, Snapshot};
use aiops_tui::model::{Incident, Status};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::time::{Duration, Instant};

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");
const UNRESPONSIVE: &str = include_str!("fixtures/status_unresponsive.json");
const PENDING: &str = include_str!("fixtures/incident_pending.json");
const RESOLVED: &str = include_str!("fixtures/incident_resolved.json");
const INSUFFICIENT: &str = include_str!("fixtures/incident_insufficient.json");

fn key(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
}
fn code(k: KeyCode) -> KeyEvent {
    KeyEvent::new(k, KeyModifiers::NONE)
}
fn incident(json: &str, id: &str) -> Incident {
    let mut i: Incident = serde_json::from_str(json).unwrap();
    i.incident_id = id.to_string();
    i
}
fn snapshot(status: &str, incidents: Vec<Incident>) -> Snapshot {
    Snapshot::new(serde_json::from_str::<Status>(status).unwrap(), incidents)
}
fn online(status: &str, incidents: Vec<Incident>) -> App {
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.apply(Msg::Poll(Ok(snapshot(status, incidents))));
    app
}
fn with_pending() -> App {
    online(UNRESPONSIVE, vec![incident(PENDING, "inc_001")])
}
/// On the incident's own page: the only place a decision can be started.
fn reviewing() -> App {
    let inc = incident(PENDING, "inc_001");
    let mut snap = snapshot(UNRESPONSIVE, vec![inc.clone()]);
    snap.detail = Some(inc);                         // the page has loaded the incident's detail
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.apply(Msg::Poll(Ok(snap)));
    app.handle_key(code(KeyCode::Enter));
    app
}
/// Lets the confirmation's key-repeat guard elapse (the loop advances `now` in real use).
fn settle(app: &mut App) {
    app.now += Duration::from_millis(600);
}

#[test]
fn a_new_app_has_no_data_and_is_connecting() {
    let app = App::new("http://127.0.0.1:8080".into());
    assert!(matches!(app.conn, Conn::Connecting) && app.snapshot.is_none());
    assert!(matches!(app.screen, Screen::Dashboard));
}

#[test]
fn a_successful_poll_goes_online_and_orders_active_incidents_first_newest_first() {
    let mut recent = incident(RESOLVED, "inc_003");
    recent.status = "RESOLVED".into();
    let app = online(
        HEALTHY,
        vec![
            incident(RESOLVED, "inc_001"),
            incident(PENDING, "inc_002"),
            recent,
            incident(PENDING, "inc_004"),
        ],
    );
    assert!(matches!(app.conn, Conn::Online));
    let ids: Vec<_> = app.rows().iter().map(|i| i.incident_id.clone()).collect();
    assert_eq!(ids, vec!["inc_004", "inc_002", "inc_003", "inc_001"]);
}

#[test]
fn a_poll_error_goes_offline_keeps_the_last_data_and_marks_it_stale() {
    let mut app = online(HEALTHY, vec![]);
    assert!(!app.is_stale());
    app.apply(Msg::Poll(Err(ApiError::Offline("connection refused".into()))));
    assert!(matches!(app.conn, Conn::Offline { .. }));
    assert!(app.snapshot.is_some() && app.is_stale());
}

#[test]
fn the_app_recovers_when_the_control_plane_returns() {
    let mut app = online(HEALTHY, vec![]);
    app.apply(Msg::Poll(Err(ApiError::Timeout)));
    app.apply(Msg::Poll(Err(ApiError::Timeout)));
    match &app.conn {
        Conn::Offline { attempts, .. } => assert_eq!(*attempts, 2),
        other => panic!("{other:?}"),
    }
    app.apply(Msg::Poll(Ok(snapshot(HEALTHY, vec![]))));
    assert!(matches!(app.conn, Conn::Online) && !app.is_stale());
}

#[test]
fn data_older_than_the_stale_threshold_is_flagged_even_while_online() {
    let mut app = online(HEALTHY, vec![]);
    app.now = app.now + Duration::from_secs(10);
    assert!(app.is_stale());
}

#[test]
fn enter_inspects_the_selected_incident_and_esc_goes_back() {
    let mut app = with_pending();
    assert!(app.handle_key(code(KeyCode::Enter)).is_empty());
    assert!(matches!(&app.screen, Screen::Detail(id) if id == "inc_001"));
    assert_eq!(app.interest().detail_id.as_deref(), Some("inc_001"));
    app.handle_key(code(KeyCode::Esc));
    assert!(matches!(app.screen, Screen::Dashboard));
    assert_eq!(app.interest().detail_id, None);
}

#[test]
fn enter_with_no_incidents_does_nothing() {
    let mut app = online(HEALTHY, vec![]);
    app.handle_key(code(KeyCode::Enter));
    assert!(matches!(app.screen, Screen::Dashboard));
}

#[test]
fn arrows_move_the_selection_and_never_leave_the_list() {
    let mut app = online(
        HEALTHY,
        vec![incident(PENDING, "inc_001"), incident(PENDING, "inc_002"), incident(PENDING, "inc_003")],
    );
    for _ in 0..10 {
        app.handle_key(code(KeyCode::Down));
    }
    assert_eq!(app.selected, 2);
    for _ in 0..10 {
        app.handle_key(code(KeyCode::Up));
    }
    assert_eq!(app.selected, 0);
}

#[test]
fn the_selection_is_clamped_when_incidents_disappear() {
    let mut app = online(HEALTHY, vec![incident(PENDING, "a"), incident(PENDING, "b")]);
    app.handle_key(code(KeyCode::Down));
    app.apply(Msg::Poll(Ok(snapshot(HEALTHY, vec![incident(PENDING, "a")]))));
    assert_eq!(app.selected, 0);
}

#[test]
fn tab_and_number_keys_switch_screens_and_audit_is_only_polled_on_its_screen() {
    let mut app = online(HEALTHY, vec![]);
    assert!(!app.interest().want_audit);
    app.handle_key(code(KeyCode::Tab));
    assert!(matches!(app.screen, Screen::Incidents));
    app.handle_key(code(KeyCode::Tab));
    assert!(matches!(app.screen, Screen::Audit) && app.interest().want_audit);
    for _ in 0..6 {                       // Control Plane, Settings, Diagnostics, About, Help, Overview
        app.handle_key(code(KeyCode::Tab));
    }
    assert!(matches!(app.screen, Screen::Dashboard) && !app.interest().want_audit);
    app.handle_key(key('2'));
    assert!(matches!(app.screen, Screen::Incidents));
    app.handle_key(key('3'));
    assert!(matches!(app.screen, Screen::Audit));
    app.handle_key(key('1'));
    assert!(matches!(app.screen, Screen::Dashboard));
}

// --- approval: confirmation, exactly one action, never accidental --------------------------

#[test]
fn pressing_a_opens_a_confirmation_and_performs_no_action() {
    let mut app = reviewing();
    let effects = app.handle_key(key('a'));
    assert!(effects.is_empty(), "approval must wait for confirmation");
    let c = app.confirm.as_ref().expect("a confirmation dialog");
    assert!(matches!(c.kind, ActionKind::Approve));
    assert_eq!((c.incident_id.as_str(), c.action.as_str(), c.workload.as_str()),
               ("inc_001", "restart_workload", "vllm"));
}

#[test]
fn enter_confirms_and_requests_exactly_one_approval() {
    let mut app = reviewing();
    app.handle_key(key('a'));
    settle(&mut app);
    let effects = app.handle_key(code(KeyCode::Enter));
    assert_eq!(effects, vec![Effect::Approve("inc_001".into())]);
    assert!(app.confirm.is_none() && app.busy.is_some());
}

#[test]
fn esc_cancels_the_confirmation_without_acting() {
    let mut app = reviewing();
    app.handle_key(key('a'));
    assert!(app.handle_key(code(KeyCode::Esc)).is_empty());
    assert!(app.confirm.is_none() && app.busy.is_none());
}

#[test]
fn other_keys_cannot_confirm_or_switch_the_pending_action() {
    let mut app = reviewing();
    app.handle_key(key('a'));
    for k in [key('a'), key('r'), key('y'), key('s'), code(KeyCode::Down), code(KeyCode::Tab)] {
        assert!(app.handle_key(k).is_empty());
    }
    let c = app.confirm.as_ref().unwrap();
    assert!(matches!(c.kind, ActionKind::Approve));          // still the original approval prompt
    assert!(app.busy.is_none());
}

#[test]
fn reject_requires_its_own_confirmation_and_calls_reject() {
    let mut app = reviewing();
    assert!(app.handle_key(key('r')).is_empty());
    assert!(matches!(app.confirm.as_ref().unwrap().kind, ActionKind::Reject));
    settle(&mut app);
    assert_eq!(app.handle_key(code(KeyCode::Enter)), vec![Effect::Reject("inc_001".into())]);
}

#[test]
fn a_second_action_cannot_start_while_one_is_in_flight() {
    let mut app = reviewing();
    app.handle_key(key('a'));
    settle(&mut app);
    app.handle_key(code(KeyCode::Enter));
    assert!(app.handle_key(key('a')).is_empty() && app.confirm.is_none());
    assert!(app.handle_key(key('r')).is_empty() && app.confirm.is_none());
    assert!(app.notice.is_some());
}

#[test]
fn approval_is_not_offered_without_a_pending_proposal() {
    // resolved, no proposal
    let mut app = online(HEALTHY, vec![incident(RESOLVED, "inc_001")]);
    assert!(app.handle_key(key('a')).is_empty() && app.confirm.is_none() && app.notice.is_some());
    // insufficient evidence: the server proposed nothing
    let mut app = online(HEALTHY, vec![incident(INSUFFICIENT, "inc_001")]);
    assert!(app.handle_key(key('r')).is_empty() && app.confirm.is_none());
    // nothing selected
    let mut app = online(HEALTHY, vec![]);
    assert!(app.handle_key(key('a')).is_empty() && app.confirm.is_none());
}

#[test]
fn a_proposal_on_an_incident_that_is_not_awaiting_approval_is_not_actionable() {
    let mut i = incident(PENDING, "inc_001");
    i.status = "EXECUTING".into();
    let mut app = online(UNRESPONSIVE, vec![i]);
    assert!(app.handle_key(key('a')).is_empty() && app.confirm.is_none());
}

#[test]
fn approval_is_decided_from_the_incident_page() {
    let mut app = reviewing();
    app.handle_key(key('a'));
    settle(&mut app);
    assert_eq!(app.handle_key(code(KeyCode::Enter)), vec![Effect::Approve("inc_001".into())]);
}

#[test]
fn nothing_can_be_confirmed_while_the_control_plane_is_offline() {
    let mut app = with_pending();
    app.apply(Msg::Poll(Err(ApiError::Offline("down".into()))));
    assert!(app.handle_key(key('a')).is_empty() && app.confirm.is_none());
    assert!(app.notice.is_some());
}

#[test]
fn the_servers_answer_to_an_action_is_shown_and_does_not_rewrite_incident_state() {
    let mut app = with_pending();
    app.handle_key(key('a'));
    app.handle_key(code(KeyCode::Enter));
    app.apply(Msg::Action {
        kind: ActionKind::Approve,
        id: "inc_001".into(),
        result: Ok(incident(RESOLVED, "inc_001")),
    });
    assert!(app.busy.is_none());
    assert!(app.notice.as_ref().unwrap().text.contains("RESOLVED"));
    // The TUI never infers or applies a state: the list still shows what the last poll said.
    assert_eq!(app.rows()[0].status, "POLICY_CHECK");
}

#[test]
fn a_server_refusal_is_reported_with_its_code_and_clears_the_busy_flag() {
    let mut app = with_pending();
    app.handle_key(key('a'));
    app.handle_key(code(KeyCode::Enter));
    app.apply(Msg::Action {
        kind: ActionKind::Approve,
        id: "inc_001".into(),
        result: Err(ApiError::Server {
            status: 409, code: "POLICY_DENIED".into(), message: "Another remediation holds this workload.".into(),
        }),
    });
    assert!(app.busy.is_none());
    let n = app.notice.as_ref().unwrap();
    assert!(n.text.contains("POLICY_DENIED") && n.text.contains("Another remediation") && n.is_error);
}

// --- keys that must always work ----------------------------------------------------------------

#[test]
fn q_and_ctrl_c_quit_and_s_refreshes() {
    let mut app = online(HEALTHY, vec![]);
    assert_eq!(app.handle_key(key('q')), vec![Effect::Quit]);
    assert_eq!(
        app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
        vec![Effect::Quit]
    );
    assert_eq!(app.handle_key(key('s')), vec![Effect::RefreshNow]);
    assert_eq!(app.handle_key(key('S')), vec![Effect::RefreshNow]);
}

#[test]
fn ctrl_c_quits_even_with_a_confirmation_open() {
    let mut app = with_pending();
    app.handle_key(key('a'));
    assert_eq!(
        app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
        vec![Effect::Quit]
    );
}

#[test]
fn unknown_keys_are_ignored() {
    let mut app = with_pending();
    for k in [key('x'), key('z'), code(KeyCode::F(5)), code(KeyCode::Backspace)] {
        assert!(app.handle_key(k).is_empty());
    }
    assert!(app.confirm.is_none() && matches!(app.screen, Screen::Dashboard));
}

// --- derived live numbers: only from real counters, never invented ------------------------------

fn observed(status: &str, at: &str, count: f64, sum: f64) -> Snapshot {
    let mut s: Status = serde_json::from_str(status).unwrap();
    s.last_observed_at = Some(at.to_string());
    let o = s.last_observation.as_mut().unwrap();
    o.insert("vllm_e2e_latency_seconds_count".into(), count.into());
    o.insert("vllm_e2e_latency_seconds_sum".into(), sum.into());
    Snapshot::new(s, vec![])
}

#[test]
fn rates_are_unavailable_until_two_observations_exist() {
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:00+00:00", 10.0, 1.0))));
    assert!(app.rates.requests_per_min.is_none() && app.rates.latency_ms.is_none());
}

#[test]
fn request_rate_and_recent_latency_come_from_counter_deltas() {
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:00+00:00", 10.0, 1.0))));
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:10+00:00", 20.0, 1.5))));
    assert!((app.rates.requests_per_min.unwrap() - 60.0).abs() < 1e-6);   // 10 req in 10 s
    assert!((app.rates.latency_ms.unwrap() - 50.0).abs() < 1e-6);         // 0.5 s over 10 req
}

#[test]
fn an_idle_window_has_a_zero_rate_and_no_latency() {
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:00+00:00", 10.0, 1.0))));
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:10+00:00", 10.0, 1.0))));
    assert_eq!(app.rates.requests_per_min, Some(0.0));
    assert!(app.rates.latency_ms.is_none());
}

#[test]
fn a_counter_reset_is_not_shown_as_a_negative_rate() {
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:00+00:00", 500.0, 40.0))));
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:10+00:00", 3.0, 0.2))));
    assert!(app.rates.requests_per_min.is_none() && app.rates.latency_ms.is_none());
}

#[test]
fn rates_are_not_recomputed_from_the_same_observation_polled_twice() {
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:00+00:00", 10.0, 1.0))));
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:10+00:00", 20.0, 1.5))));
    let before = app.rates.requests_per_min;
    app.apply(Msg::Poll(Ok(observed(HEALTHY, "2026-10-01T15:00:10+00:00", 20.0, 1.5))));
    assert_eq!(app.rates.requests_per_min, before);
}

#[test]
fn an_observation_without_the_counters_yields_no_rates() {
    let mut app = online(UNRESPONSIVE, vec![]);
    app.apply(Msg::Poll(Ok(snapshot(UNRESPONSIVE, vec![]))));
    assert!(app.rates.requests_per_min.is_none() && app.rates.latency_ms.is_none());
}

#[test]
fn telemetry_age_comes_from_the_servers_timestamp_and_the_wall_clock() {
    let mut app = online(HEALTHY, vec![]);
    app.wall = 1_790_866_931.0 + 0.25 + 4.0;                        // observed at :11.25, 4 s later
    let age = app.telemetry_age_secs().unwrap();
    assert!((age - 4.0).abs() < 0.01, "{age}");
    assert!(!app.telemetry_stale());
    app.wall += 30.0;
    assert!(app.telemetry_stale());
}

#[test]
fn time_never_runs_backwards_into_a_negative_age() {
    let mut app = online(HEALTHY, vec![]);
    app.wall = 1_790_866_931.0 - 100.0;
    assert_eq!(app.telemetry_age_secs(), Some(0.0));
    let _ = Instant::now();
}

#[test]
fn a_client_timeout_on_approve_is_not_reported_as_a_failure_and_is_never_retried() {
    let mut app = reviewing();
    app.handle_key(key('a'));
    settle(&mut app);
    assert_eq!(app.handle_key(code(KeyCode::Enter)), vec![Effect::Approve("inc_001".into())]);
    app.apply(Msg::Action { kind: ActionKind::Approve, id: "inc_001".into(), result: Err(ApiError::Timeout) });
    let n = app.active_notice().expect("the operator is told");
    assert!(n.text.contains("may still be working"), "{}", n.text);
    assert!(!n.text.contains("failed"), "a timeout must not read as a failed action: {}", n.text);
    assert!(app.busy.is_none() && app.confirm.is_none());
    // nothing re-sends by itself: only a fresh A + Enter can, and the incident state decides if it is allowed
    assert!(app.handle_key(key('s')) == vec![Effect::RefreshNow]);
}

// --- review before deciding; the confirmation cannot be fat-fingered ------------------------------

#[test]
fn a_and_r_do_nothing_but_explain_on_the_list_screens() {
    for screen_key in ['1', '2'] {
        let mut app = with_pending();
        app.handle_key(key(screen_key));
        for k in ['a', 'r'] {
            assert!(app.handle_key(key(k)).is_empty() && app.confirm.is_none());
            let n = app.active_notice().expect("it says why");
            assert!(n.text.contains("open the incident") && n.is_error, "{}", n.text);
        }
    }
}

#[test]
fn the_confirmation_carries_the_incident_the_reason_and_the_workload() {
    let app = {
        let mut a = reviewing();
        a.handle_key(key('a'));
        a
    };
    let c = app.confirm.as_ref().unwrap();
    assert_eq!(c.category, "INFERENCE_UNRESPONSIVE");
    assert!(c.why.contains("Inference requests are failing or timing out"), "{}", c.why);
    assert_eq!(c.workload, "vllm");
}

#[test]
fn enter_is_ignored_until_the_confirmation_guard_has_elapsed() {
    let mut app = reviewing();                       // Enter just opened this incident
    app.handle_key(key('a'));
    assert!(app.handle_key(code(KeyCode::Enter)).is_empty(), "a held or repeated Enter must not confirm");
    assert!(app.confirm.is_some() && app.busy.is_none());
    app.now += Duration::from_millis(200);
    assert!(app.handle_key(code(KeyCode::Enter)).is_empty());
    app.now += Duration::from_millis(400);           // 600 ms after it opened
    assert_eq!(app.handle_key(code(KeyCode::Enter)), vec![Effect::Approve("inc_001".into())]);
}

#[test]
fn esc_works_immediately_even_inside_the_guard() {
    let mut app = reviewing();
    app.handle_key(key('r'));
    assert!(app.handle_key(code(KeyCode::Esc)).is_empty() && app.confirm.is_none());
}

#[test]
fn a_decision_waits_for_the_incidents_details_to_load() {
    let mut app = with_pending();                    // list data only: no RCA yet
    app.handle_key(code(KeyCode::Enter));
    assert!(app.handle_key(key('a')).is_empty() && app.confirm.is_none());
    assert!(app.active_notice().unwrap().text.contains("still loading"));
    let inc = incident(PENDING, "inc_001");
    let mut snap = snapshot(UNRESPONSIVE, vec![inc.clone()]);
    snap.detail = Some(inc);
    app.apply(Msg::Poll(Ok(snap)));                  // the detail arrives on the next poll
    app.handle_key(key('a'));
    assert!(app.confirm.is_some());
}
