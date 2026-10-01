//! The guarded REAL fault in the TUI: never a plain key, always an explicit confirmation, hidden
//! when it cannot be done, and impossible to miss while it is active.
use aiops_tui::api::ApiError;
use aiops_tui::app::{App, Command, Effect, Loaded, Msg, Snapshot, FAULT_SECONDS};
use aiops_tui::model::Status;
use aiops_tui::{theme, ui};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::time::Duration;

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");
const T: f64 = 1_790_866_932.0;

fn key(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
}
fn ctrl(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
}
fn code(k: KeyCode) -> KeyEvent {
    KeyEvent::new(k, KeyModifiers::NONE)
}

/// A real status; `faults` is what the server offers (`None` = no injector on this control plane).
fn status(faults: Option<serde_json::Value>) -> Status {
    let mut v: serde_json::Value = serde_json::from_str(HEALTHY).unwrap();
    if let Some(f) = faults {
        v["faults"] = f;
    }
    serde_json::from_value(v).unwrap()
}

fn app_with(faults: Option<serde_json::Value>) -> App {
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.wall = T;
    a.apply(Msg::Poll(Ok(Snapshot::new(status(faults), vec![]))));
    a
}

fn offered() -> App {
    app_with(Some(serde_json::json!({"available": true, "active": null})))
}

fn active(seconds_left: f64) -> App {
    app_with(Some(serde_json::json!({"available": true, "active": {
        "fault_id": "f1", "status": "ACTIVE", "expires_at": T + seconds_left, "workload": "vllm"}})))
}

fn palette_to(a: &mut App, words: &str) {
    a.handle_key(ctrl('p'));
    for c in words.chars() {
        a.handle_key(key(c));
    }
}

fn screen(a: &App) -> String {
    ui::render_to_string(a, 130, 40)
}
fn has(s: &str, needle: &str) {
    assert!(s.contains(needle), "missing {needle:?} in:\n{s}");
}
fn lacks(s: &str, needle: &str) {
    assert!(!s.contains(needle), "unexpected {needle:?} in:\n{s}");
}

// --- it is offered only where and when it can be done ----------------------------------------------

#[test]
fn the_palette_offers_the_fault_only_when_the_server_offers_it_and_none_is_active() {
    let listed = |a: &App| a.offer();
    assert!(listed(&offered()).can_break && !listed(&offered()).can_resume);
    assert!(!listed(&app_with(None)).can_break, "no injector on this control plane");
    assert!(!listed(&app_with(Some(serde_json::json!({"available": false, "active": null})))).can_break);
    let busy = listed(&active(60.0));
    assert!(!busy.can_break && busy.can_resume, "one fault at a time; resume is offered instead");
    let mut a = offered();
    a.handle_key(ctrl('p'));
    has(&screen(&a), "Break the real workload (pause)…");
    lacks(&screen(&a), "Resume the workload now");
}

#[test]
fn it_is_hidden_while_practicing_and_while_the_control_plane_is_unreachable() {
    let mut a = offered();
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    assert!(!a.offer().can_break && a.active_fault().is_none());
    let mut off = offered();
    off.apply(Msg::Poll(Err(ApiError::Offline("refused".into()))));
    assert!(!off.offer().can_break);
    off.handle_key(ctrl('p'));
    lacks(&screen(&off), "Break the real workload");
}

// --- never a plain key; always an explicit confirmation ----------------------------------------------

#[test]
fn no_plain_key_and_no_palette_typing_ever_opens_the_fault_dialog_or_sends_it() {
    let mut a = offered();
    for c in "abcdefghijklmnopqrstuvwxyz0123456789?".chars() {
        let fx = a.handle_key(key(c));
        assert!(!fx.iter().any(|e| matches!(e, Effect::PauseWorkload { .. })), "{c}");
        assert!(!a.fault_confirm, "{c} opened the dialog");
        a.handle_key(code(KeyCode::Esc));
    }
    palette_to(&mut a, "break");                        // only filters; nothing happens until Enter
    assert!(!a.fault_confirm);
}

#[test]
fn choosing_the_command_opens_a_dialog_that_says_what_will_happen_and_sends_nothing() {
    let mut a = offered();
    palette_to(&mut a, "break");
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty(), "choosing it only opens the dialog");
    assert!(a.fault_confirm);
    let s = screen(&a);
    for needle in ["Pause the real workload?", "LIVE · GPU-REAL", "This will pause the real inference workload.",
                   "stop answering until it is automatically resumed",
                   "(in 2 minutes) or the recovery flow restarts it.", "Nothing else is touched.", "vllm", "[Enter] Confirm", "[Esc] Cancel"] {
        has(&s, needle);
    }
    assert_eq!(FAULT_SECONDS, 120);
}

#[test]
fn only_enter_after_the_guard_sends_it_exactly_once_and_names_only_the_servers_workload() {
    let mut a = offered();
    palette_to(&mut a, "break");
    a.handle_key(code(KeyCode::Enter));
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty(), "inside the guard Enter is ignored");
    for k in [key('y'), key('a'), key('q'), key('p'), code(KeyCode::Tab), ctrl('p')] {
        assert!(a.handle_key(k).is_empty(), "other keys cannot confirm, quit or switch");
    }
    assert!(a.fault_confirm && a.palette.is_none());
    a.now += Duration::from_millis(600);
    assert_eq!(
        a.handle_key(code(KeyCode::Enter)),
        vec![Effect::PauseWorkload { target: "vllm".into(), seconds: 120 }]
    );
    assert!(!a.fault_confirm);
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty(), "a second Enter sends nothing");
}

