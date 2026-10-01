//! What the operator actually sees, rendered through ratatui's test backend.
use aiops_tui::api::ApiError;
use aiops_tui::app::{App, Msg, Snapshot};
use aiops_tui::model::{Audit, Incident, Status};
use aiops_tui::ui;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::buffer::Buffer;
use aiops_tui::theme;
use ratatui::style::Color;

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");
const UNRESPONSIVE: &str = include_str!("fixtures/status_unresponsive.json");
const PENDING: &str = include_str!("fixtures/incident_pending.json");
const RESOLVED: &str = include_str!("fixtures/incident_resolved.json");
const UNRESOLVED: &str = include_str!("fixtures/incident_unresolved.json");
const INSUFFICIENT: &str = include_str!("fixtures/incident_insufficient.json");
const AUDIT: &str = include_str!("fixtures/audit.json");
const AUDIT_INVALID: &str = include_str!("fixtures/audit_invalid.json");

const T: f64 = 1_790_866_931.0; // 2026-10-01T15:02:11Z

fn incident(json: &str, id: &str) -> Incident {
    let mut i: Incident = serde_json::from_str(json).unwrap();
    i.incident_id = id.into();
    i
}
fn status_of(json: &str) -> Status {
    status(json)
}
fn status(json: &str) -> Status {
    serde_json::from_str(json).unwrap()
}
fn app_with(snapshot: Snapshot) -> App {
    let mut app = App::new("http://127.0.0.1:8080".into());
    app.wall = T + 1.25; // one second after the fixture's observation
    app.details = true; // these tests cover the TECHNICAL views, now behind `D`
    app.apply(Msg::Poll(Ok(snapshot)));
    app
}
fn app(st: &str, incidents: Vec<Incident>) -> App {
    app_with(Snapshot::new(status(st), incidents))
}
fn key(app: &mut App, code: KeyCode) {
    app.handle_key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn ch(app: &mut App, c: char) {
    key(app, KeyCode::Char(c));
}
fn text(app: &App, w: u16, h: u16) -> String {
    ui::render_to_string(app, w, h)
}
fn has(screen: &str, needle: &str) {
    assert!(screen.contains(needle), "missing {needle:?} in:\n{screen}");
}
fn lacks(screen: &str, needle: &str) {
    assert!(!screen.contains(needle), "unexpected {needle:?} in:\n{screen}");
}
/// Foreground colour of the first cell of `needle` (rendered on one row).
fn colour_of(buf: &Buffer, needle: &str) -> Color {
    let w = buf.area.width as usize;
    let cells = buf.content();
    for row in 0..buf.area.height as usize {
        let line: String = (0..w).map(|x| cells[row * w + x].symbol().to_string()).collect();
        if let Some(byte) = line.find(needle) {
            let col = line[..byte].chars().count();
            return cells[row * w + col].fg;
        }
    }
    panic!("{needle:?} not on screen");
}

// --- dashboard -------------------------------------------------------------------------------

#[test]
fn a_healthy_dashboard_shows_real_gpu_vllm_control_plane_and_safety_state() {
    let a = app(HEALTHY, vec![]);       // `app` renders the technical view (D)
    let s = text(&a, 110, 32);
    for needle in [
        "INFERENCE AUTOPILOT", "● CONTROL ONLINE", "docker-real-gpu", "GPU 0", "GPU-1e5dd8d1", "VRAM",
        "35.4%", "2.8 / 8.0 GiB", "59°C", "UTIL 23%", "vLLM · facebook/opt-125m", "● HEALTHY",
        "Probe ✓", "18 ms", "Metrics ✓", "KV cache 0.0%", "Running 0", "Waiting 0",
        "No active incidents", "WATCHDOG", "● ARMED", "Identity ✓", "Budgets ✓",
        "AUDIT ✓ VERIFIED", "Observed 1.0s ago", "1 Home", "2 Incidents", "3 Lab", "4 Activity", "5 System",
        "Review", "Quit",
    ] {
        has(&s, needle);
    }
}

#[test]
fn nothing_on_screen_is_hard_coded() {
    let mut st = status(HEALTHY);
    st.info.workload = Some("other-workload".into());
    st.info.model = Some("some/other-model".into());
    st.info.profile = Some("custom-profile".into());
    let o = st.last_observation.as_mut().unwrap();
    o.insert("gpu_temperature_c".into(), 71.0.into());
    o.insert("gpu_utilization_percent".into(), 5.0.into());
    o.insert("gpu_memory_used_bytes".into(), 6_442_450_944.0.into()); // 6.0 GiB
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    for needle in ["other-workload", "some/other-model", "custom-profile", "71°C", "UTIL 5%", "6.0 / 8.0 GiB"] {
        has(&s, needle);
    }
    for stale_value in ["opt-125m", "59°C", "docker-real-gpu", "2.8 /"] {
        lacks(&s, stale_value);
    }
}

#[test]
fn a_degraded_control_plane_is_not_shown_as_running() {
    let mut st = status(HEALTHY);
    st.health = "DEGRADED".into();
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    has(&s, "! DEGRADED");
    lacks(&s, "● CONTROL ONLINE");
}

#[test]
fn values_the_server_did_not_send_are_na_never_invented() {
    let mut st = status(HEALTHY);
    let o = st.last_observation.as_mut().unwrap();
    for k in ["vllm_kv_cache_usage", "vllm_requests_running", "vllm_requests_waiting",
              "vllm_e2e_latency_seconds_sum", "vllm_e2e_latency_seconds_count", "gpu_temperature_c"] {
        o.remove(k);
    }
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    for needle in ["KV cache N/A", "Running N/A", "Waiting N/A", "Req/min N/A", "Latency N/A",
                   "TEMP N/A", "Tokens/s N/A"] {
        has(&s, needle);
    }
}

#[test]
fn without_any_observation_the_gpu_and_vllm_panels_say_na() {
    let mut st = status(HEALTHY);
    st.last_observation = None;
    st.last_observed_at = None;
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    has(&s, "GPU N/A");
    has(&s, "VRAM           N/A");
    has(&s, "No observation");
    lacks(&s, "35.4%");
}

#[test]
fn live_rates_appear_only_when_two_real_observations_exist() {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.details = true;
    a.wall = T + 12.0;
    for (at, count, sum) in [("2026-10-01T15:02:01+00:00", 10.0, 1.0), ("2026-10-01T15:02:11+00:00", 20.0, 1.5)] {
        let mut st = status(HEALTHY);
        st.last_observed_at = Some(at.into());
        let o = st.last_observation.as_mut().unwrap();
        o.insert("vllm_e2e_latency_seconds_count".into(), count.into());
        o.insert("vllm_e2e_latency_seconds_sum".into(), sum.into());
        a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![]))));
    }
    let s = text(&a, 110, 32);
    has(&s, "Req/min 60");
    has(&s, "Latency 50 ms");
}

