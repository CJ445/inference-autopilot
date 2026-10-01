//! Settings, Diagnostics, About, Control Plane, Help, the command palette and the stop
//! confirmation: what they show, what they ask the server for, and what they never do.
use aiops_tui::api::ApiError;
use aiops_tui::app::{App, ClientInfo, Command, Effect, Loaded, Msg, Screen, Snapshot, KEYMAP};
use aiops_tui::model::{ConfigInfo, Diagnostics, Status, StopAck, VersionInfo};
use aiops_tui::{theme, ui};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::time::Duration;

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");

fn key(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
}
fn ctrl(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
}
fn code(k: KeyCode) -> KeyEvent {
    KeyEvent::new(k, KeyModifiers::NONE)
}
fn app() -> App {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = 1_790_866_932.0;
    a.details = true;                       // these tests cover the technical views
    a.client = ClientInfo { interval_ms: Some(750), timeout_ms: Some(2000) };
    let st: Status = serde_json::from_str(HEALTHY).unwrap();
    a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![]))));
    a
}
fn screen(a: &App) -> String {
    ui::render_to_string(a, 120, 44)
}
fn has(s: &str, needle: &str) {
    assert!(s.contains(needle), "missing {needle:?} in:\n{s}");
}
fn lacks(s: &str, needle: &str) {
    assert!(!s.contains(needle), "unexpected {needle:?} in:\n{s}");
}

fn config() -> ConfigInfo {
    serde_json::from_value(serde_json::json!({
        "source": "/etc/aiops/aiops.toml", "profile": "docker-real-gpu", "provider": "docker",
        "sections": {"workload": {"name": "vllm", "model": "facebook/opt-125m"},
                     "safety": {"max_temperature_c": 80, "max_gpu_memory_percent": 85}}
    }))
    .unwrap()
}
fn diagnostics() -> Diagnostics {
    serde_json::from_value(serde_json::json!({
        "ran_at": "2026-10-01T15:02:11+00:00", "duration_seconds": 1.4,
        "results": [
            {"check": "python", "status": "PASS", "detail": "3.13.5", "blocking": true},
            {"check": "gpu_headroom", "status": "WARN", "detail": "only 900 MiB free", "blocking": false},
            {"check": "vllm_probe", "status": "FAIL", "detail": "no answer in 3s", "blocking": false},
            {"check": "kubectl", "status": "NOT_APPLICABLE", "detail": "not applicable", "blocking": false}]
    }))
    .unwrap()
}

// --- navigation and what each screen asks for ----------------------------------------------------

#[test]
fn opening_settings_about_and_diagnostics_asks_the_server_once_each() {
    let mut a = app();
    assert_eq!(a.handle_key(key('5')), vec![Effect::LoadConfig]);
    assert!(a.handle_key(key('5')).is_empty(), "already loading: no duplicate request");
    assert_eq!(a.handle_key(key('7')), vec![Effect::LoadVersion]);
    assert_eq!(a.handle_key(key('6')), vec![Effect::RunDiagnostics]);
    assert!(a.handle_key(key('6')).is_empty());
    assert!(a.handle_key(key('4')).is_empty() && matches!(a.screen, Screen::ControlPlane));
    assert!(a.handle_key(key('?')).is_empty() && matches!(a.screen, Screen::Help));
}

#[test]
fn a_failed_read_is_retried_on_the_next_visit_not_cached_as_a_value() {
    let mut a = app();
    a.handle_key(key('5'));
    a.apply(Msg::Loaded(Loaded::Config(Err(ApiError::Timeout))));
    a.handle_key(code(KeyCode::Esc));
    assert_eq!(a.handle_key(key('5')), vec![Effect::LoadConfig]);
}

// --- settings ------------------------------------------------------------------------------------

#[test]
fn settings_separate_client_preferences_from_read_only_control_plane_configuration() {
    let mut a = app();
    a.handle_key(key('5'));
    has(&screen(&a), "Loading");
    a.apply(Msg::Loaded(Loaded::Config(Ok(config()))));
    let s = screen(&a);
    for needle in ["Client", "750 ms", "2000 ms", "--interval-ms", "Control plane configuration", "read-only",
                   "/etc/aiops/aiops.toml", "● Valid", "docker-real-gpu", "[workload]", "facebook/opt-125m",
                   "[safety]", "max_temperature_c", "80", "never changes it"] {
        has(&s, needle);
    }
}

