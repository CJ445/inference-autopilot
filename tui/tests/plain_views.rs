//! The default (plain) views: what a person sees first, and what must NOT be in them.
use aiops_tui::api::ApiError;
use aiops_tui::app::{App, Msg, Snapshot};
use aiops_tui::model::{AuditEvent, Incident, Status};
use aiops_tui::{theme, ui};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");
const UNRESPONSIVE: &str = include_str!("fixtures/status_unresponsive.json");
const PENDING: &str = include_str!("fixtures/incident_pending.json");
const RESOLVED: &str = include_str!("fixtures/incident_resolved.json");
const UNRESOLVED: &str = include_str!("fixtures/incident_unresolved.json");
const INSUFFICIENT: &str = include_str!("fixtures/incident_insufficient.json");
const T: f64 = 1_790_866_931.0;

fn key(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
}
fn code(k: KeyCode) -> KeyEvent {
    KeyEvent::new(k, KeyModifiers::NONE)
}
fn incident(json: &str, id: &str) -> Incident {
    let mut i: Incident = serde_json::from_str(json).unwrap();
    i.incident_id = id.into();
    i
}
fn status(json: &str) -> Status {
    serde_json::from_str(json).unwrap()
}
fn app(st: Status, incidents: Vec<Incident>) -> App {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = T + 1.25;
    a.apply(Msg::Poll(Ok(Snapshot::new(st, incidents))));
    a
}
fn detail_app(json: &str) -> App {
    let inc = incident(json, "inc_001");
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = T + 1.25;
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(code(KeyCode::Enter));
    a
}
fn screen(a: &App) -> String {
    ui::render_to_string(a, 120, 56)
}
fn has(s: &str, needle: &str) {
    assert!(s.contains(needle), "missing {needle:?} in:\n{s}");
}
fn lacks(s: &str, needle: &str) {
    assert!(!s.contains(needle), "unexpected {needle:?} in:\n{s}");
}
const TECHNICAL: [&str; 14] = [
    "INFERENCE_UNRESPONSIVE", "GPU_MEMORY_PRESSURE", "restart_workload", "POLICY_CHECK", "Probe ✓",
    "KV cache", "Tokens/s", "Req/min", "vllm-probe", "nvidia-smi", "GPU-1e5dd8d1", "Deterministic RCA",
    "APPROVAL REQUIRED", "workload=vllm",
];

fn assert_no_jargon(s: &str) {
    for t in TECHNICAL {
        lacks(s, t);
    }
}

// --- overview ----------------------------------------------------------------------------------

#[test]
fn a_healthy_home_answers_what_is_running_is_it_healthy_is_autopilot_watching_and_what_next() {
    let s = screen(&app(status(HEALTHY), vec![]));
    for needle in [
        // what is running, and is it healthy
        "Inference", "vllm", "✓ HEALTHY", "facebook/opt-125m", "Probe 18 ms", "Memory 35% of 8.0 GiB", "Temp 59°C", "Busy 23%",
        // is Autopilot watching
        "Autopilot", "✓ WATCHING", "Last observation", "No active incidents.",
        "✓ Observe › ○ Detect › ○ Diagnose › ○ Propose › ○ Approve › ○ Recover › ○ Verify",
        // what can I do next
        "Want to see the recovery loop?", "[R] Run a recovery test", "[I] View incidents", "[A] View activity",
    ] {
        has(&s, needle);
    }
    assert_no_jargon(&s);
    for gone in ["Metrics", "Queue", "Thermals", "VRAM", "AUDIT ✓ VERIFIED", "Identity", "Everything is working"] {
        lacks(&s, gone);
    }
}