#[test]
fn an_unresponsive_workload_is_unmistakable_and_the_incident_is_listed() {
    let a = app(UNRESPONSIVE, vec![incident(PENDING, "inc_001")]);
    let s = text(&a, 110, 32);
    for needle in ["✗ UNRESPONSIVE", "Probe ✗ timeout", "Metrics ✗", "inc_001",
                   "INFERENCE_UNRESPONSIVE", "AWAITING APPROVAL", "restart_workload"] {
        has(&s, needle);
    }
    lacks(&s, "No active incidents");
    assert_eq!(colour_of(&ui::render_to_buffer(&a, 110, 32), "✗ UNRESPONSIVE"), theme::CRITICAL);
}

#[test]
fn finished_incidents_are_listed_as_recent_with_the_servers_state() {
    let a = app(HEALTHY, vec![incident(RESOLVED, "inc_001")]);
    let s = text(&a, 110, 32);
    has(&s, "RECENT");
    has(&s, "15:02:47");
    has(&s, "→ RESOLVED");
    has(&s, "RECENT");
    has(&s, "No active incidents");
}

#[test]
fn the_selected_incident_is_marked() {
    let mut a = app(UNRESPONSIVE, vec![incident(PENDING, "inc_001"), incident(PENDING, "inc_002")]);
    has(&text(&a, 110, 32), "› inc_002");
    key(&mut a, KeyCode::Down);
    has(&text(&a, 110, 32), "› inc_001");
}

// --- incident list and detail ----------------------------------------------------------------

#[test]
fn the_incidents_screen_is_a_compact_table() {
    let mut a = app(UNRESPONSIVE, vec![incident(PENDING, "inc_001"), incident(RESOLVED, "inc_000")]);
    key(&mut a, KeyCode::Tab);
    let s = text(&a, 140, 24);
    for needle in ["ID", "CATEGORY", "STATE", "WORKLOAD", "CREATED", "PROPOSAL",
                   "inc_001", "15:02:11", "vllm", "restart_workload", "● AWAITING APPROVAL", "● RESOLVED"] {
        has(&s, needle);
    }
}

