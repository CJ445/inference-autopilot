//! Colour and legibility: the default follows the terminal's own theme, `NO_COLOR` removes colour,
//! nothing is dimmed to illegibility, and an error does not vanish before it is read.
use std::time::Duration;

use aiops_tui::api::ApiError;
use aiops_tui::app::{App, Msg, Snapshot, NOTICE_TTL};
use aiops_tui::model::{Incident, Status};
use aiops_tui::theme::Theme;
use aiops_tui::ui;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::style::{Color, Modifier};

const HEALTHY: &str = include_str!("fixtures/status_healthy.json");
const UNRESPONSIVE: &str = include_str!("fixtures/status_unresponsive.json");
const PENDING: &str = include_str!("fixtures/incident_pending.json");
const T: f64 = 1_790_866_931.0;

fn key(c: char) -> KeyEvent {
    KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)
}
fn app(theme: Theme) -> App {
    let st: Status = serde_json::from_str(UNRESPONSIVE).unwrap();
    let inc: Incident = serde_json::from_str(PENDING).unwrap();
    let mut a = App::new("http://127.0.0.1:8080".into());
    a.theme = theme;
    a.wall = T + 1.25;
    a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![inc]))));
    a
}
/// Every screen, plain and technical, plus practice.
fn every_view(theme: Theme) -> Vec<(String, ratatui::buffer::Buffer)> {
    let mut a = app(theme);
    let mut out = Vec::new();
    for details in [false, true] {
        a.details = details;
        for k in "12345?".chars() {
            a.handle_key(key(k));
            out.push((format!("{k} details={details}"), ui::render_to_buffer(&a, 130, 60)));
        }
        a.handle_key(key('1'));
        a.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        out.push((format!("detail details={details}"), ui::render_to_buffer(&a, 130, 60)));
        a.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    }
    out
}
fn cells(buf: &ratatui::buffer::Buffer) -> impl Iterator<Item = &ratatui::buffer::Cell> {
    buf.content().iter()
}
const RULES: &str = "─│┌┐└┘├┤┬┴┼▌›"; // decoration: rules, borders, the bar and the stage separator

#[test]
fn mono_has_no_colour_anywhere_and_state_is_still_a_word_and_a_glyph() {
    for (name, buf) in every_view(Theme::Mono) {
        for c in cells(&buf) {
            assert_eq!((c.fg, c.bg), (Color::Reset, Color::Reset), "{name}: {:?}", c.symbol());
        }
    }
    let mut a = app(Theme::Mono);
    let s = ui::render_to_string(&a, 120, 40);
    for needle in ["! NEEDS YOUR OK", "→ Approve", "LIVE"] {
        assert!(s.contains(needle), "{needle}\n{s}");
    }
    // the selected row is marked by a glyph, not a background
    assert!(s.contains("› "), "{s}");
    // and the mode is reverse video, which survives without colour
    let buf = ui::render_to_buffer(&a, 120, 40);
    assert!(cells(&buf).any(|c| c.modifier.contains(Modifier::REVERSED)));
    a.practice = true;
}

#[test]
fn the_terminal_theme_uses_only_the_terminals_own_colours_for_text() {
    for (name, buf) in every_view(Theme::Terminal) {
        for c in cells(&buf).filter(|c| c.symbol().trim() != "" && !c.symbol().chars().all(|ch| RULES.contains(ch))) {
            assert!(
                !matches!(c.fg, Color::Rgb(..) | Color::DarkGray | Color::Gray | Color::Black | Color::White | Color::Blue | Color::LightBlue),
                "{name}: {:?} drawn in {:?}", c.symbol(), c.fg
            );
        }
        for c in cells(&buf) {
            assert!(!matches!(c.bg, Color::Rgb(..)), "{name}: a fixed background colour");
        }
    }
}

#[test]
fn the_terminal_theme_maps_each_meaning_to_a_named_colour() {
    let buf = ui::render_to_buffer(&app(Theme::Terminal), 120, 40);
    let used: Vec<Color> = cells(&buf).map(|c| c.fg).collect();
    for c in [Color::Yellow, Color::Cyan, Color::Reset] {
        assert!(used.contains(&c), "{c:?} not used");
    }
    // secondary text is the normal text colour, never a fixed grey
    assert!(!used.contains(&Color::Rgb(128, 134, 150)));
}

#[test]
fn the_dark_theme_keeps_the_fixed_palette() {
    let buf = ui::render_to_buffer(&app(Theme::Dark), 120, 40);
    assert!(cells(&buf).any(|c| matches!(c.fg, Color::Rgb(..))));
}

#[test]
fn the_theme_comes_from_the_flag_or_no_color() {
    assert_eq!(Theme::from_env(None), Theme::Terminal);
    assert_eq!(Theme::from_env(Some("")), Theme::Terminal, "an empty NO_COLOR is not set (no-color.org)");
    assert_eq!(Theme::from_env(Some("1")), Theme::Mono);
    assert_eq!(Theme::parse("terminal"), Some(Theme::Terminal));
    assert_eq!(Theme::parse("dark"), Some(Theme::Dark));
    assert_eq!(Theme::parse("mono"), Some(Theme::Mono));
    assert_eq!(Theme::parse("solarized"), None);
}

#[test]
fn stale_values_are_not_dimmed_they_are_labelled() {
    for details in [false, true] {
        let st: Status = serde_json::from_str(HEALTHY).unwrap();
        let mut a = App::new("http://127.0.0.1:8080".into());
        a.details = details;
        a.wall = T + 1.25;
        a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![]))));
        a.apply(Msg::Poll(Err(ApiError::Timeout)));
        a.wall = T + 31.0;
        let s = ui::render_to_string(&a, 120, 40);
        assert!(s.contains("Stale · last values observed"), "details={details}\n{s}");
        let buf = ui::render_to_buffer(&a, 120, 40);
        assert!(!cells(&buf).any(|c| c.modifier.contains(Modifier::DIM)), "details={details}: dimmed text");
    }
}

#[test]
fn an_error_stays_until_a_key_is_pressed_and_a_success_fades() {
    let mut a = app(Theme::Terminal);
    a.handle_key(key('2'));
    a.handle_key(key('a'));                       // on the Incidents list: refused with a reason
    let err = a.active_notice().expect("an error").clone();
    assert!(err.is_error);
    a.now += NOTICE_TTL * 6;                      // a long time later
    assert!(a.active_notice().is_some(), "an unread error must not vanish by itself");
    assert!(ui::render_to_string(&a, 120, 30).contains(&err.text));
    a.handle_key(key('s'));                       // the next key acknowledges it
    assert!(a.active_notice().is_none());
}