#[test]
fn settings_never_invent_configuration_the_server_did_not_send() {
    let mut a = app();
    a.handle_key(key('5'));
    a.apply(Msg::Loaded(Loaded::Config(Err(ApiError::Offline("refused".into())))));
    let s = screen(&a);
    has(&s, "N/A: control plane offline");
    for invented in ["● Valid", "[workload]", "/etc/aiops"] {
        lacks(&s, invented);
    }
}

// --- diagnostics ---------------------------------------------------------------------------------

#[test]
fn diagnostics_show_the_doctors_statuses_unreinterpreted() {
    let mut a = app();
    a.handle_key(key('6'));
    has(&screen(&a), "Loading");
    a.apply(Msg::Loaded(Loaded::Diagnostics(Ok(diagnostics()))));
    let s = screen(&a);
    for needle in ["● PASS", "● WARN", "● FAIL", "○ NOT_APPLICABLE", "python", "3.13.5", "gpu_headroom",
                   "1 PASS · 1 WARN · 1 FAIL · 1 NOT_APPLICABLE", "Failures", "Warnings",
                   "only 900 MiB free", "does not block start", "ran in 1.4s"] {
        has(&s, needle);
    }
}

#[test]
fn d_runs_diagnostics_again_but_never_two_at_once_and_only_on_that_screen() {
    let mut a = app();
    assert!(a.handle_key(key('d')).is_empty(), "D means nothing elsewhere");
    a.handle_key(key('6'));
    a.apply(Msg::Loaded(Loaded::Diagnostics(Ok(diagnostics()))));
    assert_eq!(a.handle_key(key('d')), vec![Effect::RunDiagnostics]);
    assert!(a.handle_key(key('d')).is_empty(), "one run at a time");
}

#[test]
fn diagnostics_need_the_control_plane_and_say_so_when_it_is_unreachable() {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Err(ApiError::Offline("refused".into()))));
    assert!(a.handle_key(key('6')).is_empty());
    assert!(a.active_notice().unwrap().text.contains("unreachable"));
}

// --- about ---------------------------------------------------------------------------------------

#[test]
fn about_shows_the_real_versions_and_omits_what_is_not_reported() {
    let mut a = app();
    a.handle_key(key('7'));
    a.apply(Msg::Loaded(Loaded::Version(Ok(VersionInfo {
        version: "0.1.0".into(), git_revision: None, python: Some("3.13.5".into()), platform: None,
    }))));
    let s = screen(&a);
    for needle in ["Operator UI", env!("CARGO_PKG_VERSION"), "Control plane", "0.1.0", "3.13.5", "● connected"] {
        has(&s, needle);
    }
    lacks(&s, "Git revision");           // not reported: omitted, never a placeholder hash
    lacks(&s, "Platform");
}

#[test]
fn about_without_an_answer_says_na() {
    let mut a = app();
    a.handle_key(key('7'));
    a.apply(Msg::Loaded(Loaded::Version(Err(ApiError::Timeout))));
    has(&screen(&a), "N/A: control plane did not answer in time");
}

// --- control plane screen and the stop path ----------------------------------------------------------

#[test]
fn the_control_plane_screen_shows_only_what_the_server_reported() {
    let mut a = app();
    a.handle_key(key('4'));
    let s = screen(&a);
    for needle in ["Control plane", "● ONLINE", "http://127.0.0.1:8080", "4242", "docker-real-gpu", "docker",
                   "vllm", "facebook/opt-125m", "● ARMED", "[ X ] Stop control plane"] {
        has(&s, needle);
    }
    has(&s, "Started");
    has(&s, "N/A");                       // the fixture reports no started_at
}