fn detail_app(json: &str) -> App {
    let inc = incident(json, "inc_001");
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = app_with(snap);
    key(&mut a, KeyCode::Enter);
    a
}

#[test]
fn a_pending_incident_detail_shows_evidence_rca_proposal_policy_and_no_verification() {
    let s = text(&detail_app(PENDING), 110, 60);
    for needle in [
        "inc_001", "INFERENCE_UNRESPONSIVE", "vllm", "Evidence", "Inference probe",
        "FAILED (timeout)", "vllm-probe", "GPU memory", "2.8 GiB used", "vLLM metrics", "UNAVAILABLE",
        "Deterministic RCA", "Classification", "SUPPORTED", "Inference requests are failing or timing out",
        "Remediation", "restart_workload", "workload=vllm", "Policy", "APPROVAL REQUIRED", "PENDING",
        "[ A ] Approve", "[ R ] Reject", "Verification", "Not started", "Timeline",
        "15:02:11  DETECTED", "15:02:12  POLICY_CHECK", "● AWAITING APPROVAL",
        "Evidence › RCA › Proposal › Policy › Approval › Execute › Verify › Result",
    ] {
        has(&s, needle);
    }
}

#[test]
fn a_resolved_incident_shows_the_recovery_checks_the_server_recorded() {
    let s = text(&detail_app(RESOLVED), 110, 70);
    for needle in [
        "Verification", "✓ Workload identity changed", "✓ GPU observable", "✓ vLLM metrics readable",
        "✓ Stable window of real completions", "RESULT", "● RESOLVED", "15:02:18  APPROVED",
        "15:02:18  EXECUTING", "15:02:22  VERIFYING", "15:02:47  RESOLVED",
    ] {
        has(&s, needle);
    }
}

#[test]
fn a_failed_verification_shows_each_failed_check_and_the_unresolved_result() {
    let s = text(&detail_app(UNRESOLVED), 110, 70);
    has(&s, "✗ vLLM metrics readable");
    has(&s, "✗ Stable window of real completions");
    has(&s, "✓ Workload identity changed");
    has(&s, "● UNRESOLVED");
}

#[test]
fn resolved_is_never_claimed_unless_the_server_says_so() {
    let mut inc = incident(RESOLVED, "inc_001");
    inc.status = "VERIFYING".into();
    inc.timeline.pop();                     // a realistic timeline: it has not reached RESOLVED
    inc.verification = None;
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = app_with(snap);
    key(&mut a, KeyCode::Enter);
    let s = text(&a, 110, 70);
    lacks(&s, "RESOLVED");
    has(&s, "VERIFYING");
    has(&s, "In progress");
}

#[test]
fn an_incident_with_insufficient_evidence_says_so_and_offers_no_proposal() {
    let s = text(&detail_app(INSUFFICIENT), 110, 60);
    has(&s, "INSUFFICIENT EVIDENCE");
    has(&s, "Insufficient evidence");
    has(&s, "No proposal");
    lacks(&s, "AWAITING APPROVAL");
    lacks(&s, "[ A ] Approve");
}

#[test]
fn a_long_detail_can_be_scrolled() {
    let mut a = detail_app(RESOLVED);
    let before = text(&a, 110, 24);
    for _ in 0..30 {
        key(&mut a, KeyCode::Down);
    }
    assert_ne!(before, text(&a, 110, 24));
}

// --- confirmation and results ----------------------------------------------------------------

#[test]
fn approval_is_confirmed_with_the_incident_the_reason_and_the_effect() {
    let mut a = detail_app(PENDING);                       // review the incident first
    ch(&mut a, 'a');
    let s = text(&a, 110, 40);
    for needle in ["RECOVERY REQUEST", "inc_001", "Your model stopped answering", "workload vllm",
                   "Inference requests are failing or timing out", "Evidence", "A test request to the model did not complete in time.",
                   "Proposed", "Restart the model server", "no rollback", "allowlisted and runs only after you approve",
                   "[Enter] Approve", "[Esc] Cancel"] {
        has(&s, needle);
    }
}

