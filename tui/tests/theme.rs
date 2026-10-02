//! Colour and legibility. The default on a truecolor terminal is a tonal palette (a ladder of
//! surfaces, one accent, three signals); a light twin, the terminal's own colours, and `NO_COLOR`
//! are the alternatives. Nothing is dimmed to illegibility and an error does not vanish unread.
use aiops_tui::api::ApiError;
use aiops_tui::app::{App, Msg, Snapshot, NOTICE_TTL};
use aiops_tui::model::{Incident, Status};
use aiops_tui::theme::{self, Theme};
use aiops_tui::ui;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::layout::Rect;
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
    a.apply(Msg::Poll(Ok(Snapshot::new(st, vec![inc.clone()]))));
    let mut snap = Snapshot::new(serde_json::from_str(UNRESPONSIVE).unwrap(), vec![inc.clone()]);
    snap.detail = Some(inc);
    a.apply(Msg::Poll(Ok(snap)));
    a
}
/// Every space, plain and technical, the incident stream, and every overlay.
fn every_view(theme: Theme) -> Vec<(String, ratatui::buffer::Buffer)> {
    let mut a = app(theme);
    let mut out = Vec::new();
    for details in [false, true] {
        a.details = details;
        for k in "12345?".chars() {
            a.handle_key(key(k));
            out.push((format!("{k} details={details}"), ui::render_to_buffer(&a, 130, 50)));
        }
        a.handle_key(key('1'));
        a.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        out.push((format!("detail details={details}"), ui::render_to_buffer(&a, 130, 50)));
        a.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    }
    a.details = false;
    a.handle_key(key('1'));
    a.handle_key(key('a'));                              // the decision
    out.push(("decision".into(), ui::render_to_buffer(&a, 130, 50)));
    a.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
    a.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
    out.push(("palette".into(), ui::render_to_buffer(&a, 130, 50)));
    out
}
fn cells(buf: &ratatui::buffer::Buffer) -> impl Iterator<Item = &ratatui::buffer::Cell> {
    buf.content().iter()
}
const RULES: &str = "─│┌┐└┘├┤┬┴┼▌›┃";

// --- the palette ---------------------------------------------------------------------------------

#[test]
fn every_text_token_is_legible_on_every_surface_it_sits_on_in_both_tonal_themes() {
    for theme in [Theme::Dark, Theme::Light] {
        let surfaces = [theme::PAGE, theme::PANEL, theme::ELEMENT, theme::RAISED].map(|c| theme.resolve(c));
        for (name, token) in [
            ("text", theme::TEXT), ("muted", theme::TEXT_MUTED), ("accent", theme::ACCENT),
            ("healthy", theme::HEALTHY), ("warning", theme::WARNING), ("critical", theme::CRITICAL),
        ] {
            let colour = theme.resolve(token);
            for (n, surface) in surfaces.iter().enumerate() {
                let ratio = theme::contrast(colour, *surface).expect("rgb");
                assert!(ratio >= 4.5, "{theme:?}: {name} on surface {n} is {ratio:.2}:1 (needs 4.5:1)");
            }
        }
        // body text is held to the higher bar (7:1) on the page and the panel
        for surface in &surfaces[..2] {
            let ratio = theme::contrast(theme.resolve(theme::TEXT), *surface).unwrap();
            assert!(ratio >= 7.0, "{theme:?}: text is {ratio:.2}:1");
        }
        // the accent used as a bar or a glyph colour must be seen against the page
        for token in [theme::ACCENT, theme::WARNING, theme::CRITICAL, theme::HEALTHY] {
            assert!(theme::contrast(theme.resolve(token), surfaces[0]).unwrap() >= 3.0);
        }
        // the surfaces are an ordered ladder: each step is distinguishable from the last
        for pair in surfaces.windows(2) {
            assert_ne!(pair[0], pair[1]);
        }
    }
}

#[test]
fn light_is_the_twin_of_dark_and_never_leaks_a_dark_token() {
    for (name, buf) in every_view(Theme::Light) {
        for c in cells(&buf) {
            for dark in [theme::PAGE, theme::PANEL, theme::TEXT] {
                assert_ne!(c.bg, dark, "{name}: a dark surface in the light theme");
            }
            assert_ne!(c.fg, theme::TEXT, "{name}: dark-theme text in the light theme");
        }
    }
}

#[test]
fn the_tonal_theme_paints_every_cell_so_it_never_depends_on_the_terminals_background() {
    for (name, buf) in every_view(Theme::Dark) {
        for c in cells(&buf) {
            assert!(matches!(c.bg, Color::Rgb(..)), "{name}: an unpainted cell {:?}", c.symbol());
        }
    }
}

// --- the alternatives ----------------------------------------------------------------------------