#[test]
fn stop_needs_x_then_an_explicit_enter_and_sends_exactly_one_request() {
    let mut a = app();
    assert!(a.handle_key(key('x')).is_empty() && !a.stop_confirm, "X does nothing off the Control Plane screen");
    a.handle_key(key('4'));
    assert!(a.handle_key(key('x')).is_empty() && a.stop_confirm);
    let s = screen(&a);
    for needle in ["Stop control plane?", "operator UI will disconnect", "managed workload is not touched", "[Enter] Stop", "[Esc] Cancel"] {
        has(&s, needle);
    }
    assert!(a.handle_key(key('q')).is_empty(), "other keys cannot confirm or quit");
    assert!(a.handle_key(key('a')).is_empty());
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty(), "inside the guard Enter is ignored");
    a.now += Duration::from_millis(600);
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::StopControlPlane]);
    assert!(!a.stop_confirm);
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty(), "Enter again sends nothing");
}

#[test]
fn esc_cancels_the_stop_without_sending_anything() {
    let mut a = app();
    a.handle_key(key('4'));
    a.handle_key(key('x'));
    assert!(a.handle_key(code(KeyCode::Esc)).is_empty() && !a.stop_confirm);
    assert!(a.stop_sent.is_none());
}

#[test]
fn stop_cannot_be_started_while_the_control_plane_is_unreachable() {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Err(ApiError::Offline("refused".into()))));
    a.handle_key(key('4'));
    assert!(a.handle_key(key('x')).is_empty() && !a.stop_confirm);
    assert!(a.active_notice().unwrap().is_error);
}

#[test]
fn a_stop_timeout_is_not_a_failure_and_is_never_resent() {
    let mut a = app();
    a.handle_key(key('4'));
    a.handle_key(key('x'));
    a.now += Duration::from_millis(600);
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::StopControlPlane]);
    a.apply(Msg::Loaded(Loaded::Stop(Err(ApiError::Timeout))));
    let n = a.active_notice().unwrap();
    assert!(n.text.contains("may already be stopping") && !n.text.contains("failed"), "{}", n.text);
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty(), "nothing re-sends it");
}

#[test]
fn an_accepted_stop_is_reported_and_a_refusal_is_reported_as_one() {
    let mut a = app();
    a.apply(Msg::Loaded(Loaded::Stop(Ok(StopAck { stopping: true }))));
    assert!(a.active_notice().unwrap().text.contains("run `aiops`"));
    a.apply(Msg::Loaded(Loaded::Stop(Err(ApiError::Server {
        status: 400, code: "CONFIRMATION_REQUIRED".into(), message: "x".into(),
    }))));
    let n = a.active_notice().unwrap();
    assert!(n.is_error && n.text.contains("CONFIRMATION_REQUIRED"));
}

// --- command palette -------------------------------------------------------------------------------

#[test]
fn ctrl_p_opens_the_palette_and_esc_closes_it() {
    let mut a = app();
    a.handle_key(ctrl('p'));
    assert!(a.palette.is_some());
    let s = screen(&a);
    for needle in ["Commands", "Search commands", "Open Overview", "Open Settings", "Open Diagnostics",
                   "Open About", "Open Help", "Refresh", "Stop control plane", "Quit"] {
        has(&s, needle);
    }
    a.handle_key(code(KeyCode::Esc));
    assert!(a.palette.is_none());
    lacks(&screen(&a), "Search commands");
}

#[test]
fn the_palette_filters_navigates_and_executes() {
    let mut a = app();
    a.handle_key(ctrl('p'));
    for c in "diag".chars() {
        a.handle_key(key(c));
    }
    assert_eq!(a.palette.as_ref().unwrap().matches(Default::default()), vec![Command::Diagnostics]);
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::RunDiagnostics]);
    assert!(a.palette.is_none() && matches!(a.screen, Screen::Diagnostics));

    a.handle_key(ctrl('p'));
    a.handle_key(code(KeyCode::Down));
    a.handle_key(code(KeyCode::Down));
    a.handle_key(code(KeyCode::Up));
    assert_eq!(a.palette.as_ref().unwrap().selected, 1);
    a.handle_key(code(KeyCode::Enter));               // the second command: Open Incidents
    assert!(matches!(a.screen, Screen::Incidents));
}