#[test]
fn the_confirm_key_looks_inactive_until_the_guard_has_elapsed() {
    let mut a = detail_app(PENDING);
    ch(&mut a, 'a');
    assert_eq!(colour_of(&ui::render_to_buffer(&a, 110, 40), "[Enter] Approve"), theme::TEXT_MUTED);
    a.now += std::time::Duration::from_millis(600);
    assert_eq!(colour_of(&ui::render_to_buffer(&a, 110, 40), "[Enter] Approve"), theme::ACCENT);
}

#[test]
fn rejection_has_its_own_clearly_labelled_confirmation() {
    let mut a = detail_app(PENDING);
    ch(&mut a, 'r');
    let s = text(&a, 110, 40);
    has(&s, "DECLINE THIS RECOVERY");
    has(&s, "[Enter] Decline");
    has(&s, "No action is taken; the proposal is closed.");
    lacks(&s, "RECOVERY REQUEST");
    lacks(&s, "no rollback");
}

#[test]
fn while_the_server_works_the_operator_sees_that_it_is_working_and_what_the_server_says() {
    let mut a = detail_app(PENDING);
    ch(&mut a, 'a');
    a.now += std::time::Duration::from_millis(600);
    key(&mut a, KeyCode::Enter);
    let s = text(&a, 110, 40);
    for needle in ["Waiting for the server", "Approve inc_001 sent", "Server state", "● AWAITING APPROVAL",
                   "INFERENCE_UNRESPONSIVE", "Evidence"] {
        has(&s, needle);                       // an inline banner: the incident stays visible
    }
    lacks(&s, "In progress");                  // no blocking modal any more
}

#[test]
fn the_banner_follows_the_servers_state_and_never_predicts_it() {
    let mut a = detail_app(PENDING);
    ch(&mut a, 'a');
    a.now += std::time::Duration::from_millis(600);
    key(&mut a, KeyCode::Enter);
    for (status, shown) in [("EXECUTING", "● EXECUTING"), ("VERIFYING", "● VERIFYING")] {
        let mut inc = incident(PENDING, "inc_001");
        inc.status = status.into();
        a.apply(Msg::Poll(Ok(Snapshot::new(status_of(UNRESPONSIVE), vec![inc]))));
        let s = text(&a, 110, 40);
        has(&s, "Waiting for the server");
        has(&s, &format!("Server state  {shown}"));
    }
    a.apply(Msg::Action { kind: aiops_tui::app::ActionKind::Approve, id: "inc_001".into(),
                          result: Ok(incident(RESOLVED, "inc_001")) });
    lacks(&text(&a, 110, 40), "Waiting for the server");     // gone when the server has answered
}

#[test]
fn a_server_refusal_appears_in_the_footer_in_red() {
    let mut a = app(UNRESPONSIVE, vec![incident(PENDING, "inc_001")]);
    a.apply(Msg::Action {
        kind: aiops_tui::app::ActionKind::Approve,
        id: "inc_001".into(),
        result: Err(ApiError::Server { status: 409, code: "POLICY_DENIED".into(), message: "denied".into() }),
    });
    let s = text(&a, 130, 32);
    has(&s, "POLICY_DENIED");
    assert_eq!(colour_of(&ui::render_to_buffer(&a, 130, 32), "Approve failed"), theme::CRITICAL);
}

// --- watchdog and audit visibility --------------------------------------------------------------

fn with_watchdog(state: &str, extra: &str) -> App {
    let mut st = status(HEALTHY);
    let json = format!("{{\"state\": \"{state}\"{extra}}}");
    st.watchdog = Some(serde_json::from_str(&json).unwrap());
    app_with(Snapshot::new(st, vec![]))
}

#[test]
fn an_armed_watchdog_shows_identity_and_budget_state() {
    let s = text(&app(HEALTHY, vec![]), 110, 32);
    has(&s, "WATCHDOG");
    has(&s, "● ARMED");
}

#[test]
fn every_unavailable_watchdog_state_is_obvious() {
    for (state, extra, detail) in [
        ("stalled", "", "stalled"),
        ("stale", "", "stale"),
        ("not running", "", "not running"),
        ("aborted", ", \"reason\": [\"ram_percent\"]", "ram_percent"),
        ("unknown", ", \"error\": \"unreadable\"", "unknown"),
    ] {
        let a = with_watchdog(state, extra);
        let s = text(&a, 110, 32);
        has(&s, "! UNAVAILABLE");
        has(&s, detail);
        lacks(&s, "● ARMED");
        assert_eq!(colour_of(&ui::render_to_buffer(&a, 110, 32), "! UNAVAILABLE"), theme::CRITICAL, "{state}");
    }
}