#[test]
fn d_swaps_between_the_plain_and_the_technical_view_and_back() {
    let mut a = app(status(HEALTHY), vec![]);
    has(&screen(&a), "✓ WATCHING");
    a.handle_key(key('d'));
    let tech = screen(&a);
    for needle in ["Probe ✓", "Metrics ✓", "KV cache", "GPU 0 · GPU-1e5dd8d1", "AUDIT ✓ VERIFIED", "● HEALTHY", "docker-real-gpu"] {
        has(&tech, needle);
    }
    lacks(&tech, "✓ WATCHING");
    a.handle_key(key('D'));
    has(&screen(&a), "✓ WATCHING");
    // and the footer says which way it goes
    has(ui::render_to_string(&a, 160, 40).lines().last().unwrap(), "D Details");
    a.handle_key(key('d'));
    has(ui::render_to_string(&a, 160, 40).lines().last().unwrap(), "D Simple view");
}

#[test]
fn each_situation_has_one_honest_state_word_and_a_glyph() {
    // a problem waiting for a decision
    let waiting = app(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001")]);
    let s = screen(&waiting);
    has(&s, "! NEEDS YOUR OK");
    has(&s, "Your model stopped answering");
    has(&s, "→ Approve");
    // a problem being worked on
    let mut working = incident(PENDING, "inc_001");
    working.status = "VERIFYING".into();
    let s = screen(&app(status(UNRESPONSIVE), vec![working]));
    has(&s, "→ VERIFYING");
    has(&s, "✓ Recover › → Verify");
    // the model not answering but no incident yet (the detector needs two failures in a row)
    let mut st = status(UNRESPONSIVE);
    st.last_observation.as_mut().unwrap().insert("inference_probe_ok".into(), false.into());
    let s = screen(&app(st, vec![]));
    has(&s, "✗ NOT ANSWERING");
    has(&s, "✓ WATCHING");
    lacks(&s, "Probe 3004 ms");                              // a failed probe's timeout is not a measurement
    // no first reading
    let mut st = status(HEALTHY);
    st.last_observation = None;
    let s = screen(&app(st, vec![]));
    has(&s, "○ NO READING YET");
    has(&s, "N/A");
}

#[test]
fn the_state_colours_follow_the_situation_and_never_rely_on_colour_alone() {
    let colour_of = |a: &App, needle: &str| {
        let buf = ui::render_to_buffer(a, 120, 56);
        let w = buf.area.width as usize;
        for row in 0..buf.area.height as usize {
            let line: String = (0..w).map(|x| buf.content()[row * w + x].symbol().to_string()).collect();
            if let Some(b) = line.find(needle) {
                return buf.content()[row * w + line[..b].chars().count()].fg;
            }
        }
        panic!("{needle} not on screen")
    };
    assert_eq!(colour_of(&app(status(HEALTHY), vec![]), "✓ HEALTHY"), theme::HEALTHY);
    assert_eq!(colour_of(&app(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001")]), "! NEEDS YOUR OK"), theme::WARNING);
    let mut st = status(UNRESPONSIVE);
    st.last_observation.as_mut().unwrap().insert("inference_probe_ok".into(), false.into());
    assert_eq!(colour_of(&app(st, vec![]), "✗ NOT ANSWERING"), theme::CRITICAL);
}

#[test]
fn no_workload_is_a_valid_calm_state_with_a_truthful_explanation() {
    for (state, headline, text) in [
        ("absent", "NO WORKLOAD CONNECTED", "no managed inference"),
        ("stopped", "WORKLOAD STOPPED", "Autopilot will not start it"),
    ] {
        let mut st = status(HEALTHY);
        st.workload = Some(serde_json::from_value(serde_json::json!({"name": "vllm", "state": state})).unwrap());
        st.last_observation = None;
        st.last_observed_at = None;
        let s = screen(&app(st, vec![]));
        has(&s, headline);
        has(&s, text);
        has(&s, "✓ WATCHING");
        lacks(&s, "NOT ANSWERING");
        lacks(&s, "✗");
        assert_no_jargon(&s);
    }
    let mut st = status(HEALTHY);
    st.workload = Some(serde_json::from_value(serde_json::json!({"name": "vllm", "state": "absent"})).unwrap());
    st.last_observation = None;
    let s = screen(&app(st, vec![]));
    has(&s, "workload is currently available.");
    has(&s, "Autopilot will not create or start one automatically.");
    has(&s, "Go to System (5) for diagnostics.");
    let mut st = status(HEALTHY);
    st.workload = Some(serde_json::from_value(serde_json::json!({"name": "vllm", "state": "starting"})).unwrap());
    let s = screen(&app(st, vec![]));
    has(&s, "→ STARTING");
}

#[test]
fn unavailable_values_are_na_never_invented() {
    let mut st = status(HEALTHY);
    let o = st.last_observation.as_mut().unwrap();
    for k in ["gpu_memory_used_bytes", "gpu_memory_total_bytes", "gpu_temperature_c", "inference_probe_latency_ms"] {
        o.remove(k);
    }
    let s = screen(&app(st, vec![]));
    has(&s, "Memory N/A");
    has(&s, "Temp N/A");
    has(&s, "Probe N/A");                                    // latency unknown: not "0 ms"
    lacks(&s, "0%");
    // there is no p95 anywhere: only what the server reports
    lacks(&s, "p95");
}

#[test]
fn an_unreachable_control_plane_is_said_plainly() {
    let mut a = app(status(HEALTHY), vec![]);
    a.apply(Msg::Poll(Err(ApiError::Timeout)));
    let s = screen(&a);
    has(&s, "CONTROL PLANE OFFLINE");
    has(&s, "✗ NOT WATCHING");
    lacks(&s, "✓ WATCHING");
    has(&s, "Stale · last values observed");
}

#[test]
fn incidents_on_home_are_one_line_each_with_state_word_glyph_and_age() {
    let a = app(status(HEALTHY), vec![incident(RESOLVED, "inc_001")]);
    let s = screen(&a);
    has(&s, "✓ RESOLVED");
    has(&s, "Your model stopped answering");
    has(&s, "inc_001 · Recovered");
    has(&s, "✓ WATCHING");                                    // a closed incident does not stop it watching
    let mut b = app(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001")]);
    has(&screen(&b), "› ! NEEDS YOUR OK");
    has(&screen(&b), "Enter  Review the selected incident");
    b.handle_key(code(KeyCode::Enter));
    has(&screen(&b), "What happened");                       // Enter opens the incident
}

// --- incidents list -----------------------------------------------------------------------------

#[test]
fn the_incident_list_reads_as_operations_what_why_what_action_and_whether_it_needs_you() {
    let mut a = app(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001"), incident(RESOLVED, "inc_000")]);
    a.handle_key(key('2'));
    let s = screen(&a);
    for needle in ["› ! NEEDS YOUR OK", "Your model stopped answering", "inc_001 · Detected",
                   "Diagnosis: Inference requests are failing or timing out",
                   "Action: Restart the model server · Approval: REQUIRED",
                   "✓ RESOLVED", "inc_000 · Recovered", "Enter  Review the selected incident"] {
        has(&s, needle);
    }
    assert_no_jargon(&s);
    for old in ["ID  ", "PROBLEM", "STATUS", "STARTED"] {
        lacks(&s, old);                                       // not a database table any more
    }
    let narrow = ui::render_to_string(&a, 72, 30);
    has(&narrow, "! NEEDS YOUR OK");
    has(&narrow, "Your model stopped answering");
}

#[test]
fn an_empty_incident_list_is_useful_not_a_dead_end() {
    let mut a = app(status(HEALTHY), vec![]);
    a.handle_key(key('2'));
    let s = screen(&a);
    for needle in ["NO ACTIVE INCIDENTS", "Autopilot is watching the inference workload.",
                   "Want to see the recovery loop?", "[R] Run a recovery test"] {
        has(&s, needle);
    }
    // with nothing to watch it does not claim to be watching
    let mut st = status(HEALTHY);
    st.workload = Some(serde_json::from_value(serde_json::json!({"name": "vllm", "state": "absent"})).unwrap());
    st.last_observation = None;
    let mut b = app(st, vec![]);
    b.handle_key(key('2'));
    let s = screen(&b);
    has(&s, "There is no managed workload to watch right now.");
    lacks(&s, "Autopilot is watching the inference workload.");
}

// --- the incident page ----------------------------------------------------------------------------

#[test]
fn a_pending_incident_reads_as_what_happened_what_we_found_and_what_to_do() {
    let s = screen(&detail_app(PENDING));
    for needle in [
        "Your model stopped answering", "● Needs your OK", "vllm · inc_001 · started 15:02:11",
        "What happened", "The model stopped responding to test requests.",
        "What we found", "Inference requests are failing or timing out",
        "A test request to the model did not complete in time.", "The model's metrics are not available.",
        "GPU memory in use: 2.8 GiB.",
        "Recommended action", "Restart the model server",
        "The model is unavailable while it restarts. There is no rollback.",
        "[ A ] Approve", "[ R ] Reject",
        "Recovery check", "the server checks that the model answers again",
        "Step by step", "15:02:11  Problem detected and investigated", "15:02:12  Fix proposed",
        "D  Technical details",
    ] {
        has(&s, needle);
    }
    assert_no_jargon(&s);
    for gone in ["Evidence", "Policy", "Classification", "POLICY_CHECK", "DETECTED", "TRIAGING"] {
        lacks(&s, gone);
    }
}

#[test]
fn the_decision_buttons_wait_for_the_full_incident_because_the_keys_do_nothing_before_it() {
    // opened from the list, the detail has not arrived yet
    let mut a = app(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001")]);
    a.handle_key(code(KeyCode::Enter));
    let s = screen(&a);
    has(&s, "Needs your OK");
    has(&s, "Loading the full details…");
    lacks(&s, "[ A ] Approve");
    // and the key is refused with the reason, not silently
    a.handle_key(key('a'));
    has(&screen(&a), "still loading this incident's details");
    // once it has arrived the buttons appear
    has(&screen(&detail_app(PENDING)), "[ A ] Approve");
}

#[test]
fn the_recovery_check_names_a_count_only_when_the_server_gave_one() {
    let mut inc = incident(PENDING, "inc_001");
    inc.verification = Some(serde_json::from_value(serde_json::json!({"checks": null, "required_completions": 3})).unwrap());
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(code(KeyCode::Enter));
    let s = screen(&a);
    has(&s, "must answer 3 test requests in a row");
    let unknown = screen(&detail_app(PENDING));
    lacks(&unknown, "in a row");                              // no number the TUI made up
}

#[test]
fn the_pipeline_is_the_same_seven_stages_on_every_screen() {
    let waiting = "✓ Observe › ✓ Detect › ✓ Diagnose › ✓ Propose › → Approve › ○ Recover › ○ Verify";
    has(&screen(&detail_app(PENDING)), waiting);                       // the incident page
    has(&screen(&app(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001")])), waiting);   // Home
    let mut lab = app(status(UNRESPONSIVE), vec![incident(PENDING, "inc_001")]);
    lab.handle_key(key('3'));
    has(&screen(&lab), "✓ Observe › ○ Detect");                       // the idle Lab: nothing to detect yet
    has(&screen(&detail_app(RESOLVED)), "✓ Observe › ✓ Detect › ✓ Diagnose › ✓ Propose › ✓ Approve › ✓ Recover › ✓ Verify");
    has(&screen(&detail_app(UNRESOLVED)), "✓ Recover › ✗ Verify");
    lacks(&screen(&detail_app(PENDING)), "Policy");
}

#[test]
fn a_resolved_incident_lists_what_was_checked_in_plain_words() {
    let s = screen(&detail_app(RESOLVED));
    for needle in ["✓ The model server was restarted", "✓ The GPU can be read", "✓ The model's metrics can be read",
                   "✓ The model answers test requests again", "Resolved", "15:02:18  You approved the fix",
                   "15:02:18  Fix started", "15:02:22  Checking recovery", "15:02:47  Resolved"] {
        has(&s, needle);
    }
    lacks(&s, "[ A ] Approve");
    assert_no_jargon(&s);
}

#[test]
fn a_failed_check_is_shown_as_failed_and_the_result_is_not_resolved() {
    let s = screen(&detail_app(UNRESOLVED));
    has(&s, "✗ The model's metrics can be read");
    has(&s, "✗ The model answers test requests again");
    has(&s, "✓ The model server was restarted");
    has(&s, "Not resolved");
    lacks(&s, "Resolved\n");
}

#[test]
fn resolved_is_still_never_claimed_unless_the_server_says_so() {
    let mut inc = incident(RESOLVED, "inc_001");
    inc.status = "VERIFYING".into();
    inc.timeline.pop();
    inc.verification = None;
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(code(KeyCode::Enter));
    let s = screen(&a);
    has(&s, "● Checking recovery");
    has(&s, "In progress: waiting for the server's result.");
    lacks(&s, "Resolved");
}

#[test]
fn insufficient_evidence_is_said_plainly_and_offers_no_decision() {
    let s = screen(&detail_app(INSUFFICIENT));
    has(&s, "Not enough evidence to name a cause.");
    has(&s, "No fix is proposed.");
    lacks(&s, "[ A ] Approve");
    lacks(&s, "Needs your OK");
}

#[test]
fn a_memory_incident_never_claims_the_gpu_ran_out_of_memory() {
    let mut inc = incident(PENDING, "inc_001");
    inc.category = "GPU_MEMORY_PRESSURE".into();
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(code(KeyCode::Enter));
    let s = screen(&a);
    has(&s, "GPU memory is above its limit");
    has(&s, "GPU memory use went above the configured limit.");
    lacks(&s, "out of memory");
    lacks(&s, "running out");
}

#[test]
fn an_unknown_category_keeps_its_own_name_and_the_server_text() {
    let mut inc = incident(PENDING, "inc_001");
    inc.category = "DISK_FULL".into();
    let mut snap = Snapshot::new(status(UNRESPONSIVE), vec![inc.clone()]);
    snap.detail = Some(inc);
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(code(KeyCode::Enter));
    let s = screen(&a);
    has(&s, "Disk full");                                     // humanised, not invented
    has(&s, "Inference requests are failing or timing out");  // the server's own RCA text, verbatim
}

#[test]
fn d_on_the_incident_page_shows_the_technical_page_and_back() {
    let mut a = detail_app(PENDING);
    a.handle_key(key('d'));
    let tech = screen(&a);
    for needle in ["INFERENCE_UNRESPONSIVE", "Deterministic RCA", "Classification", "vllm-probe", "restart_workload",
                   "POLICY_CHECK"] {
        has(&tech, needle);
    }
    a.handle_key(key('d'));
    has(&screen(&a), "What happened");
}

// --- activity ---------------------------------------------------------------------------------------

fn with_audit(events: Vec<serde_json::Value>, valid: bool) -> App {
    let mut a = app(status(HEALTHY), vec![]);
    let mut snap = Snapshot::new(status(HEALTHY), vec![]);
    let evs: Vec<AuditEvent> = events.into_iter().map(|e| serde_json::from_value(e).unwrap()).collect();
    snap.audit = Some(serde_json::from_value(serde_json::json!({"valid": valid, "total": evs.len(), "events": evs.iter().map(|e| serde_json::json!({"seq": e.seq, "event": e.event, "data": e.data, "hash": e.hash})).collect::<Vec<_>>()})).unwrap());
    a.apply(Msg::Poll(Ok(snap)));
    a.handle_key(key('4'));
    a
}

#[test]
fn activity_reads_as_what_happened() {
    let a = with_audit(vec![
        serde_json::json!({"seq": 0, "event": "incident_created", "data": {"incident_id": "inc_001"}, "hash": "a1"}),
        serde_json::json!({"seq": 1, "event": "remediation_proposed", "data": {"action": "restart_workload"}, "hash": "a2"}),
        serde_json::json!({"seq": 2, "event": "approval_granted", "data": {"incident_id": "inc_001"}, "hash": "a3"}),
        serde_json::json!({"seq": 3, "event": "verification_finished", "data": {"checks": {"x": true}}, "hash": "a4"}),
        serde_json::json!({"seq": 4, "event": "something_new", "data": {}, "hash": "a5"}),
    ], true);
    let s = screen(&a);
    for needle in ["Activity log intact", "What happened", "Problem detected (inc_001)",
                   "Fix proposed: Restart the model server", "You approved the fix (inc_001)",
                   "Recovery verified", "something_new", "D  Technical details"] {
        has(&s, needle);
    }
    for gone in ["incident_created", "remediation_proposed", "a1", "{\"incident_id\""] {
        lacks(&s, gone);
    }
}

#[test]
fn activity_with_nothing_yet_and_with_a_broken_chain() {
    let empty = screen(&with_audit(vec![], true));
    has(&empty, "NO RECENT ACTIVITY");
    has(&empty, "Autopilot has not recorded any events yet.");
    has(&empty, "[R] Run a recovery test");
    let broken = screen(&with_audit(vec![serde_json::json!({"seq": 0, "event": "incident_created", "data": {}, "hash": "h"})], false));
    has(&broken, "ACTIVITY LOG INTEGRITY FAILURE");
    lacks(&broken, "Activity log intact");
}

#[test]
fn d_on_activity_shows_the_raw_events_and_hashes() {
    let mut a = with_audit(vec![
        serde_json::json!({"seq": 0, "event": "incident_created", "data": {"incident_id": "inc_001"}, "hash": "a1b2c3d4e5f6"}),
    ], true);
    a.handle_key(key('d'));
    let s = screen(&a);
    has(&s, "incident_created");
    has(&s, "a1b2c3d4e5f6");
    has(&s, "AUDIT ✓ VERIFIED");
}

// --- sizes ---------------------------------------------------------------------------------------------

#[test]
fn every_plain_screen_renders_at_common_sizes_without_panicking() {
    let mut a = detail_app(PENDING);
    for (w, h) in [(72, 18), (80, 24), (120, 40), (200, 60)] {
        for k in "1234?".chars() {
            a.handle_key(key(k));
            ui::render_to_string(&a, w, h);
        }
        a.handle_key(key('1'));
        a.handle_key(code(KeyCode::Enter));
        ui::render_to_string(&a, w, h);
        a.handle_key(key('d'));
        ui::render_to_string(&a, w, h);
        a.handle_key(key('d'));
        a.handle_key(code(KeyCode::Esc));
    }
}

// --- the mode badge ---------------------------------------------------------------------------------

fn header_row(a: &App) -> String {
    ui::render_to_string(a, 120, 30).lines().next().unwrap().to_string()
}
fn badge_cell(a: &App, text: &str) -> ratatui::style::Modifier {
    let buf = ui::render_to_buffer(a, 120, 30);
    let w = buf.area.width as usize;
    let line: String = (0..w).map(|x| buf.content()[x].symbol().to_string()).collect();
    let at = line.chars().position(|_| true).map(|_| line.find(text).unwrap()).unwrap();
    buf.content()[line[..at].chars().count()].modifier
}

#[test]
fn the_header_always_says_which_world_this_is_in_words_and_in_reverse_video() {
    let real = app(status(HEALTHY), vec![]);
    assert!(header_row(&real).contains("LIVE · GPU-REAL"), "{}", header_row(&real));
    assert!(badge_cell(&real, "LIVE").contains(ratatui::style::Modifier::REVERSED), "not colour alone");
    // GPU-REAL only when a GPU reading from the real telemetry is present; otherwise plain LIVE
    let mut st = status(HEALTHY);
    st.last_observation.as_mut().unwrap().remove("gpu_uuid");
    let row = header_row(&app(st, vec![]));
    assert!(row.contains("LIVE") && !row.contains("GPU-REAL"), "{row}");
    let mut st = status(HEALTHY);
    st.last_observation = None;
    let row = header_row(&app(st, vec![]));
    assert!(row.contains("LIVE") && !row.contains("REAL"), "{row}");
    // a simulated session says SIMULATION, never LIVE
    let mut st = status(HEALTHY);
    st.mode = Some("SIMULATION".into());
    let sim = app(st, vec![]);
    let row = header_row(&sim);
    assert!(row.contains("SIMULATION") && !row.contains("LIVE"), "{row}");
    assert!(badge_cell(&sim, "SIMULATION").contains(ratatui::style::Modifier::REVERSED));
}