#[test]
fn typing_in_the_palette_never_triggers_the_keys_it_resembles() {
    let mut a = app();
    a.handle_key(ctrl('p'));
    for c in "qarsx".chars() {
        assert!(a.handle_key(key(c)).is_empty(), "{c} must only type");
    }
    assert_eq!(a.palette.as_ref().unwrap().query, "qarsx");
    assert!(a.confirm.is_none() && !a.stop_confirm);
    a.handle_key(code(KeyCode::Backspace));
    assert_eq!(a.palette.as_ref().unwrap().query, "qars");
}

#[test]
fn a_query_with_no_match_executes_nothing() {
    let mut a = app();
    a.handle_key(ctrl('p'));
    for c in "zzz".chars() {
        a.handle_key(key(c));
    }
    has(&screen(&a), "No matching command");
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty() && a.palette.is_none());
}

#[test]
fn the_palette_stop_command_only_opens_the_same_confirmation() {
    let mut a = app();
    a.handle_key(ctrl('p'));
    for c in "stop".chars() {
        a.handle_key(key(c));
    }
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty());     // no request yet
    assert!(a.stop_confirm);
    a.now += Duration::from_millis(600);
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::StopControlPlane]);
}

#[test]
fn the_palette_offers_no_approve_reject_restart_or_remediation_command() {
    // Deciding and remediating happen on an incident's own page, never from the palette. The only
    // palette commands that mention the workload are the two guarded fault commands (pause and
    // resume), which are testing tools, not remediation.
    for c in Command::ALL {
        let l = c.label().to_lowercase();
        for forbidden in ["approve", "reject", "restart", "remediat"] {
            assert!(!l.contains(forbidden), "{l}");
        }
        if l.contains("workload") {
            assert!(matches!(c, Command::BreakWorkload | Command::ResumeWorkload), "{l}");
        }
    }
}

#[test]
fn a_confirmation_in_progress_blocks_the_palette() {
    let mut a = app();
    a.handle_key(key('4'));
    a.handle_key(key('x'));
    a.handle_key(ctrl('p'));
    assert!(a.palette.is_none());
}

#[test]
fn other_control_keys_are_not_the_plain_commands() {
    let mut a = app();
    assert!(a.handle_key(ctrl('a')).is_empty() && a.confirm.is_none(), "Ctrl+A is not Approve");
    assert!(a.handle_key(ctrl('q')).is_empty(), "Ctrl+Q is not Quit");
}

// --- help matches reality ---------------------------------------------------------------------------

#[test]
fn help_lists_exactly_the_keys_that_work() {
    let mut a = app();
    a.handle_key(key('?'));
    let s = screen(&a);
    for (k, what) in KEYMAP {
        has(&s, k);
        has(&s, what);
    }
    // every documented key really does what Help says
    let mut b = app();
    assert_eq!(b.handle_key(key('s')), vec![Effect::RefreshNow]);
    assert_eq!(b.handle_key(key('q')), vec![Effect::Quit]);
    assert_eq!(b.handle_key(ctrl('c')), vec![Effect::Quit]);
    b.handle_key(code(KeyCode::Tab));
    assert!(matches!(b.screen, Screen::Incidents));
    b.handle_key(code(KeyCode::Esc));
    assert!(matches!(b.screen, Screen::Dashboard));
    b.handle_key(ctrl('p'));
    assert!(b.palette.is_some());
    b.handle_key(code(KeyCode::Esc));
    b.handle_key(key('?'));
    assert!(matches!(b.screen, Screen::Help));
    for (n, c) in "1234567".chars().enumerate() {
        b.handle_key(key(c));
        assert_eq!(b.screen, aiops_tui::app::SCREEN_ORDER[n], "key {c}");
    }
}

#[test]
fn the_sidebar_lists_every_screen_and_marks_the_current_one() {
    let mut a = app();
    a.handle_key(key('6'));
    let s = screen(&a);
    for needle in ["OVERVIEW", "INCIDENTS", "AUDIT", "SYSTEM", "Control Plane", "OPERATOR", "Settings",
                   "Diagnostics", "About", "Help"] {
        has(&s, needle);
    }
    let marked: Vec<&str> = s.lines().filter(|l| l.contains("▌")).collect();
    assert_eq!(marked.len(), 1);
    assert!(marked[0].contains("Diagnostics"));
}

