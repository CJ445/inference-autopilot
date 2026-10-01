//! Practice (SIMULATION) in the TUI: how it is entered and left, that it can never be mistaken for
//! the real system, and that nothing from one namespace leaks into the other.
use aiops_tui::api::ApiError;
use aiops_tui::app::{ActionKind, App, Command, Conn, Effect, Loaded, Msg, Screen, Snapshot};
use aiops_tui::model::{Incident, Status};
use aiops_tui::{theme, ui};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::time::Duration;

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");
const PENDING: &str = include_str!("fixtures/incident_pending.json");

fn key(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
}
fn ctrl(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
}
fn code(k: KeyCode) -> KeyEvent {
    KeyEvent::new(k, KeyModifiers::NONE)
}

/// What the server sends for a practice session at `stage` (no watchdog: nothing real to report).
fn practice_status(stage: &str) -> Status {
    let mut v: serde_json::Value = serde_json::from_str(HEALTHY).unwrap();
    v["mode"] = "SIMULATION".into();
    v["practice"] = serde_json::json!({ "stage": stage });
    v["info"] = serde_json::json!({"profile": "practice", "provider": "simulation",
                                   "workload": "sim-vllm", "model": "sim/opt-125m"});
    v["workload"] = serde_json::json!({"name": "sim-vllm", "state": "running"});
    v.as_object_mut().unwrap().remove("watchdog");
    serde_json::from_value(v).unwrap()
}

fn practice_snapshot(stage: &str, incidents: Vec<Incident>) -> Snapshot {
    let mut s = Snapshot::new(practice_status(stage), incidents);
    s.practice = true;
    s
}

fn real_app() -> App {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = 1_790_866_932.0;
    a.apply(Msg::Poll(Ok(Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![]))));
    a
}

/// An app that has entered practice and received a poll for it.
fn practicing(stage: &str) -> App {
    let mut a = real_app();
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    a.apply(Msg::Poll(Ok(practice_snapshot(stage, vec![]))));
    a
}

fn pending_incident() -> Incident {
    let mut i: Incident = serde_json::from_str(PENDING).unwrap();
    i.service = Some("vllm".into());
    i
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

// --- entering and leaving ----------------------------------------------------------------------

#[test]
fn p_asks_the_server_for_a_practice_session_and_only_when_it_is_reachable() {
    let mut a = real_app();
    assert_eq!(a.handle_key(key('p')), vec![Effect::StartPractice]);
    assert_eq!(a.handle_key(key('P')), vec![Effect::StartPractice]);
    assert!(!a.practice, "nothing changes until the server has answered");

    let mut off = App::new("http://127.0.0.1:8080".into());
    off.apply(Msg::Poll(Err(ApiError::Offline("refused".into()))));
    assert!(off.handle_key(key('p')).is_empty());
    assert!(off.active_notice().unwrap().is_error);
}

#[test]
fn a_started_practice_clears_the_real_view_and_polls_the_practice_namespace() {
    let mut a = real_app();
    assert!(!a.interest().practice);
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    assert!(a.practice && a.snapshot.is_none() && a.screen == Screen::Dashboard);
    assert!(a.interest().practice);
    has(&screen(&a), "Preparing the practice session");
    has(&screen(&a), "PRACTICE · SIMULATION");
    lacks(&screen(&a), "35.4%");                       // none of the real values stays on screen
}

#[test]
fn a_failed_start_leaves_the_real_view_alone() {
    let mut a = real_app();
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Err(ApiError::Timeout))));
    assert!(!a.practice && a.snapshot.is_some());
    assert!(a.active_notice().unwrap().text.contains("could not start practice"));
}

#[test]
fn esc_on_the_overview_leaves_practice_and_does_nothing_otherwise() {
    let mut a = practicing("healthy");
    assert_eq!(a.handle_key(code(KeyCode::Esc)), vec![Effect::EndPractice]);
    a.apply(Msg::Loaded(Loaded::PracticeStopped(Ok(()))));
    assert!(!a.practice && a.snapshot.is_none() && a.interest().practice == false);

    let mut real = real_app();
    assert!(real.handle_key(code(KeyCode::Esc)).is_empty());          // the real overview: nothing

    let mut p = practicing("healthy");
    p.handle_key(key('4'));                                           // another screen: Esc goes back
    assert!(p.handle_key(code(KeyCode::Esc)).is_empty() && p.screen == Screen::Dashboard && p.practice);
}

#[test]
fn leaving_practice_is_local_even_if_the_server_does_not_confirm() {
    let mut a = practicing("healthy");
    a.apply(Msg::Loaded(Loaded::PracticeStopped(Err(ApiError::Timeout))));
    assert!(!a.practice && a.snapshot.is_none());
    assert!(a.active_notice().unwrap().is_error);
}

#[test]
fn a_server_that_lost_the_session_returns_the_operator_to_the_real_system() {
    let mut a = practicing("healthy");
    a.apply(Msg::Poll(Err(ApiError::Server {
        status: 409, code: "NO_PRACTICE".into(), message: "no practice session is running".into(),
    })));
    assert!(!a.practice && matches!(a.conn, Conn::Online));
    assert!(a.active_notice().unwrap().text.contains("practice session ended"));
}