#[test]
fn a_missing_watchdog_section_is_shown_as_unavailable_not_assumed_armed() {
    let mut st = status(HEALTHY);
    st.watchdog = None;
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    has(&s, "! UNAVAILABLE");
    lacks(&s, "● ARMED");
}

#[test]
fn a_watchdog_protecting_the_wrong_process_is_flagged() {
    let mut st = status(HEALTHY);
    st.watchdog.as_mut().unwrap().protected_pid = Some(1);
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    has(&s, "Identity ✗");
}

#[test]
fn a_sample_over_a_budget_is_flagged() {
    let mut st = status(HEALTHY);
    st.watchdog.as_mut().unwrap().last_sample.as_mut().unwrap()
        .insert("temperature_c".into(), 95.0.into());
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    has(&s, "Budgets ✗");
    has(&s, "temperature_c");
}

#[test]
fn audit_integrity_is_prominent_when_valid_and_alarming_when_not() {
    let ok = app(HEALTHY, vec![]);
    let s = text(&ok, 110, 32);
    has(&s, "AUDIT ✓ VERIFIED");
    assert_eq!(colour_of(&ui::render_to_buffer(&ok, 110, 32), "AUDIT ✓ VERIFIED"), theme::HEALTHY);

    let mut st = status(HEALTHY);
    st.audit.as_mut().unwrap().valid = false;
    let bad = app_with(Snapshot::new(st, vec![]));
    let s = text(&bad, 110, 32);
    has(&s, "AUDIT ✗ INTEGRITY FAILURE");
    has(&s, "Audit ✗");
    assert_eq!(colour_of(&ui::render_to_buffer(&bad, 110, 32), "AUDIT ✗ INTEGRITY FAILURE"), theme::CRITICAL);
}

#[test]
fn an_unreported_audit_state_is_na_not_verified() {
    let mut st = status(HEALTHY);
    st.audit = None;
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    has(&s, "AUDIT N/A");
    lacks(&s, "VERIFIED");
}

#[test]
fn the_audit_screen_lists_events_and_the_integrity_banner() {
    let mut snap = Snapshot::new(status(HEALTHY), vec![]);
    snap.audit = Some(serde_json::from_str::<Audit>(AUDIT).unwrap());
    let mut a = app_with(snap);
    ch(&mut a, '4');
    let s = text(&a, 110, 30);
    for needle in ["AUDIT ✓ VERIFIED", "incident_created", "rca_generated", "remediation_proposed",
                   "policy_evaluated", "a1b2c3d4e5f6"] {
        has(&s, needle);
    }
}

#[test]
fn the_audit_screen_does_not_hide_an_integrity_failure() {
    let mut snap = Snapshot::new(status(HEALTHY), vec![]);
    snap.audit = Some(serde_json::from_str::<Audit>(AUDIT_INVALID).unwrap());
    let mut a = app_with(snap);
    ch(&mut a, '4');
    has(&text(&a, 110, 30), "AUDIT ✗ INTEGRITY FAILURE");
}

// --- the control plane is unavailable --------------------------------------------------------------

#[test]
fn a_control_plane_that_never_answered_shows_connecting_and_no_invented_values() {
    let a = App::new("http://127.0.0.1:8080".into());
    let s = text(&a, 100, 30);
    has(&s, "CONNECTING");
    for invented in ["35.4%", "● CONTROL ONLINE", "HEALTHY", "AUDIT ✓"] {
        lacks(&s, invented);
    }
}

#[test]
fn an_offline_control_plane_is_shown_not_crashed_on() {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Err(ApiError::Offline("connection refused".into()))));
    let s = text(&a, 100, 30);
    has(&s, "CONTROL PLANE OFFLINE");
    has(&s, "Retrying");
    has(&s, "connection refused");
    assert_eq!(colour_of(&ui::render_to_buffer(&a, 100, 30), "CONTROL PLANE OFFLINE"), theme::CRITICAL);
}

#[test]
fn when_the_control_plane_drops_the_last_values_are_kept_but_marked_stale() {
    let mut a = app(HEALTHY, vec![]);
    a.apply(Msg::Poll(Err(ApiError::Timeout)));
    let s = text(&a, 110, 32);
    has(&s, "CONTROL PLANE OFFLINE");
    has(&s, "STALE");
    has(&s, "35.4%");                  // the last known value, clearly labelled as old
    lacks(&s, "● CONTROL ONLINE");
}