#[test]
fn every_screen_renders_at_the_minimum_and_common_sizes_without_panicking() {
    let mut a = app();
    a.apply(Msg::Loaded(Loaded::Config(Ok(config()))));
    a.apply(Msg::Loaded(Loaded::Version(Ok(VersionInfo { version: "1".into(), git_revision: Some("abc".into()), python: None, platform: None }))));
    a.apply(Msg::Loaded(Loaded::Diagnostics(Ok(diagnostics()))));
    for (w, h) in [(72, 18), (80, 24), (120, 40), (200, 60)] {
        for n in "1234567?".chars() {
            a.handle_key(key(n));
            ui::render_to_string(&a, w, h);
        }
        a.handle_key(ctrl('p'));
        ui::render_to_string(&a, w, h);
        a.handle_key(code(KeyCode::Esc));
    }
}

#[test]
fn the_state_chips_use_the_semantic_palette() {
    let a = {
        let mut a = app();
        a.handle_key(key('6'));
        a.apply(Msg::Loaded(Loaded::Diagnostics(Ok(diagnostics()))));
        a
    };
    let buf = ui::render_to_buffer(&a, 120, 44);
    let find = |needle: &str| {
        let w = buf.area.width as usize;
        let cells = buf.content();
        for row in 0..buf.area.height as usize {
            let line: String = (0..w).map(|x| cells[row * w + x].symbol().to_string()).collect();
            if let Some(b) = line.find(needle) {
                return cells[row * w + line[..b].chars().count()].fg;
            }
        }
        panic!("{needle} not on screen")
    };
    assert_eq!(find("● PASS"), theme::HEALTHY);
    assert_eq!(find("● WARN"), theme::WARNING);
    assert_eq!(find("● FAIL"), theme::CRITICAL);
    assert_eq!(find("○ NOT_APPLICABLE"), theme::TEXT_MUTED);
}

// --- no workload: the control plane runs with zero workloads ---------------------------------------------

fn with_workload(state: Option<&str>, observation: bool) -> App {
    let mut st: Status = serde_json::from_str(HEALTHY).unwrap();
    st.workload = state.map(|s| serde_json::from_value(serde_json::json!({"name": "vllm", "state": s})).unwrap());
    if !observation {
        st.last_observation = None;
        st.last_observed_at = None;
    }
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = 1_790_866_932.0;
    a.details = true;                       // the technical view (the plain one is tested in plain_views.rs)
    a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![]))));
    a
}

#[test]
fn an_absent_workload_says_so_and_shows_no_workload_telemetry() {
    let s = screen(&with_workload(Some("absent"), false));
    for needle in ["○ NO WORKLOAD", "No workload running", "nothing is observed", "picked up automatically",
                   "● CONTROL ONLINE", "No active incidents", "No observation"] {
        has(&s, needle);
    }
    for invented in ["● HEALTHY", "✗ UNRESPONSIVE", "Probe", "Metrics", "KV cache", "Running 0", "35.4%"] {
        lacks(&s, invented);
    }
}

#[test]
fn a_stopped_workload_is_shown_as_stopped_not_unresponsive() {
    let s = screen(&with_workload(Some("stopped"), false));
    for needle in ["○ STOPPED", "Workload stopped"] {
        has(&s, needle);
    }
    for invented in ["● HEALTHY", "✗ UNRESPONSIVE", "Probe"] {
        lacks(&s, invented);
    }
}

#[test]
fn a_running_workload_shows_its_telemetry_as_before() {
    let s = screen(&with_workload(Some("running"), true));
    for needle in ["● HEALTHY", "Probe ✓", "Metrics ✓", "35.4%", "Observed"] {
        has(&s, needle);
    }
    lacks(&s, "No workload running");
}

#[test]
fn a_server_that_does_not_report_the_workload_changes_nothing() {
    let s = screen(&with_workload(None, true));
    has(&s, "● HEALTHY");
    lacks(&s, "NO WORKLOAD");
}