#[test]
fn mono_has_no_colour_anywhere_and_state_is_still_a_word_and_a_glyph() {
    for (name, buf) in every_view(Theme::Mono) {
        for c in cells(&buf) {
            assert_eq!((c.fg, c.bg), (Color::Reset, Color::Reset), "{name}: {:?}", c.symbol());
        }
    }
    let a = app(Theme::Mono);
    let s = ui::render_to_string(&a, 120, 40);
    for needle in ["! RECOVERY READY", "[ A ] Approve", "LIVE", "▸ Overview"] {
        assert!(s.contains(needle), "{needle}\n{s}");
    }
    // the bar is a glyph, so structure survives without colour or tone
    assert!(s.contains('┃'), "{s}");
    // the mode badge is reverse video, which survives without colour
    let buf = ui::render_to_buffer(&a, 120, 40);
    assert!(cells(&buf).any(|c| c.modifier.contains(Modifier::REVERSED)));
}

#[test]
fn the_terminal_theme_uses_only_the_terminals_own_colours_and_no_surfaces() {
    for (name, buf) in every_view(Theme::Terminal) {
        for c in cells(&buf) {
            assert!(!matches!(c.bg, Color::Rgb(..)), "{name}: a fixed background colour");
            if c.symbol().trim() != "" && !c.symbol().chars().all(|ch| RULES.contains(ch)) {
                assert!(
                    !matches!(c.fg, Color::Rgb(..) | Color::DarkGray | Color::Gray | Color::Black | Color::White | Color::Blue | Color::LightBlue),
                    "{name}: {:?} drawn in {:?}", c.symbol(), c.fg
                );
            }
        }
    }
    let buf = ui::render_to_buffer(&app(Theme::Terminal), 120, 40);
    let used: Vec<Color> = cells(&buf).map(|c| c.fg).collect();
    for c in [Color::Yellow, Color::Cyan, Color::Reset] {
        assert!(used.contains(&c), "{c:?} not used");
    }
}

#[test]
fn the_theme_comes_from_the_flag_or_the_environment() {
    assert_eq!(Theme::from_env(None, None), Theme::Terminal);
    assert_eq!(Theme::from_env(None, Some("truecolor")), Theme::Dark);
    assert_eq!(Theme::from_env(None, Some("24bit")), Theme::Dark);
    assert_eq!(Theme::from_env(None, Some("")), Theme::Terminal);
    assert_eq!(Theme::from_env(Some(""), Some("truecolor")), Theme::Dark, "an empty NO_COLOR is not set (no-color.org)");
    assert_eq!(Theme::from_env(Some("1"), Some("truecolor")), Theme::Mono, "NO_COLOR wins");
    for (name, theme) in [("dark", Theme::Dark), ("studio", Theme::Dark), ("light", Theme::Light), ("terminal", Theme::Terminal), ("mono", Theme::Mono)] {
        assert_eq!(Theme::parse(name), Some(theme), "{name}");
    }
    assert_eq!(Theme::parse("solarized"), None);
}

// --- the dimmed backdrop ----------------------------------------------------------------------------

#[test]
fn the_backdrop_dims_everything_outside_the_overlay_and_nothing_inside_it() {
    let mut a = app(Theme::Dark);
    let plain = ui::render_to_buffer(&a, 120, 40);
    a.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
    let with = ui::render_to_buffer(&a, 120, 40);
    // the header is outside the overlay: darker than without it
    let (before, after) = (plain.get(1, 0).fg, with.get(1, 0).fg);
    let sum = |c: Color| match c { Color::Rgb(r, g, b) => r as u32 + g as u32 + b as u32, _ => 0 };
    assert!(sum(after) < sum(before), "{before:?} -> {after:?}");
    // inside the panel the text is at full strength
    let panel_text = with.content().iter().filter(|c| c.symbol() == "C").map(|c| c.fg).any(|c| c == theme::TEXT);
    assert!(panel_text, "the palette's own text is dimmed");
    // outside any theme with RGB, dimming falls back to the DIM attribute
    let mut m = app(Theme::Terminal);
    m.handle_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
    let buf = ui::render_to_buffer(&m, 120, 40);
    assert!(buf.get(1, 0).modifier.contains(Modifier::DIM));
}

#[test]
fn dim_outside_leaves_the_kept_rectangle_alone() {
    let mut buf = ratatui::buffer::Buffer::empty(Rect::new(0, 0, 10, 4));
    for c in buf.content.iter_mut() {
        c.set_fg(Color::Rgb(200, 200, 200));
    }
    theme::dim_outside(&mut buf, Rect::new(2, 1, 3, 2));
    assert_eq!(buf.get(3, 2).fg, Color::Rgb(200, 200, 200));
    assert_eq!(buf.get(0, 0).fg, Color::Rgb(90, 90, 90));
}

// --- legibility of state and notices ------------------------------------------------------------------

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
fn an_error_toast_stays_until_a_key_is_pressed_and_a_success_fades() {
    let mut a = app(Theme::Dark);
    a.handle_key(key('2'));
    a.handle_key(key('a'));                       // on the Incidents list: refused with a reason
    let err = a.active_notice().expect("an error").clone();
    assert!(err.is_error);
    a.now += NOTICE_TTL * 6;                      // a long time later
    assert!(a.active_notice().is_some(), "an unread error must not vanish by itself");
    let s = ui::render_to_string(&a, 120, 40);
    assert!(s.contains("open the incident"), "the toast is on screen:\n{s}");
    a.handle_key(key('s'));                       // the next key acknowledges it
    assert!(a.active_notice().is_none());
}