#[test]
fn old_telemetry_from_a_running_control_plane_is_flagged() {
    let mut a = app(HEALTHY, vec![]);
    a.wall = T + 90.0;
    has(&text(&a, 110, 32), "telemetry stale");
}

#[test]
fn a_slow_api_is_called_out() {
    let mut snap = Snapshot::new(status(HEALTHY), vec![]);
    snap.poll_ms = 2600;
    has(&text(&app_with(snap), 110, 32), "SLOW API");
}

#[test]
fn a_tiny_terminal_degrades_to_a_message_instead_of_panicking() {
    let a = app(HEALTHY, vec![]);
    for (w, h) in [(10, 3), (40, 10), (1, 1), (0, 0), (200, 5)] {
        let s = text(&a, w, h);
        if w >= 20 && h >= 3 {
            has(&s, "too small");
        }
    }
}

#[test]
fn rendering_every_screen_at_common_sizes_never_panics() {
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001"), incident(RESOLVED, "inc_002")]);
    snap.detail = Some(incident(RESOLVED, "inc_002"));
    snap.audit = Some(serde_json::from_str(AUDIT).unwrap());
    for (w, h) in [(80, 24), (100, 30), (120, 40), (200, 60), (60, 20)] {
        let mut a = app_with(snap.clone());
        for _ in 0..4 {
            text(&a, w, h);
            key(&mut a, KeyCode::Tab);
        }
        key(&mut a, KeyCode::Char('1') );
        key(&mut a, KeyCode::Enter);
        text(&a, w, h);
        ch(&mut a, 'a');
        text(&a, w, h);
    }
}

#[test]
fn a_gpu_the_profile_does_not_report_is_unknown_not_a_failure() {
    let mut st = status(HEALTHY);
    let o = st.last_observation.as_mut().unwrap();
    for k in ["gpu_uuid", "gpu_memory_total_bytes", "gpu_temperature_c", "gpu_utilization_percent"] {
        o.remove(k);
    }
    let s = text(&app_with(Snapshot::new(st, vec![])), 110, 32);
    has(&s, "GPU N/A");           // unknown, shown as unknown ...
    lacks(&s, "GPU ✗");           // ... never as a failure
    lacks(&s, "! UNAVAILABLE");
}

// --- real-terminal findings: narrow footer, long evidence labels, stale provenance -------------

#[test]
fn the_footer_never_drops_quit_even_in_a_narrow_terminal() {
    let a = app(HEALTHY, vec![]);
    for w in [72, 76, 90] {
        let s = text(&a, w, 24);
        let footer = s.lines().last().unwrap();
        assert!(footer.contains("Q Quit"), "no Quit at {w}: {footer:?}");
        assert!(footer.chars().count() <= w as usize);
    }
}

#[test]
fn a_and_r_are_visibly_inactive_unless_the_selected_incident_awaits_a_decision() {
    let key_colour = |a: &App, k: &str| colour_of(&ui::render_to_buffer(a, 110, 32), k);
    let mut waiting = app(UNRESPONSIVE, vec![incident(PENDING, "inc_001")]);
    // only the keys that work on a screen are listed: the lists do not offer Approve at all
    assert!(!text(&waiting, 110, 32).lines().last().unwrap().contains("Approve"), "not offered on the list screens");
    key(&mut waiting, KeyCode::Enter);                 // on the incident's own page it is live
    let mut resolved = app(HEALTHY, vec![incident(RESOLVED, "inc_001")]);
    key(&mut resolved, KeyCode::Enter);
    assert_ne!(key_colour(&waiting, "A Approve"), theme::TEXT_MUTED);
    assert_eq!(key_colour(&resolved, "A Approve"), theme::TEXT_MUTED);
    assert_eq!(key_colour(&resolved, "R Reject"), theme::TEXT_MUTED);
}

#[test]
fn a_long_evidence_label_never_runs_into_its_value() {
    let mut inc = incident(PENDING, "inc_001");
    inc.evidence[0].metric = "vllm_allocation_failure_count".into();
    inc.evidence[0].value = serde_json::json!(5);
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = app_with(snap);
    key(&mut a, KeyCode::Enter);
    has(&text(&a, 110, 60), "vllm allocation failure count  5");
}

#[test]
fn a_dropped_connection_labels_the_observation_as_stale() {
    let mut a = app(HEALTHY, vec![]);
    a.apply(Msg::Poll(Err(ApiError::Timeout)));
    has(&text(&a, 110, 32), "Stale · observed");
}