// --- the fault ----------------------------------------------------------------------------------

#[test]
fn f_breaks_the_simulated_model_only_while_practicing_and_only_when_it_is_healthy() {
    let mut real = real_app();
    assert!(real.handle_key(key('f')).is_empty() && real.active_notice().is_none(), "F means nothing for real");

    let mut a = practicing("healthy");
    assert_eq!(a.handle_key(key('f')), vec![Effect::InjectFault]);
    for stage in ["detecting", "awaiting_approval", "recovering", "resolved"] {
        let mut b = practicing(stage);
        assert!(b.handle_key(key('F')).is_empty(), "{stage}");
        assert!(b.active_notice().unwrap().text.contains("only be injected while the model is healthy"));
    }
    let mut pending = real_app();
    pending.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));      // no snapshot yet
    assert!(pending.handle_key(key('f')).is_empty());
}

// --- the two namespaces never mix -----------------------------------------------------------------

#[test]
fn a_poll_for_the_other_namespace_is_discarded() {
    // a real poll that was in flight when practice started
    let mut a = real_app();
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    a.apply(Msg::Poll(Ok(Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![]))));
    assert!(a.snapshot.is_none(), "real data must never appear under the practice banner");
    // a practice poll that arrives after leaving practice
    let mut b = practicing("healthy");
    b.apply(Msg::Loaded(Loaded::PracticeStopped(Ok(()))));
    b.apply(Msg::Poll(Ok(practice_snapshot("healthy", vec![]))));
    assert!(b.snapshot.is_none(), "practice data must never appear as the real system");
    // and the matching namespace is accepted
    b.apply(Msg::Poll(Ok(Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![]))));
    assert!(b.snapshot.is_some());
}

#[test]
fn a_decision_in_practice_is_marked_as_simulated_and_a_real_one_is_not() {
    let decide = |practice: bool| {
        let mut a = real_app();
        if practice {
            a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
        }
        let inc = pending_incident();
        let mut snap = if practice { practice_snapshot("awaiting_approval", vec![inc.clone()]) }
                       else { Snapshot::new(serde_json::from_str(HEALTHY).unwrap(), vec![inc.clone()]) };
        snap.detail = Some(inc);
        a.apply(Msg::Poll(Ok(snap)));
        a.handle_key(code(KeyCode::Enter));
        a.handle_key(key('a'));
        a
    };
    let sim = decide(true);
    assert!(sim.confirm.as_ref().unwrap().practice);
    let s = screen(&sim);
    for needle in ["Approve: Restart the model server?", "PRACTICE · SIMULATION", "nothing real is touched",
                   "A simulated restart: nothing real is restarted."] {
        has(&s, needle);
    }
    let real = decide(false);
    assert!(!real.confirm.as_ref().unwrap().practice);
    let r = screen(&real);
    has(&r, "no rollback");
    lacks(&r, "SIMULATION");
    // the decision still waits for the guard, in practice too
    let mut sim = sim;
    assert!(sim.handle_key(code(KeyCode::Enter)).is_empty());
    sim.now += Duration::from_millis(600);
    assert_eq!(sim.handle_key(code(KeyCode::Enter)), vec![Effect::Approve("inc_001".into())]);
    let _ = ActionKind::Approve;
}

// --- the palette ------------------------------------------------------------------------------------

#[test]
fn the_palette_offers_practice_and_only_offers_exit_while_practicing() {
    let mut a = real_app();
    a.handle_key(ctrl('p'));
    let words = |a: &App| -> Vec<Command> { a.palette.as_ref().unwrap().matches(a.offer()) };
    assert!(words(&a).contains(&Command::Practice) && !words(&a).contains(&Command::ExitPractice));
    has(&screen(&a), "Practice an incident");
    lacks(&screen(&a), "Exit practice");
    for c in "practice".chars() {
        a.handle_key(key(c));
    }
    assert_eq!(words(&a), vec![Command::Practice]);
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::StartPractice]);

    let mut p = practicing("healthy");
    p.handle_key(ctrl('p'));
    assert!(words(&p).contains(&Command::ExitPractice));
    has(&screen(&p), "Exit practice");
    for c in "exit".chars() {
        p.handle_key(key(c));
    }
    assert_eq!(p.handle_key(code(KeyCode::Enter)), vec![Effect::EndPractice]);
}

// --- what the operator sees --------------------------------------------------------------------------

/// The banner's colour and whether it is reverse video (a solid bar that does not rest on colour alone).
fn bg_of_banner(buf: &ratatui::buffer::Buffer) -> Option<(ratatui::style::Color, bool)> {
    let w = buf.area.width as usize;
    let cells = buf.content();
    for row in 0..buf.area.height as usize {
        let line: String = (0..w).map(|x| cells[row * w + x].symbol().to_string()).collect();
        if let Some(b) = line.find("PRACTICE · SIMULATION") {
            let cell = &cells[row * w + line[..b].chars().count()];
            return Some((cell.fg, cell.modifier.contains(ratatui::style::Modifier::REVERSED)));
        }
    }
    None
}