#[test]
fn esc_cancels_the_dialog_immediately_and_sends_nothing() {
    let mut a = offered();
    palette_to(&mut a, "break");
    a.handle_key(code(KeyCode::Enter));
    assert!(a.handle_key(code(KeyCode::Esc)).is_empty() && !a.fault_confirm);
    a.now += Duration::from_secs(2);
    assert!(a.handle_key(code(KeyCode::Enter)).is_empty());
}

#[test]
fn the_confirm_key_looks_inactive_until_the_guard_has_elapsed() {
    let mut a = offered();
    palette_to(&mut a, "break");
    a.handle_key(code(KeyCode::Enter));
    let colour = |a: &App| {
        let buf = ui::render_to_buffer(a, 130, 40);
        let w = buf.area.width as usize;
        for row in 0..buf.area.height as usize {
            let line: String = (0..w).map(|x| buf.content()[row * w + x].symbol().to_string()).collect();
            if let Some(b) = line.find("[Enter] Confirm") {
                return buf.content()[row * w + line[..b].chars().count()].fg;
            }
        }
        panic!("dialog not on screen")
    };
    assert_eq!(colour(&a), theme::TEXT_MUTED);
    a.now += Duration::from_millis(600);
    assert_eq!(colour(&a), theme::ACCENT);
}

// --- while a fault is active ---------------------------------------------------------------------------

#[test]
fn an_active_fault_is_a_red_banner_on_every_screen_with_the_time_left() {
    let mut a = active(90.0);
    for n in "1234567?".chars() {
        a.handle_key(key(n));
        let s = screen(&a);
        has(&s, "LIVE · GPU-REAL · FAULT ACTIVE");
        has(&s, "The real workload is paused; it resumes by itself in 90s.");
        has(&s, "Resume the workload now");
        let buf = ui::render_to_buffer(&a, 130, 40);
        let w = buf.area.width as usize;
        let row = (0..buf.area.height as usize)
            .find(|r| (0..w).map(|x| buf.content()[r * w + x].symbol().to_string()).collect::<String>().contains("FAULT ACTIVE"))
            .unwrap();
        assert_eq!(buf.content()[row * w + 1].bg, theme::CRITICAL, "{n}");
    }
    a.wall = T + 30.0;
    has(&screen(&a), "resumes by itself in 60s");
    assert_eq!(a.active_fault().unwrap().fault_id, "f1");
    let long = active(300.0);
    has(&screen(&long), "resumes by itself in 5m 00s");
    // a narrow terminal keeps the facts and drops only the hint
    let narrow = ui::render_to_string(&active(90.0), 100, 30);
    has(&narrow, "FAULT ACTIVE");
    has(&narrow, "it resumes by itself in 90s.");
    lacks(&narrow, "Resume the workload now");
    a.wall = T + 200.0;
    has(&screen(&a), "is resuming now");
}

#[test]
fn no_banner_without_a_fault_and_none_in_practice() {
    lacks(&screen(&offered()), "FAULT ACTIVE");
    let mut a = active(60.0);
    a.apply(Msg::Loaded(Loaded::PracticeStarted(Ok(()))));
    a.apply(Msg::Poll(Ok({
        let mut s = Snapshot::new(status(None), vec![]);
        s.practice = true;
        s
    })));
    let s = screen(&a);
    lacks(&s, "FAULT ACTIVE");
    has(&s, "PRACTICE · SIMULATION");
}

#[test]
fn resuming_is_offered_while_active_and_sends_exactly_one_request() {
    let mut a = active(60.0);
    palette_to(&mut a, "resume");
    assert_eq!(a.handle_key(code(KeyCode::Enter)), vec![Effect::ResumeWorkload]);
    let mut none = offered();
    assert!(none.run(Command::ResumeWorkload).is_empty());
    assert!(none.active_notice().unwrap().is_error);
}

// --- what the operator is told ---------------------------------------------------------------------------

#[test]
fn the_results_are_reported_without_overstating() {
    let mut a = offered();
    a.apply(Msg::Loaded(Loaded::FaultPaused(Ok(()))));
    let n = a.active_notice().unwrap();
    assert!(!n.is_error && n.text.contains("resumes by itself"), "{}", n.text);

    a.apply(Msg::Loaded(Loaded::FaultPaused(Err(ApiError::Server {
        status: 409, code: "PRECONDITION_FAILED".into(), message: "the workload is already paused".into(),
    }))));
    let n = a.active_notice().unwrap();
    assert!(n.is_error && n.text.contains("the workload is already paused"));

    a.apply(Msg::Loaded(Loaded::FaultPaused(Err(ApiError::Timeout))));
    let n = a.active_notice().unwrap();
    assert!(n.is_error && n.text.contains("ends by itself") && !n.text.contains("failed"), "{}", n.text);

    a.apply(Msg::Loaded(Loaded::FaultResumed(Ok(()))));
    assert!(!a.active_notice().unwrap().is_error);
    a.apply(Msg::Loaded(Loaded::FaultResumed(Err(ApiError::Timeout))));
    assert!(a.active_notice().unwrap().is_error);
}

#[test]
fn a_dialog_in_progress_blocks_the_palette_and_every_screen_renders_with_banner_and_dialog() {
    let mut a = active(60.0);
    for (w, h) in [(72, 18), (80, 24), (130, 40), (200, 60)] {
        for n in "1234567?".chars() {
            a.handle_key(key(n));
            ui::render_to_string(&a, w, h);
        }
    }
    let mut b = offered();
    palette_to(&mut b, "break");
    b.handle_key(code(KeyCode::Enter));
    for (w, h) in [(72, 18), (80, 24), (130, 40)] {
        ui::render_to_string(&b, w, h);
    }
    b.handle_key(ctrl('p'));
    assert!(b.palette.is_none());
}