#[test]
fn the_view_follows_the_workload_as_it_appears_and_disappears() {
    let mut a = with_workload(Some("absent"), false);
    has(&screen(&a), "No workload running");
    let running: Status = {
        let mut st: Status = serde_json::from_str(HEALTHY).unwrap();
        st.workload = Some(serde_json::from_value(serde_json::json!({"name": "vllm", "state": "running"})).unwrap());
        st
    };
    a.apply(Msg::Poll(Ok(Snapshot::new(running, vec![]))));
    let s = screen(&a);
    has(&s, "Probe ✓");
    lacks(&s, "No workload running");
    let gone: Status = {
        let mut st: Status = serde_json::from_str(HEALTHY).unwrap();
        st.workload = Some(serde_json::from_value(serde_json::json!({"name": "vllm", "state": "absent"})).unwrap());
        st.last_observation = None;
        st.last_observed_at = None;
        st
    };
    a.apply(Msg::Poll(Ok(Snapshot::new(gone, vec![]))));
    let s = screen(&a);
    has(&s, "No workload running");
    lacks(&s, "Probe ✓");
}

#[test]
fn the_control_plane_screen_shows_the_workload_state() {
    for (state, needle) in [("absent", "No workload running"), ("stopped", "Workload stopped"),
                            ("running", "● running"), ("unknown", "unknown")] {
        let mut a = with_workload(Some(state), state == "running");
        a.handle_key(key('4'));
        has(&screen(&a), needle);
    }
}

#[test]
fn a_workload_in_its_start_period_shows_as_starting_not_unresponsive() {
    let mut a = with_workload(Some("starting"), true);
    {
        // the model is still loading: the probe fails, and that is not what the operator is told
        let mut st: Status = serde_json::from_str(HEALTHY).unwrap();
        st.workload = Some(serde_json::from_value(serde_json::json!({"name": "vllm", "state": "starting"})).unwrap());
        st.last_observation.as_mut().unwrap().insert("inference_probe_ok".into(), false.into());
        st.last_observation.as_mut().unwrap().insert("inference_probe_error".into(), "refused".into());
        a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![]))));
    }
    let s = screen(&a);
    has(&s, "◔ STARTING");
    lacks(&s, "✗ UNRESPONSIVE");
    a.handle_key(key('4'));
    has(&screen(&a), "◔ starting");
}

#[test]
fn left_and_right_move_through_the_sidebar_screens_and_wrap() {
    use aiops_tui::app::SCREEN_ORDER;
    let mut a = app();
    for n in 1..SCREEN_ORDER.len() {
        a.handle_key(code(KeyCode::Right));
        assert_eq!(a.screen, SCREEN_ORDER[n]);
    }
    a.handle_key(code(KeyCode::Right));
    assert_eq!(a.screen, SCREEN_ORDER[0], "wraps from the last screen to the first");
    a.handle_key(code(KeyCode::Left));
    assert_eq!(a.screen, SCREEN_ORDER[SCREEN_ORDER.len() - 1], "and back");
    a.handle_key(code(KeyCode::Left));
    assert_eq!(a.screen, SCREEN_ORDER[SCREEN_ORDER.len() - 2]);
}

#[test]
fn left_and_right_load_what_the_new_screen_needs_and_are_ignored_in_overlays() {
    let mut a = app();
    for _ in 0..4 {
        a.handle_key(code(KeyCode::Right)); // Incidents, Audit, Control Plane
    }
    assert!(matches!(a.screen, Screen::Settings));
    assert_eq!(a.config.is_loading(), true, "arriving by arrow asks the server like any other way in");
    a.handle_key(ctrl('p'));
    assert!(a.handle_key(code(KeyCode::Left)).is_empty());
    assert!(matches!(a.screen, Screen::Settings), "the palette keeps the arrows");
}

#[test]
fn a_detail_view_steps_from_incidents() {
    let inc: aiops_tui::model::Incident =
        serde_json::from_str(include_str!("fixtures/incident_pending.json")).unwrap();
    let st: Status = serde_json::from_str(HEALTHY).unwrap();
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![inc]))));
    a.handle_key(code(KeyCode::Enter));
    assert!(matches!(a.screen, Screen::Detail(_)));
    a.handle_key(code(KeyCode::Right));
    assert!(matches!(a.screen, Screen::Audit));
}