#[test]
fn the_banner_is_on_every_screen_in_practice_and_never_otherwise() {
    let mut a = practicing("healthy");
    for n in "1234567?".chars() {
        a.handle_key(key(n));
        let s = screen(&a);
        has(&s, "PRACTICE · SIMULATION");
        assert_eq!(bg_of_banner(&ui::render_to_buffer(&a, 120, 40)), Some((theme::WARNING, true)), "{n}");
    }
    let mut real = real_app();
    for n in "1234567?".chars() {
        real.handle_key(key(n));
        lacks(&screen(&real), "PRACTICE");
        lacks(&screen(&real), "SIMULATION");
    }
}

#[test]
fn the_banner_says_when_a_screen_shows_the_real_system() {
    let mut a = practicing("healthy");
    for n in "4567".chars() {                           // Control Plane, Settings, Diagnostics, About
        a.handle_key(key(n));
        has(&screen(&a), "These screens show the real system");
    }
    a.handle_key(key('1'));
    has(&screen(&a), "Nothing here touches your GPU, containers, or real workload.");
}

#[test]
fn the_overview_guides_the_operator_from_the_servers_stage() {
    for (stage, text) in [
        ("healthy", "Press F to break the model (simulated)."),
        ("detecting", "needs two failed checks in a row"),
        ("awaiting_approval", "Open the incident (Enter), review it, then approve (A)."),
        ("recovering", "Restarting and verifying the recovery"),
        ("resolved", "Recovered and verified (simulated)."),
        ("rejected", "The incident closed as rejected."),
    ] {
        has(&screen(&practicing(stage)), text);
    }
}

#[test]
fn a_practice_overview_shows_simulated_values_and_no_real_safety_state() {
    let mut p = practicing("healthy");
    p.details = true;                                   // the technical view carries the safety section
    let s = screen(&p);
    for needle in ["simulated session", "sim/opt-125m", "Simulated session: there is nothing real to protect.",
                   "AUDIT ✓ VERIFIED (8 events, simulated)"] {
        has(&s, needle);
    }
    for real_only in ["Watchdog", "● ARMED", "Identity", "Budgets"] {
        lacks(&s, real_only);
    }
}

#[test]
fn the_footer_offers_practice_and_its_own_keys() {
    let real = ui::render_to_string(&real_app(), 120, 40);
    has(real.lines().last().unwrap(), "P Practice");
    let quiet = |stage: &str| {
        let mut a = practicing(stage);
        a.now += Duration::from_secs(11);                 // the "practice started" notice has expired
        a
    };
    let healthy = ui::render_to_string(&quiet("healthy"), 190, 40);
    let foot = healthy.lines().last().unwrap();
    for needle in ["F Break it", "P Restart practice", "Esc Leave practice"] {
        has(foot, needle);
    }
    // F is shown inactive once the model is no longer healthy
    let broken = quiet("detecting");
    let buf = ui::render_to_buffer(&broken, 190, 40);
    let w = buf.area.width as usize;
    let row = buf.area.height as usize - 1;
    let line: String = (0..w).map(|x| buf.content()[row * w + x].symbol().to_string()).collect();
    let at = line.find("F Break it").expect("F is still listed");
    assert_eq!(buf.content()[row * w + line[..at].chars().count()].fg, theme::TEXT_MUTED);
}

#[test]
fn the_real_overview_with_nothing_wrong_points_at_practice() {
    let s = screen(&real_app());
    has(&s, "Nothing right now");
    has(&s, "P  Practice an incident");
    lacks(&screen(&practicing("healthy")), "P  Practice an incident");
    // the technical view says the same
    let mut tech = real_app();
    tech.details = true;
    has(&screen(&tech), "No active incidents");
    has(&screen(&tech), "Press P to practice an incident (simulated; nothing real is touched)");
    tech.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    tech.apply(Msg::Poll(Ok(practice_snapshot("healthy", vec![]))));
    lacks(&screen(&tech), "Press P to practice an incident");
}

#[test]
fn help_lists_the_practice_keys() {
    let mut a = real_app();
    a.handle_key(key('?'));
    let s = screen(&a);
    has(&s, "Practice an incident (simulated; nothing real is touched)");
    has(&s, "Break the model while practicing (simulated)");
}

#[test]
fn every_practice_screen_renders_at_common_sizes_without_panicking() {
    let mut a = practicing("awaiting_approval");
    let inc = pending_incident();
    let mut snap = practice_snapshot("awaiting_approval", vec![inc.clone()]);
    snap.detail = Some(inc);
    a.apply(Msg::Poll(Ok(snap)));
    for (w, h) in [(72, 18), (80, 24), (120, 40), (200, 60)] {
        for n in "1234567?".chars() {
            a.handle_key(key(n));
            ui::render_to_string(&a, w, h);
        }
        a.handle_key(key('1'));
        a.handle_key(code(KeyCode::Enter));
        ui::render_to_string(&a, w, h);
        a.handle_key(key('a'));
        ui::render_to_string(&a, w, h);
        a.handle_key(code(KeyCode::Esc));
        a.handle_key(code(KeyCode::Esc));
    }
}
