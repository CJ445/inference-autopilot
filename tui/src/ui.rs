//! Rendering. Everything shown as infrastructure state comes from the server's data in `App`;
//! anything the server did not send is `N/A`. Colour only ever means state or focus (`theme`).
//!
//! Layout: a quiet header, a navigation sidebar, a content pane made of ruled sections rather than
//! boxed cards, and a footer of key hints. An incident reads top to bottom in the order the
//! control plane works: evidence, deterministic RCA, remediation, verification.
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph};
use ratatui::{Frame, Terminal};
use serde_json::Value;

use crate::app::{is_closed, ActionKind, App, Conn, Remote, Screen, CONFIRM_GUARD, KEYMAP};
use crate::model::{flag, num, text, Check, Evidence, Incident, Obj, Watchdog};
use crate::theme::{self, ACCENT, BORDER, CRITICAL, HEALTHY, TEXT_MUTED, WARNING};
use crate::pipeline::{self, Stage as PStage};
use crate::plain as words;
use crate::timeparse::{clock_hms, parse_rfc3339};

pub const MIN_W: u16 = 72;
pub const MIN_H: u16 = 18;
const LABEL_W: usize = 15;

// ---- small building blocks ------------------------------------------------------------------

fn fg(color: Color) -> Style {
    Style::default().fg(color)
}
fn span(content: impl Into<String>, color: Color) -> Span<'static> {
    Span::styled(content.into(), fg(color))
}
fn bold(content: impl Into<String>, color: Color) -> Span<'static> {
    Span::styled(content.into(), fg(color).add_modifier(Modifier::BOLD))
}
fn muted(content: impl Into<String>) -> Span<'static> {
    span(content, TEXT_MUTED)
}
fn plain(content: impl Into<String>) -> Span<'static> {
    Span::raw(content.into())
}
fn blank() -> Line<'static> {
    Line::from("")
}
fn mark(ok: Option<bool>) -> Span<'static> {
    match ok {
        Some(true) => span("✓", HEALTHY),
        Some(false) => span("✗", CRITICAL),
        None => muted("?"),
    }
}
fn gib(bytes: f64) -> String {
    format!("{:.1}", bytes / 1_073_741_824.0)
}
fn na(value: Option<String>) -> String {
    value.unwrap_or_else(|| "N/A".to_string())
}
fn width_of(spans: &[Span]) -> usize {
    spans.iter().map(|s| s.content.chars().count()).sum()
}

/// `left` and `right` on one row, `right` flush to the edge (dropped if there is no room).
fn split_row(mut left: Vec<Span<'static>>, right: Vec<Span<'static>>, width: usize) -> Line<'static> {
    let used = width_of(&left) + width_of(&right);
    if used + 2 <= width {
        left.push(plain(" ".repeat(width - used - 1)));
        left.extend(right);
    }
    Line::from(left)
}

/// A section divider: `─ Title ────────  note`.
fn rule(title: &str, note: Option<Span<'static>>, width: usize) -> Line<'static> {
    let note_w = note.as_ref().map_or(0, |n| n.content.chars().count() + 2);
    let fill = width.saturating_sub(title.chars().count() + note_w + 5);
    let mut spans = vec![
        span(" ─ ", BORDER),
        bold(title.to_string(), TEXT_MUTED),
        span(format!(" {}", "─".repeat(fill)), BORDER),
    ];
    if let Some(n) = note {
        spans.push(plain(" "));
        spans.push(n);
        spans.push(plain(" "));
    }
    Line::from(spans)
}

/// A thin full-width divider with no title.
fn hline(width: usize) -> Line<'static> {
    Line::from(span(format!(" {}", "─".repeat(width.saturating_sub(2))), BORDER))
}

/// Greedy word wrap to `room` columns.
fn wrap_words(content: &str, room: usize) -> Vec<String> {
    let room = room.max(10);
    let mut out: Vec<String> = Vec::new();
    for word in content.split_whitespace() {
        match out.last_mut() {
            Some(cur) if cur.chars().count() + 1 + word.chars().count() <= room => {
                cur.push(' ');
                cur.push_str(word);
            }
            _ => out.push(word.to_string()),
        }
    }
    out
}

/// Word wrap into one-space-indented lines (the pane keeps its margin when text wraps).
fn wrapped(content: &str, width: usize) -> Vec<Line<'static>> {
    wrap_words(content, width.saturating_sub(2)).into_iter().map(|l| Line::from(format!(" {l}"))).collect()
}

/// A labelled row: the label is quiet, the value carries the colour.
fn kv(label: &str, mut value: Vec<Span<'static>>) -> Line<'static> {
    let mut spans = vec![plain(" "), muted(format!("{label:<LABEL_W$}"))];
    spans.append(&mut value);
    Line::from(spans)
}

/// A full-width row on the secondary background (the selection).
fn highlighted(mut spans: Vec<Span<'static>>, width: usize) -> Line<'static> {
    let pad = width.saturating_sub(width_of(&spans));
    spans.push(plain(" ".repeat(pad)));
    let bg = Style::default().bg(theme::BACKGROUND_SECONDARY);
    Line::from(spans.into_iter().map(|s| Span::styled(s.content, s.style.patch(bg))).collect::<Vec<_>>())
}

/// `31s`, `4m`, `7h`: a long age is read, not counted in seconds.
fn human_age(secs: f64) -> String {
    let s = secs.max(0.0);
    if s < 90.0 {
        format!("{s:.0}s")
    } else if s < 5400.0 {
        format!("{:.0}m", s / 60.0)
    } else {
        format!("{:.0}h", s / 3600.0)
    }
}

/// The values on screen are old: say so in words, at full contrast (dimming made them unreadable).
fn stale_line(app: &App) -> Option<Line<'static>> {
    let age = if app.is_stale() { app.data_age().map(|d| d.as_secs() as f64) } else if app.telemetry_stale() { app.telemetry_age_secs() } else { None }?;
    Some(Line::from(vec![plain(" "), bold(format!("Stale · last values observed {} ago", human_age(age)), WARNING)]))
}

fn state_chip(status: &str) -> Span<'static> {
    let (label, color) = theme::state(status);
    bold(format!("● {label}"), color)
}

// ---- entry points ---------------------------------------------------------------------------

pub fn render(f: &mut Frame, app: &App) {
    let area = f.size();
    if area.width < MIN_W || area.height < MIN_H {
        let msg = format!("Terminal too small: need at least {MIN_W}x{MIN_H} (have {}x{})", area.width, area.height);
        f.render_widget(Paragraph::new(msg).style(fg(WARNING)), area);
        return;
    }
    // While practicing (amber) or while a REAL fault is active (red), a loud one-line banner sits
    // under the header on EVERY screen.
    let banner = app.practice || app.active_fault().is_some();
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints(if banner {
            vec![Constraint::Length(3), Constraint::Length(1), Constraint::Min(5), Constraint::Length(1)]
        } else {
            vec![Constraint::Length(3), Constraint::Min(5), Constraint::Length(1)]
        })
        .split(area);
    header(f, rows[0], app);
    if app.practice {
        practice_banner(f, rows[1], app);
    } else if banner {
        fault_banner(f, rows[1], app);
    }
    let (main, foot) = if banner { (rows[2], rows[3]) } else { (rows[1], rows[2]) };
    // An approve/reject in flight is shown inline, above the screen it was started from.
    let pane = if app.busy.is_some() && main.height > 8 {
        let split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(3), Constraint::Min(1)])
            .split(main);
        progress_banner(f, split[0], app);
        split[1]
    } else {
        main
    };
    match &app.screen {
        Screen::Dashboard if app.details => overview(f, pane, app),
        Screen::Dashboard => home(f, pane, app),
        Screen::Incidents if app.details => incidents(f, pane, app),
        Screen::Incidents => incident_list(f, pane, app),
        Screen::Detail(id) => detail(f, pane, app, id),
        Screen::Lab => lab(f, pane, app),
        Screen::Audit if app.details => audit(f, pane, app),
        Screen::Audit => audit_plain(f, pane, app),
        Screen::System => system(f, pane, app),
        Screen::Help => help(f, pane, app),
    }
    footer(f, foot, app);
    if app.confirm.is_some() {
        confirm_modal(f, area, app);
    } else if app.stop_confirm {
        stop_modal(f, area, app);
    } else if app.refusal.is_some() {
        refusal_modal(f, area, app);
    } else if app.fault_confirm {
        fault_modal(f, area, app);
    } else if app.palette.is_some() {
        palette_modal(f, area, app);
    }
    theme::apply(f.buffer_mut(), app.theme);
}

pub fn render_to_buffer(app: &App, width: u16, height: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).expect("test backend");
    terminal.draw(|f| render(f, app)).expect("draw");
    terminal.backend().buffer().clone()
}

/// The screen as plain text, one line per row (used by `--once` and by the tests).
pub fn render_to_string(app: &App, width: u16, height: u16) -> String {
    let buf = render_to_buffer(app, width, height);
    let w = buf.area.width as usize;
    let cells = buf.content();
    (0..buf.area.height as usize)
        .map(|row| {
            (0..w).map(|x| cells[row * w + x].symbol()).collect::<String>().trim_end().to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

// ---- header, sidebar, footer ----------------------------------------------------------------

fn header(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let status = match &app.conn {
        Conn::Connecting => bold("… CONNECTING", WARNING),
        Conn::Offline { .. } => bold("✗ CONTROL PLANE OFFLINE", CRITICAL),
        Conn::Online if app.snapshot.is_none() => bold("… LOADING", WARNING),
        Conn::Online => {
            let health = app.snapshot.as_ref().map_or("N/A", |s| s.status.health.as_str());
            if health == "HEALTHY" {
                span("● CONTROL ONLINE", HEALTHY)
            } else {
                bold(format!("! {health}"), WARNING)
            }
        }
    };
    let mut left = vec![plain(" "), bold("INFERENCE AUTOPILOT", ACCENT)];
    if app.snapshot.is_some() {
        left.push(plain("  "));
        left.push(mode_badge(app));
    }
    let title = split_row(left, vec![status], w);

    // navigation: the current area is bracketed (so it does not rest on colour), Incidents carries
    // the number of open incidents
    let open = app.rows().iter().filter(|i| !is_closed(&i.status)).count();
    let here = match &app.screen {
        Screen::Detail(_) => &Screen::Incidents,
        other => other,
    };
    let mut nav: Vec<Span> = vec![plain(" ")];
    for (n, (screen, label)) in [
        (Screen::Dashboard, "Home"), (Screen::Incidents, "Incidents"), (Screen::Lab, "Lab"),
        (Screen::Audit, "Activity"), (Screen::System, "System"),
    ].into_iter().enumerate() {
        let text = format!("{} {label}", n + 1);
        if &screen == here {
            nav.push(bold(format!("[{text}]"), ACCENT));
        } else {
            nav.push(plain(format!(" {text} ")));
        }
        if screen == Screen::Incidents && open > 0 {
            nav.push(bold(format!(" ● {open}"), WARNING));
        }
        nav.push(plain(" "));
    }
    if app.screen == Screen::Help {
        nav.push(bold("[? Help]", ACCENT));
    }

    // what protects you, right-aligned, dropped from the right when the row is narrow
    // Warnings come first so they are the LAST thing to be dropped when the row is narrow.
    let mut chips: Vec<Vec<Span>> = Vec::new();
    if let Some(s) = &app.snapshot {
        if app.is_stale() {
            let age = app.data_age().map_or(0, |d| d.as_secs());
            chips.push(vec![bold(format!("STALE {}", human_age(age as f64)), WARNING)]);
        }
        let real = s.status.mode.as_deref() != Some("SIMULATION");
        if real && app.telemetry_stale() {
            chips.push(vec![span(format!("telemetry stale ({})", human_age(app.telemetry_age_secs().unwrap_or(0.0))), WARNING)]);
        }
        if real && s.poll_ms > 1500 {
            chips.push(vec![span(format!("SLOW API ({:.1}s)", s.poll_ms as f64 / 1000.0), WARNING)]);
        }
        if real {
            let armed = s.status.watchdog.as_ref().map(|w| w.state == "armed");
            chips.push(vec![muted("Watchdog "), match armed { Some(true) => span("✓", HEALTHY), _ => span("!", CRITICAL) }]);
            chips.push(vec![muted("Audit "), mark(s.status.audit.as_ref().map(|a| a.valid))]);
        } else {
            chips.push(vec![muted("simulated session")]);
        }
        if app.details {
            chips.push(vec![muted(na(s.status.info.profile.clone()))]);   // which configuration (technical)
        }
    } else {
        chips.push(vec![muted("Watchdog ?   Audit ?")]);
    }
    if let Conn::Offline { error, .. } = &app.conn {
        chips.push(vec![muted(format!("Retrying… ({error})"))]);
    }
    let room = w.saturating_sub(width_of(&nav) + 2);
    let mut right: Vec<Span> = Vec::new();
    for chip in chips {
        let cost = width_of(&chip) + 3;
        if width_of(&right) + cost > room {
            break;
        }
        right.extend(chip);
        right.push(plain("   "));
    }
    let nav_row = split_row(nav, right, w);
    let block = Block::default().borders(Borders::BOTTOM).border_style(fg(BORDER));
    f.render_widget(Paragraph::new(vec![title, nav_row]).block(block), area);
}

/// Which world this is, in words and in reverse video (so it does not rest on colour alone).
/// REAL claims only what the data shows: GPU-REAL needs a GPU reading from the real telemetry
/// (a finer claim, such as which container runtime, would need the TUI to name it: it does not).
fn mode_badge(app: &App) -> Span<'static> {
    let Some(s) = &app.snapshot else { return muted("") };
    let (text, color) = if s.status.mode.as_deref() == Some("SIMULATION") {
        ("SIMULATION", WARNING)
    } else if s.status.last_observation.as_ref().is_some_and(|o| text(o, "gpu_uuid").is_some()) {
        ("LIVE · GPU-REAL", HEALTHY)
    } else {
        ("LIVE", HEALTHY)
    };
    Span::styled(format!(" {text} "), fg(color).add_modifier(Modifier::REVERSED | Modifier::BOLD))
}

/// A and R are live only on an incident's own page, and only while it awaits a decision.
fn can_decide(app: &App) -> bool {
    matches!(app.screen, Screen::Detail(_))
        && matches!(app.conn, Conn::Online)
        && app.busy.is_none()
        && app.selected_incident().map_or(false, awaiting_decision)
}

fn awaiting_decision(i: &Incident) -> bool {
    i.status == "POLICY_CHECK" && i.proposal.is_some()
}

fn footer(f: &mut Frame, area: Rect, app: &App) {
    let line = match app.active_notice() {
        Some(n) => Line::from(span(format!(" {}", n.text), if n.is_error { CRITICAL } else { HEALTHY })),
        None => {
            let live = can_decide(app);
            let offer = app.offer();
            // (key, label, active, keep-priority): when the row is too narrow the lowest
            // priorities go first, so Quit is never the one that falls off the edge. Only the keys
            // that work on this screen are listed.
            let mut hints: Vec<(&str, &str, bool, u8)> = vec![("Ctrl+P", "Commands", true, 8), ("?", "Help", true, 1), ("Q", "Quit", true, 9)];
            match &app.screen {
                Screen::Detail(_) => {
                    hints.push(("A", "Approve", live, 7));
                    hints.push(("R", "Reject", live, 7));
                    hints.push(("↑↓", "Scroll", true, 4));
                    hints.push(("Esc", "Back", true, 6));
                    hints.push(("D", if app.details { "Simple view" } else { "Technical details" }, true, 5));
                }
                Screen::System => {
                    hints.push(("D", "Run checks again", true, 7));
                    hints.push(("X", "Stop", true, 7));
                    hints.push(("←→", "Areas", true, 3));
                }
                Screen::Help => hints.push(("Esc", "Back", true, 6)),
                screen => {
                    if matches!(screen, Screen::Dashboard | Screen::Incidents | Screen::Lab) && !app.rows().is_empty() {
                        hints.push(("Enter", "Review", true, 6));
                    }
                    if matches!(screen, Screen::Incidents | Screen::Audit) {
                        hints.push(("↑↓", "Navigate", true, 4));
                    }
                    hints.push(("R", if app.practice { "Run again" } else { "Run test" }, !matches!(screen, Screen::Incidents), 7));
                    if app.practice {
                        let healthy = app.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.as_str()) == Some("healthy");
                        hints.push(("F", "Break it", healthy, 5));
                        if matches!(screen, Screen::Dashboard | Screen::Lab) {
                            hints.push(("Esc", "Leave test", true, 6));
                        }
                    } else if offer.can_break {
                        hints.push(("F", "Inject fault", true, 6));
                    }
                    hints.push(("←→", "Areas", true, 3));
                    hints.push(("D", if app.details { "Simple view" } else { "Details" }, true, 2));
                }
            }
            let cost = |h: &(&str, &str, bool, u8)| h.0.chars().count() + h.1.chars().count() + 4;
            while 1 + hints.iter().map(cost).sum::<usize>() > area.width as usize && hints.len() > 1 {
                let weakest = (0..hints.len()).min_by_key(|&n| hints[n].3).unwrap_or(0);
                hints.remove(weakest);
            }
            let mut spans = vec![plain(" ")];
            for (key, label, on, _) in hints {
                spans.push(if on { bold(key, theme::TEXT) } else { muted(key) });
                spans.push(muted(format!(" {label}   ")));
            }
            Line::from(spans)
        }
    };
    f.render_widget(Paragraph::new(line), area);
}

// ---- overview -------------------------------------------------------------------------------

fn no_data(f: &mut Frame, area: Rect, app: &App) {
    let mut lines = vec![blank()];
    match &app.conn {
        Conn::Offline { error, attempts, .. } => {
            lines.push(Line::from(bold(" CONTROL PLANE OFFLINE", CRITICAL)));
            lines.push(Line::from(format!(" {error}")));
            lines.push(Line::from(muted(format!(" Retrying {} (attempt {attempts})…", app.url))));
            lines.push(Line::from(muted(" Start it with `aiops` (or `aiops start`); this screen will recover by itself.")));
        }
        _ if app.practice => lines.push(Line::from(span(" Preparing the recovery test…", WARNING))),
        _ => lines.push(Line::from(span(format!(" Connecting to {}…", app.url), WARNING))),
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// The workload as the server reports it: `None` where the server does not say.
fn workload_state(app: &App) -> Option<&str> {
    app.snapshot.as_ref()?.status.workload.as_ref().map(|w| w.state.as_str())
}

/// True when the managed workload is not there to observe (absent or stopped).
fn no_workload(app: &App) -> bool {
    matches!(workload_state(app), Some("absent" | "stopped"))
}

fn observation(app: &App) -> Option<&Obj> {
    app.snapshot.as_ref()?.status.last_observation.as_ref()
}

fn overview(f: &mut Frame, area: Rect, app: &App) {
    if app.snapshot.is_none() {
        return no_data(f, area, app);
    }
    let w = area.width as usize;
    let mut l = headline(app, w);
    if app.practice {
        l.push(blank());
        l.extend(practice_guide(app));
    }
    l.push(blank());
    l.extend(incident_rows(app, w));
    l.push(blank());
    if let Some(line) = stale_line(app) {
        l.push(line);
        l.push(blank());
    }
    l.extend(inference_section(app, w));
    l.push(blank());
    l.extend(gpu_section(app, w));
    l.push(blank());
    l.extend(safety_section(app, w));
    f.render_widget(Paragraph::new(l), area);
}

/// What to do next in the practice, from the stage the SERVER reports (never guessed here).
fn practice_guide(app: &App) -> Vec<Line<'static>> {
    let stage = app.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.as_str());
    let text = match stage {
        None => "Preparing the recovery test…".to_string(),
        Some("healthy") => "The simulated model is healthy. The test is about to break it (F does it now).".to_string(),
        Some("detecting") => "The model stopped answering. The detector needs two failed checks in a row before it opens an incident.".to_string(),
        Some("awaiting_approval") => "A restart was proposed. Open the incident (Enter), review it, then approve (A).".to_string(),
        Some("recovering") => "Restarting and verifying the recovery…".to_string(),
        Some("resolved") => "Recovered and verified (simulated). R runs the test again; Esc leaves it.".to_string(),
        Some(other) => format!("The incident closed as {other}. R runs the test again; Esc leaves it."),
    };
    vec![Line::from(vec![plain(" "), bold("Recovery test  ", WARNING), plain(text)])]
}

/// A REAL fault is in progress: the workload is paused. Red, on every screen, with the time left.
fn fault_banner(f: &mut Frame, area: Rect, app: &App) {
    let left = app.active_fault().map_or(0.0, |a| (a.expires_at - app.wall).max(0.0));
    let when = if left < 1.0 {
        "is resuming now".to_string()
    } else if left < 120.0 {
        format!("resumes by itself in {left:.0}s")
    } else {
        format!("resumes by itself in {}m {:02}s", left as u64 / 60, left as u64 % 60)
    };
    let style = fg(CRITICAL).add_modifier(Modifier::REVERSED | Modifier::BOLD);
    // The facts always fit; the hint for ending it sooner is added only when there is room.
    let base = format!(" LIVE · GPU-REAL · FAULT ACTIVE   The real workload is paused; it {when}.");
    let full = format!("{base}  Ctrl+P → Resume the workload now");
    let text = if full.chars().count() <= area.width as usize { full } else { base };
    let pad = " ".repeat((area.width as usize).saturating_sub(text.chars().count()));
    f.render_widget(Paragraph::new(Line::from(Span::styled(format!("{text}{pad}"), style))), area);
}

/// The dialog that stands between the operator and pausing the REAL workload.
/// Optional sections are dropped, last first, until the dialog fits: the decision line (and the
/// words above it that say what is about to happen) are never the part that is cut off.
fn fit_sections(area: Rect, core_top: Vec<Line<'static>>, optional: Vec<Vec<Line<'static>>>, decision: Vec<Line<'static>>) -> Vec<Line<'static>> {
    let room = (area.height as usize).saturating_sub(2);
    let mut keep = optional.len();
    let total = |k: usize| core_top.len() + decision.len() + optional.iter().take(k).map(Vec::len).sum::<usize>();
    while keep > 0 && total(keep) > room {
        keep -= 1;
    }
    let mut lines = core_top;
    for section in optional.into_iter().take(keep) {
        lines.extend(section);
    }
    lines.extend(decision);
    lines
}

fn fault_modal(f: &mut Frame, area: Rect, app: &App) {
    let minutes = crate::app::FAULT_SECONDS / 60;
    let target = app.snapshot.as_ref().and_then(|s| s.status.info.workload.clone()).unwrap_or_else(|| "N/A".into());
    let armed = app.now.saturating_duration_since(app.fault_opened) >= CONFIRM_GUARD;
    let width = 80.min(area.width);
    let top = vec![
        split_row(
            vec![plain(" "), bold("REAL INFRASTRUCTURE FAULT", CRITICAL)],
            vec![bold("LIVE · GPU-REAL", CRITICAL), plain(" ")],
            width.saturating_sub(4) as usize,
        ),
        Line::from(format!(" This will pause the managed workload {target}.")),
        Line::from(format!(" It stops answering for up to {minutes} minutes, then resumes by itself.")),
    ];
    let flow = vec![
        blank(),
        Line::from(muted(" Expected flow")),
        Line::from("  Healthy › Paused › Detected › Your approval › Restart › Verified recovery"),
    ];
    let safety = vec![
        blank(),
        Line::from(muted(" Safety")),
        Line::from("  ✓ only the managed workload, by its checked identity"),
        Line::from("  ✓ a recovery lease is saved before anything is paused"),
        Line::from(format!("  ✓ it is resumed automatically after {minutes} minutes")),
        Line::from("  ✓ a separate process resumes it even if this one dies"),
    ];
    let decision = vec![
        blank(),
        Line::from(vec![
            plain(" "),
            if armed { bold("[Enter] Inject fault", ACCENT) } else { muted("[Enter] Inject fault") },
            bold("   [Esc] Cancel", ACCENT),
        ]),
    ];
    let lines = fit_sections(area, top, vec![safety, flow], decision);
    let rect = centered(area, width, lines.len() as u16 + 2);
    f.render_widget(Clear, rect);
    f.render_widget(Paragraph::new(lines).block(modal_block("Confirm", CRITICAL)), rect);
}

/// The loud one-line label shown on every screen while a simulation is on screen.
fn practice_banner(f: &mut Frame, area: Rect, app: &App) {
    let real_system_screen = app.screen == Screen::System;
    let note = if real_system_screen {
        "This screen shows the real system; the recovery test itself touches nothing real."
    } else {
        "Nothing here touches your GPU, containers, or real workload."
    };
    let style = fg(WARNING).add_modifier(Modifier::REVERSED | Modifier::BOLD);
    let text = format!(" RECOVERY TEST · SIMULATION   {note}");
    let pad = " ".repeat((area.width as usize).saturating_sub(text.chars().count()));
    f.render_widget(Paragraph::new(Line::from(Span::styled(format!("{text}{pad}"), style))), area);
}

fn headline(app: &App, w: usize) -> Vec<Line<'static>> {
    let info = app.snapshot.as_ref().map(|s| &s.status.info);
    let name = info.and_then(|i| i.model.clone().or_else(|| i.workload.clone()));
    let workload = info.and_then(|i| i.workload.clone()).filter(|wl| Some(wl) != name.as_ref());
    let o = observation(app);
    let state = match o.and_then(|o| flag(o, "inference_probe_ok")) {
        _ if workload_state(app) == Some("absent") => muted("○ NO WORKLOAD"),
        _ if workload_state(app) == Some("stopped") => muted("○ STOPPED"),
        _ if workload_state(app) == Some("starting") => span("◔ STARTING", WARNING),
        Some(true) => bold("● HEALTHY", HEALTHY),
        Some(false) => bold("✗ UNRESPONSIVE", CRITICAL),
        None => muted("N/A"),
    };
    let mut left = vec![plain(" "), bold(format!("vLLM · {}", na(name)), theme::TEXT)];
    if let Some(wl) = workload {
        left.push(muted(format!("   {wl}")));
    }
    vec![split_row(left, vec![state], w)]
}

fn incident_rows(app: &App, w: usize) -> Vec<Line<'static>> {
    let rows = app.rows();
    let mut lines = vec![rule("Incidents", None, w)];
    let (active, closed): (Vec<_>, Vec<_>) = rows.iter().enumerate().partition(|(_, i)| !is_closed(&i.status));
    let row = |idx: usize, spans: Vec<Span<'static>>| {
        let sel = idx == app.selected;
        let mut s = vec![if sel { bold(" › ", ACCENT) } else { plain("   ") }];
        s.extend(spans);
        if sel { highlighted(s, w) } else { Line::from(s) }
    };
    if active.is_empty() {
        lines.push(Line::from(span("   No active incidents", HEALTHY)));
        if !app.practice {
            lines.push(Line::from(muted("   Press R to run a recovery test (simulated; nothing real is touched)")));
        }
    }
    for (idx, i) in &active {
        let action = i.proposal.as_ref().map_or("-".to_string(), |p| p.action.clone());
        lines.push(row(*idx, vec![
            span(format!("{}  ", i.incident_id), ACCENT),
            plain(format!("{:<24}  ", i.category)),
            state_chip(&i.status),
            muted(format!("  {action}")),
        ]));
    }
    if !closed.is_empty() {
        lines.push(Line::from(muted("   RECENT")));
        for (idx, i) in &closed {
            let (label, color) = theme::state(&i.status);
            lines.push(row(*idx, vec![
                muted(format!("{}  {:<24}  → ", last_time(i), i.category)),
                span(label, color),
            ]));
        }
    }
    lines
}

/// The control plane's own freshness for what it last observed, or why there is none.
fn provenance(app: &App) -> Span<'static> {
    match app.telemetry_age_secs() {
        Some(age) if app.is_stale() && !app.telemetry_stale() => {
            span(format!("Stale · observed {age:.1}s ago"), WARNING)
        }
        Some(age) if app.telemetry_stale() => span(format!("Telemetry stale ({age:.1}s)"), WARNING),
        Some(age) => muted(format!("Observed {age:.1}s ago")),
        None => muted("No observation"),
    }
}

fn inference_section(app: &App, w: usize) -> Vec<Line<'static>> {
    if no_workload(app) {
        let what = if workload_state(app) == Some("stopped") { "Workload stopped" } else { "No workload running" };
        return vec![
            rule("Inference", None, w),
            Line::from(vec![plain(" "), bold(what, theme::TEXT), muted(": nothing is observed.")]),
            Line::from(muted(" Start the managed container and it is picked up automatically.")),
        ];
    }
    let o = observation(app);
    let probe = match o.and_then(|o| flag(o, "inference_probe_ok")) {
        Some(true) => {
            let ms = o.and_then(|o| num(o, "inference_probe_latency_ms"));
            vec![span("Probe ✓", HEALTHY), plain(format!("  {}", na(ms.map(|m| format!("{m:.0} ms")))))]
        }
        Some(false) => {
            let err = o.and_then(|o| text(o, "inference_probe_error")).unwrap_or_else(|| "failed".into());
            vec![span(format!("Probe ✗ {err}"), CRITICAL)]
        }
        None => vec![muted("Probe N/A")],
    };
    let metrics = match o.and_then(|o| flag(o, "vllm_metrics_available")) {
        Some(true) => span("Metrics ✓", HEALTHY),
        Some(false) => span("Metrics ✗", CRITICAL),
        None => muted("Metrics N/A"),
    };
    let count = |key: &str| na(o.and_then(|o| num(o, key)).map(|v| format!("{v:.0}")));
    let rpm = app.rates.requests_per_min.map(|r| format!("{r:.0}"));
    let lat = app.rates.latency_ms.map(|l| format!("{l:.0} ms"));
    let kv_cache = na(o.and_then(|o| num(o, "vllm_kv_cache_usage")).map(|k| format!("{:.1}%", k * 100.0)));
    let mut health = probe;
    health.push(plain("   "));
    health.push(metrics);
    vec![
        rule("Inference", None, w),
        kv("Health", health),
        kv("Traffic", vec![plain(format!("Req/min {}   Latency {}", na(rpm), na(lat)))]),
        kv("Queue", vec![plain(format!(
            "Running {}   Waiting {}   KV cache {kv_cache}   Tokens/s N/A",
            count("vllm_requests_running"), count("vllm_requests_waiting")
        ))]),
    ]
}

fn gpu_section(app: &App, w: usize) -> Vec<Line<'static>> {
    let o = observation(app);
    let uuid = o.and_then(|o| text(o, "gpu_uuid"));
    let device = match &uuid {
        Some(u) => format!("GPU 0 · {}", u.chars().take(17).collect::<String>()),
        None => "GPU N/A".to_string(),
    };
    let used = o.and_then(|o| num(o, "gpu_memory_used_bytes"));
    let total = o.and_then(|o| num(o, "gpu_memory_total_bytes")).filter(|t| *t > 0.0);
    let memory = match (used, total) {
        (Some(u), Some(t)) => {
            let pct = u / t * 100.0;
            let filled = ((pct / 10.0).round() as usize).min(10);
            // a quiet gauge: grey until memory is actually a concern
            let color = if pct < 70.0 { TEXT_MUTED } else if pct < 85.0 { WARNING } else { CRITICAL };
            vec![
                plain(format!("{} / {} GiB  {pct:.1}%  ", gib(u), gib(t))),
                span(format!("{}{}", "▰".repeat(filled), "▱".repeat(10 - filled)), color),
            ]
        }
        _ => vec![muted("N/A")],
    };
    let temp = o.and_then(|o| num(o, "gpu_temperature_c"));
    let util = o.and_then(|o| num(o, "gpu_utilization_percent"));
    vec![
        rule("GPU", Some(provenance(app)), w),
        kv("Device", vec![plain(device)]),
        kv("VRAM", memory),
        kv("Thermals", vec![plain(format!(
            "TEMP {}   UTIL {}",
            na(temp.map(|t| format!("{t:.0}°C"))), na(util.map(|u| format!("{u:.0}%")))
        ))]),
    ]
}

fn watchdog_badge(w: Option<&Watchdog>) -> (Span<'static>, Option<String>) {
    match w {
        Some(w) if w.state == "armed" => (span("● ARMED", HEALTHY), None),
        Some(w) => {
            let mut detail = w.state.clone();
            if let Some(r) = w.reason.as_ref().filter(|r| !r.is_empty()) {
                detail = format!("{detail}: {}", r.join(", "));
            }
            if let Some(e) = &w.error {
                detail = format!("{detail}: {e}");
            }
            (bold("! UNAVAILABLE", CRITICAL), Some(detail))
        }
        None => (bold("! UNAVAILABLE", CRITICAL), Some("not reported".to_string())),
    }
}

/// Budgets are judged only from the limits and the last sample the server reported.
fn budget_state(w: &Watchdog) -> Result<(), Vec<String>> {
    let (Some(limits), Some(sample)) = (&w.limits, &w.last_sample) else { return Err(vec![]) };
    let over: Vec<String> = limits
        .iter()
        .filter_map(|(k, limit)| {
            let name = k.strip_prefix("max_")?;
            (sample.get(name)?.as_f64()? > limit.as_f64()?).then(|| name.to_string())
        })
        .collect();
    if over.is_empty() { Ok(()) } else { Err(over) }
}

fn safety_section(app: &App, w: usize) -> Vec<Line<'static>> {
    let s = app.snapshot.as_ref();
    if s.map_or(false, |s| s.status.mode.as_deref() == Some("SIMULATION")) {
        let audit = match s.and_then(|s| s.status.audit.as_ref()) {
            Some(a) if a.valid => Line::from(span(format!(" AUDIT ✓ VERIFIED ({} events, simulated)", a.events), HEALTHY)),
            Some(_) => Line::from(bold(" AUDIT ✗ INTEGRITY FAILURE", CRITICAL)),
            None => Line::from(muted(" AUDIT N/A")),
        };
        return vec![rule("Safety", None, w), Line::from(muted(" Simulated session: there is nothing real to protect.")), audit];
    }
    let wd = s.and_then(|s| s.status.watchdog.as_ref());
    let (badge, detail) = watchdog_badge(wd);
    let mut dog = vec![badge];
    if let Some(d) = detail {
        dog.push(span(format!(" ({d})"), CRITICAL));
    } else if let Some(wd) = wd {
        let pid = s.and_then(|s| s.status.info.pid);
        dog.push(plain("   Identity "));
        dog.push(mark(match (wd.protected_pid, pid) {
            (Some(a), Some(b)) => Some(a == b),
            _ => None,
        }));
        dog.push(plain("   Budgets "));
        match budget_state(wd) {
            Ok(()) => dog.push(span("✓", HEALTHY)),
            Err(over) if over.is_empty() => dog.push(muted("?")),
            Err(over) => dog.push(span(format!("✗ {}", over.join(", ")), CRITICAL)),
        }
    }
    let audit_line = match s.and_then(|s| s.status.audit.as_ref()) {
        Some(a) if a.valid => Line::from(span(format!(" AUDIT ✓ VERIFIED ({} events)", a.events), HEALTHY)),
        Some(_) => Line::from(bold(" AUDIT ✗ INTEGRITY FAILURE", CRITICAL)),
        None => Line::from(muted(" AUDIT N/A")),
    };
    vec![rule("Safety", None, w), kv("WATCHDOG", dog), audit_line]
}


// ---- plain (default) views ---------------------------------------------------------------------
// Written from the same data as the technical views, in words a person follows. `D` shows the
// technical versions; nothing here is removed from them.

fn plain_chip(status: &str) -> Span<'static> {
    let (_, color) = theme::state(status);
    bold(format!("● {}", words::state_phrase(status)), color)
}

fn started_at(i: &Incident) -> String {
    first_time(i)
}

fn attention_rows(app: &App, w: usize) -> Vec<Line<'static>> {
    let rows = app.rows();
    let mut lines = Vec::new();
    for (idx, i) in rows.iter().enumerate().take(5) {
        let sel = idx == app.selected;
        let when = match i.status.as_str() {
            "RESOLVED" => format!("Recovered {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
            s if is_closed(s) => format!("Closed {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
            _ => format!("Detected {}", ago(app, &i.timeline.first().map_or(String::new(), |t| t.at.clone()))),
        };
        let head = vec![
            if sel { bold(" › ", ACCENT) } else { plain("   ") },
            badge_span(&i.status),
            plain(format!("   {}", words::problem_title(&i.category))),
        ];
        let row = split_row(head, vec![muted(format!("{} · {when}", i.incident_id)), plain(" ")], w);
        lines.push(if sel { highlighted(row.spans, w) } else { row });
    }
    lines
}

fn detail_plain(i: &Incident, w: usize, app: &App) -> Vec<Line<'static>> {
    let required = i.verification.as_ref().and_then(|v| v.required_completions);
    let mut l: Vec<Line> = Vec::new();
    l.push(split_row(vec![plain(" "), bold(words::problem_title(&i.category), theme::TEXT)], vec![plain_chip(&i.status)], w));
    l.push(Line::from(muted(format!(
        " {} · {} · started {}",
        i.service.clone().unwrap_or_else(|| "N/A".into()), i.incident_id, started_at(i)
    ))));
    l.push(blank());
    l.push(pipeline_line(&pipeline::stages(Some(i), true), w));
    l.push(blank());

    l.push(rule("What happened", None, w));
    l.extend(wrapped(words::problem_summary(&i.category).unwrap_or(&words::problem_title(&i.category)), w));
    l.push(blank());

    l.push(rule("What we found", None, w));
    match &i.rca {
        Some(r) if r.insufficient_evidence || r.root_cause.is_none() => {
            l.push(Line::from(" Not enough evidence to name a cause."));
        }
        Some(r) => l.extend(wrapped(r.root_cause.as_ref().map_or("", |c| c.statement.as_str()), w)),
        None => l.push(Line::from(muted(" N/A"))),
    }
    for e in &i.evidence {
        let sentence = words::evidence_sentence(e).unwrap_or_else(|| format!("{}: {}", words::humanise(&e.metric), e.value));
        let glyph = match e.relation.as_str() {
            "supports" => muted("✓"),
            "contradicts" => span("✗", WARNING),
            _ => muted("·"),
        };
        l.push(Line::from(vec![plain(" "), glyph, plain(format!(" {sentence}"))]));
    }
    l.push(blank());

    l.push(rule("Recommended action", None, w));
    match &i.proposal {
        Some(p) => {
            l.push(Line::from(vec![plain(" "), bold(words::action_phrase(&p.action), theme::TEXT)]));
            if p.action == "restart_workload" {
                l.push(Line::from(muted(" The model is unavailable while it restarts. There is no rollback.")));
            }
        }
        None if is_closed(&i.status) && i.status != "INSUFFICIENT_EVIDENCE" => {
            l.push(Line::from(muted(" No proposal in the server's record.")));
        }
        None => l.push(Line::from(" No fix is proposed.")),
    }
    if awaiting_decision(i) {
        l.push(Line::from(vec![plain(" "), bold("Needs your OK", WARNING)]));
        l.push(blank());
        // a decision needs the full incident on screen: the keys do nothing until it has loaded
        let loaded = app.snapshot.as_ref().and_then(|s| s.detail.as_ref()).is_some_and(|d| d.incident_id == i.incident_id);
        if loaded {
            l.push(split_row(vec![], vec![bold("[ A ] ", ACCENT), plain("Approve    "), bold("[ R ] ", ACCENT), plain("Reject")], w));
        } else {
            l.push(Line::from(muted(" Loading the full details…")));
        }
    }
    l.push(blank());

    l.push(rule("Recovery check", None, w));
    let checks = i.verification.as_ref().and_then(|v| v.checks.as_ref()).filter(|c| !c.is_empty());
    match checks {
        Some(checks) => {
            let mut keys: Vec<&String> = checks.keys().collect();
            keys.sort_by_key(|k| (CHECK_ORDER.iter().position(|c| c == k).unwrap_or(99), (*k).clone()));
            for k in keys {
                l.push(Line::from(vec![plain(" "), mark(Some(checks[k])), plain(format!(" {}", words::check_phrase(k, required)))]));
            }
            let (label, color) = (words::state_phrase(&i.status), theme::state(&i.status).1);
            l.push(blank());
            l.push(Line::from(vec![plain(" "), bold(label, color)]));
        }
        None if matches!(i.status.as_str(), "VERIFYING") => l.push(Line::from(muted(" In progress: waiting for the server's result."))),
        None if is_closed(&i.status) => l.push(Line::from(muted(" The recovery check did not run."))),
        None => l.extend(wrapped(&words::recovery_check(required), w)),
    }
    l.push(blank());

    l.push(rule("Step by step", None, w));
    for (time, phrase) in words::timeline_events(i) {
        l.push(Line::from(vec![muted(format!(" {time}  ")), plain(phrase)]));
    }
    l.push(blank());
    l.push(Line::from(muted(if app.details { " D  Simple view" } else { " D  Technical details" })));
    l
}

fn audit_plain(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let s = app.snapshot.as_ref();
    let fetched = s.and_then(|s| s.audit.as_ref());
    let valid = fetched.map(|a| a.valid).or_else(|| s.and_then(|s| s.status.audit.as_ref().map(|a| a.valid)));
    let mut lines = vec![blank(), match valid {
        Some(true) => Line::from(span(" Activity log intact", HEALTHY)),
        Some(false) => Line::from(bold(" ACTIVITY LOG INTEGRITY FAILURE", CRITICAL)),
        None => Line::from(muted(" Activity log N/A")),
    }];
    lines.push(rule("What happened", None, w));
    match fetched {
        None => lines.push(Line::from(muted(" waiting for the activity log…"))),
        Some(a) if a.events.is_empty() => {
            lines.push(Line::from(vec![plain(" "), bold("NO RECENT ACTIVITY", theme::TEXT)]));
            lines.push(Line::from(" Autopilot has not recorded any events yet."));
            if !app.practice {
                lines.push(blank());
                lines.push(Line::from(vec![plain(" "), bold("[R] ", ACCENT), plain("Run a recovery test")]));
            }
        }
        Some(a) => {
            for e in &a.events {
                lines.push(Line::from(vec![muted(" • "), plain(words::event_phrase(e))]));
            }
            lines.push(blank());
            lines.push(Line::from(muted(" D  Technical details (event names, hashes)")));
        }
    }
    f.render_widget(Paragraph::new(lines).scroll((app.scroll, 0)), area);
}

// ---- incidents screen -----------------------------------------------------------------------

fn first_time(i: &Incident) -> String {
    i.timeline.first().map_or("--:--:--".into(), |t| clock_hms(&t.at))
}

fn last_time(i: &Incident) -> String {
    i.timeline.last().map_or("--:--:--".into(), |t| clock_hms(&t.at))
}

fn incidents(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    // Columns by available width: the narrow table keeps what an operator triages by.
    let cols: &[(&str, usize)] = match w {
        0..=69 => &[("ID", 9), ("STATE", 22), ("CREATED", 9)],
        70..=104 => &[("ID", 9), ("CATEGORY", 24), ("STATE", 22), ("CREATED", 9)],
        _ => &[("ID", 9), ("CATEGORY", 24), ("STATE", 22), ("WORKLOAD", 9), ("CREATED", 9), ("PROPOSAL", 18)],
    };
    let header: String = cols.iter().map(|(h, n)| format!("{h:<n$} ")).collect();
    let mut lines = vec![blank(), Line::from(muted(format!("   {header}"))), hline(w)];
    let rows = app.rows();
    if rows.is_empty() {
        lines.push(Line::from(muted("   No incidents")));
    }
    for (idx, i) in rows.iter().enumerate() {
        let sel = idx == app.selected;
        let (label, color) = theme::state(&i.status);
        let mut spans = vec![if sel { bold(" › ", ACCENT) } else { plain("   ") }];
        for (name, n) in cols {
            spans.push(match *name {
                "ID" => span(format!("{:<n$} ", i.incident_id), ACCENT),
                "CATEGORY" => plain(format!("{:<n$} ", i.category)),
                "STATE" => bold(format!("{:<n$} ", format!("● {label}")), color),
                "WORKLOAD" => muted(format!("{:<n$} ", i.service.clone().unwrap_or_else(|| "-".into()))),
                "CREATED" => muted(format!("{:<n$} ", first_time(i))),
                _ => plain(format!("{:<n$} ", i.proposal.as_ref().map_or("-".to_string(), |p| p.action.clone()))),
            });
        }
        lines.push(if sel { highlighted(spans, w) } else { Line::from(spans) });
    }
    f.render_widget(Paragraph::new(lines), area);
}

// ---- incident detail ------------------------------------------------------------------------

const CHECK_ORDER: [&str; 5] = [
    "workload_available", "workload_restarted", "gpu_observable", "vllm_metrics_readable",
    "inference_probe_stable",
];

fn check_label(key: &str) -> String {
    match key {
        "workload_restarted" => "Workload identity changed".into(),
        "workload_available" => "Workload available".into(),
        "gpu_observable" => "GPU observable".into(),
        "gpu_memory_below_threshold" => "GPU memory below threshold".into(),
        "vllm_metrics_readable" => "vLLM metrics readable".into(),
        "inference_probe_stable" => "Stable window of real completions".into(),
        "verification_error" => "Verification could run".into(),
        other => other.replace('_', " "),
    }
}

/// One evidence line: what was measured and what it was, in operator words.
fn evidence_row(e: &Evidence, label_w: usize) -> Line<'static> {
    let glyph = match e.relation.as_str() {
        "supports" => muted("✓"),
        "contradicts" => span("✗", WARNING),
        _ => muted("·"),
    };
    let (label, value): (String, Vec<Span<'static>>) = match (e.metric.as_str(), &e.value) {
        ("inference_probe", Value::Object(o)) => {
            let value = if o.get("ok").and_then(Value::as_bool) == Some(false) {
                let err = o.get("error").and_then(Value::as_str).unwrap_or("no detail");
                vec![bold("FAILED", CRITICAL), plain(format!(" ({err})"))]
            } else {
                let ms = o.get("latency_ms").and_then(Value::as_f64).unwrap_or(0.0);
                vec![bold("PASSED", HEALTHY), plain(format!(" ({ms:.0} ms)"))]
            };
            ("Inference probe".into(), value)
        }
        ("vllm_metrics", Value::Object(o)) => {
            let up = o.get("available").and_then(Value::as_bool) == Some(true);
            ("vLLM metrics".into(), vec![if up { bold("AVAILABLE", HEALTHY) } else { bold("UNAVAILABLE", CRITICAL) }])
        }
        ("gpu_memory_used_bytes", Value::Number(n)) => {
            ("GPU memory".into(), vec![plain(format!("{} GiB used", gib(n.as_f64().unwrap_or(0.0))))])
        }
        (metric, v) => {
            let s = v.to_string();
            let s = if s.chars().count() > 40 { format!("{}…", s.chars().take(40).collect::<String>()) } else { s };
            (metric.replace('_', " "), vec![plain(s)])
        }
    };
    let mut spans = vec![plain(" "), glyph, plain(" "), muted(format!("{label:<label_w$}"))];
    spans.extend(value);
    spans.push(muted(format!("  {}", e.source)));
    Line::from(spans)
}

/// The label an evidence row will carry (so the column can be sized to the longest).
fn evidence_label(e: &Evidence) -> String {
    match e.metric.as_str() {
        "inference_probe" => "Inference probe".into(),
        "vllm_metrics" => "vLLM metrics".into(),
        "gpu_memory_used_bytes" => "GPU memory".into(),
        metric => metric.replace('_', " "),
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Stage {
    Done,
    Current,
    Todo,
    Skipped,
}

const STAGES: [&str; 8] = ["Evidence", "RCA", "Proposal", "Policy", "Approval", "Execute", "Verify", "Result"];

/// Where an incident is in the control-plane pipeline, from the server's status alone.
fn stages(status: &str) -> [Stage; 8] {
    use Stage::*;
    let upto = |current: usize, skipped: &[usize]| {
        let mut s = [Todo; 8];
        for (n, slot) in s.iter_mut().enumerate() {
            *slot = if skipped.contains(&n) { Skipped } else if n < current { Done } else if n == current { Current } else { Todo };
        }
        s
    };
    match status {
        "DETECTED" | "TRIAGING" => upto(0, &[]),
        "DIAGNOSED" => upto(1, &[]),
        "PROPOSED" => upto(2, &[]),
        "POLICY_CHECK" => upto(4, &[]),
        "APPROVED" | "EXECUTING" => upto(5, &[]),
        "VERIFYING" => upto(6, &[]),
        "REJECTED" => upto(7, &[5, 6]),
        "EXECUTION_FAILED" => upto(7, &[6]),
        "INSUFFICIENT_EVIDENCE" => upto(7, &[2, 3, 4, 5, 6]),
        _ => upto(8, &[]),
    }
}

fn tracker(status: &str, width: usize) -> Option<Line<'static>> {
    let total: usize = STAGES.iter().map(|s| s.len()).sum::<usize>() + 3 * (STAGES.len() - 1) + 1;
    if width < total + 1 {
        return None; // too narrow: the sections below already read in pipeline order
    }
    let mut spans = vec![plain(" ")];
    for (n, (name, st)) in STAGES.iter().zip(stages(status)).enumerate() {
        if n > 0 {
            spans.push(span(" › ", BORDER));
        }
        spans.push(match st {
            Stage::Done => plain(*name),
            Stage::Current => Span::styled(name.to_string(), fg(ACCENT).add_modifier(Modifier::BOLD | Modifier::UNDERLINED)),
            Stage::Todo => muted(*name),
            Stage::Skipped => Span::styled(name.to_string(), fg(theme::TEXT_MUTED).add_modifier(Modifier::CROSSED_OUT)),
        });
    }
    Some(Line::from(spans))
}

fn detail_lines(i: &Incident, w: usize) -> Vec<Line<'static>> {
    let mut l: Vec<Line> = Vec::new();
    l.push(split_row(vec![plain(" "), bold(i.category.clone(), theme::TEXT)], vec![state_chip(&i.status)], w));
    l.push(Line::from(muted(format!(
        " {} · {} · created {}",
        i.service.clone().unwrap_or_else(|| "N/A".into()), i.incident_id, first_time(i)
    ))));
    if let Some(t) = tracker(&i.status, w) {
        l.push(blank());
        l.push(t);
    }
    l.push(blank());

    l.push(rule("Evidence", None, w));
    if i.evidence.is_empty() {
        l.push(Line::from(muted(" none recorded")));
    }
    let label_w = i.evidence.iter().map(|e| evidence_label(e).chars().count()).max().unwrap_or(0).max(15) + 2;
    l.extend(i.evidence.iter().map(|e| evidence_row(e, label_w)));
    l.push(blank());

    l.push(rule("Deterministic RCA", None, w));
    match &i.rca {
        Some(r) if r.insufficient_evidence || r.root_cause.is_none() => {
            l.push(kv("Evidence", vec![bold("INSUFFICIENT", WARNING)]));
            l.push(Line::from(" Insufficient evidence to determine a root cause."));
        }
        Some(r) => {
            let class = r.root_cause.as_ref().map_or(i.category.clone(), |c| c.category.clone());
            l.push(kv("Classification", vec![plain(class)]));
            l.push(kv("Evidence", vec![bold("SUPPORTED", HEALTHY), muted(format!("  {} items", r.evidence_ids.len()))]));
            l.extend(wrapped(r.root_cause.as_ref().map_or("", |c| c.statement.as_str()), w));
        }
        None => l.push(Line::from(muted(" N/A"))),
    }
    l.push(blank());

    l.push(rule("Remediation", None, w));
    match &i.proposal {
        Some(p) => {
            let params: Vec<String> = p
                .parameters
                .iter()
                .map(|(k, v)| format!("{k}={}", v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())))
                .collect();
            l.push(kv("Action", vec![plain(format!("{}  ", p.action)), muted(params.join(" "))]));
        }
        None if is_closed(&i.status) && i.status != "INSUFFICIENT_EVIDENCE" => {
            l.push(kv("Action", vec![muted("N/A (no proposal in the server's record)")]))
        }
        None => l.push(kv("Action", vec![muted("No proposal")])),
    }
    let policy = match i.status.as_str() {
        "POLICY_CHECK" | "PROPOSED" if i.proposal.is_some() => bold("APPROVAL REQUIRED", WARNING),
        "REJECTED" => span("REJECTED", theme::REJECTED),
        "APPROVED" | "EXECUTING" | "VERIFYING" | "RESOLVED" | "UNRESOLVED" | "EXECUTION_FAILED" => {
            span("APPROVED", HEALTHY)
        }
        _ => muted("NOT APPLICABLE"),
    };
    l.push(kv("Policy", vec![policy]));
    if awaiting_decision(i) {
        l.push(kv("Proposal", vec![bold("PENDING", theme::PENDING)]));
    }
    if let Some(r) = &i.remediation {
        let extra = r.error.as_ref().map(|e| format!(" ({})", e.as_str().unwrap_or("see audit"))).unwrap_or_default();
        l.push(kv("Execution", vec![plain(format!("{}{extra}", r.state.replace('_', " ")))]));
    }
    if awaiting_decision(i) {
        l.push(blank());
        l.push(split_row(vec![], vec![
            bold("[ A ] ", ACCENT), plain("Approve    "), bold("[ R ] ", ACCENT), plain("Reject"),
        ], w));
    }
    l.push(blank());

    l.push(rule("Verification", None, w));
    let checks = i.verification.as_ref().and_then(|v| v.checks.as_ref()).filter(|c| !c.is_empty());
    if let Some(checks) = checks {
        let mut keys: Vec<&String> = checks.keys().collect();
        keys.sort_by_key(|k| (CHECK_ORDER.iter().position(|c| c == k).unwrap_or(99), (*k).clone()));
        for k in keys {
            l.push(Line::from(vec![plain(" "), mark(Some(checks[k])), plain(format!(" {}", check_label(k)))]));
        }
        l.push(blank());
        l.push(Line::from(muted(" RESULT")));
        let (label, color) = theme::state(&i.status);
        l.push(Line::from(bold(format!(" {label}"), color)));
    } else {
        l.push(Line::from(muted(match i.status.as_str() {
            "VERIFYING" => " In progress (waiting for the server's result)",
            "APPROVED" | "EXECUTING" => " Not started: the remediation is running",
            _ => " Not started",
        })));
    }
    l.push(blank());

    l.push(rule("Timeline", None, w));
    for t in &i.timeline {
        l.push(Line::from(vec![muted(format!(" {}  ", clock_hms(&t.at))), plain(t.state.clone())]));
    }
    l
}

fn detail(f: &mut Frame, area: Rect, app: &App, _id: &str) {
    let lines = match app.selected_incident() {
        Some(i) if app.details => detail_lines(i, area.width as usize),
        Some(i) => detail_plain(i, area.width as usize, app),
        None => vec![
            blank(),
            Line::from(span(" This incident is no longer reported by the control plane (Esc to go back).", WARNING)),
        ],
    };
    f.render_widget(Paragraph::new(lines).scroll((app.scroll, 0)), area);
}

// ---- audit ----------------------------------------------------------------------------------

fn audit(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let s = app.snapshot.as_ref();
    let fetched = s.and_then(|s| s.audit.as_ref());
    let valid = fetched.map(|a| a.valid).or_else(|| s.and_then(|s| s.status.audit.as_ref().map(|a| a.valid)));
    let mut lines = vec![blank(), match valid {
        Some(true) => Line::from(span(" AUDIT ✓ VERIFIED", HEALTHY)),
        Some(false) => Line::from(bold(" AUDIT ✗ INTEGRITY FAILURE", CRITICAL)),
        None => Line::from(muted(" AUDIT N/A")),
    }];
    lines.push(rule("Audit log", None, w));
    match fetched {
        None => lines.push(Line::from(muted(" waiting for the audit log…"))),
        Some(a) => {
            let room = w.saturating_sub(48).max(10);
            for e in &a.events {
                let data: String = e.data.to_string().chars().take(room).collect();
                lines.push(Line::from(vec![
                    muted(format!(" {:>4}  ", e.seq)),
                    plain(format!("{:<22} ", e.event)),
                    muted(format!("{:<12}  {data}", e.hash)),
                ]));
            }
        }
    }
    f.render_widget(Paragraph::new(lines).scroll((app.scroll, 0)), area);
}

// ---- modals ---------------------------------------------------------------------------------

fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width);
    let h = h.min(area.height);
    Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h }
}

fn modal_block(title: &str, color: Color) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .title(format!(" {title} "))
        .border_style(fg(color))
}

fn incident_by_id<'a>(app: &'a App, id: &str) -> Option<&'a Incident> {
    let s = app.snapshot.as_ref()?;
    s.detail.as_ref().filter(|d| d.incident_id == id).or_else(|| s.incidents.iter().find(|i| i.incident_id == id))
}

/// The recovery request: what happened, the evidence, the action, and why it is asking you.
fn confirm_modal(f: &mut Frame, area: Rect, app: &App) {
    let Some(c) = &app.confirm else { return };
    let width: u16 = 78.min(area.width);
    let room = (width as usize).saturating_sub(6);
    let approve = c.kind == ActionKind::Approve;
    let action = words::action_phrase(&c.action);
    let mut top = vec![split_row(
        vec![plain(" "), bold(if approve { "RECOVERY REQUEST" } else { "DECLINE THIS RECOVERY" }, WARNING)],
        if c.practice { vec![bold("SIMULATION", WARNING), plain(" ")] } else { vec![bold("LIVE", CRITICAL), plain(" ")] },
        width.saturating_sub(4) as usize,
    )];
    top.push(Line::from(vec![plain(" "), bold(words::problem_title(&c.category), theme::TEXT), muted(format!("   {}", c.incident_id))]));
    for part in wrap_words(if c.why.is_empty() { "No cause statement from the server." } else { &c.why }, room) {
        top.push(Line::from(format!(" {part}")));
    }
    top.push(Line::from(vec![plain(" "), muted("Proposed  "), bold(action, theme::TEXT), muted(format!("   workload {}", c.workload))]));
    let effect = match (c.kind, c.action.as_str()) {
        (ActionKind::Reject, _) => "No action is taken; the proposal is closed.".to_string(),
        (ActionKind::Approve, _) if c.practice => "A simulated restart: nothing real is restarted.".to_string(),
        (ActionKind::Approve, "restart_workload") => "The model is unavailable while it restarts. There is no rollback.".to_string(),
        (ActionKind::Approve, _) => "The server executes the proposed action.".to_string(),
    };
    for part in wrap_words(&effect, room) {
        top.push(Line::from(format!(" {part}")));
    }
    let mut evidence = Vec::new();
    if let Some(i) = incident_by_id(app, &c.incident_id) {
        let found: Vec<String> = i.evidence.iter().filter(|e| e.relation == "supports").filter_map(words::evidence_sentence).collect();
        if !found.is_empty() {
            evidence.push(blank());
            evidence.push(Line::from(muted(" Evidence")));
            for e in found {
                for (n, part) in wrap_words(&e, room.saturating_sub(2)).into_iter().enumerate() {
                    evidence.push(Line::from(format!("  {} {part}", if n == 0 { "•" } else { " " })));
                }
            }
        }
    }
    let why_ask = if approve { vec![blank(), Line::from(muted(" This action is allowlisted and runs only after you approve it."))] } else { Vec::new() };
    let armed = app.now.saturating_duration_since(c.opened) >= CONFIRM_GUARD;
    let verb = if approve { "Approve" } else { "Decline" };
    let decision = vec![
        blank(),
        Line::from(vec![
            plain(" "),
            if armed { bold(format!("[Enter] {verb}"), ACCENT) } else { muted(format!("[Enter] {verb}")) },
            bold("   [Esc] Cancel", ACCENT),
        ]),
    ];
    let lines = fit_sections(area, top, vec![why_ask, evidence], decision);
    let rect = centered(area, width, lines.len() as u16 + 2);
    f.render_widget(Clear, rect);
    f.render_widget(Paragraph::new(lines).block(modal_block("Decision", theme::BORDER_FOCUSED)), rect);
}

/// The server refused an approval because the problem is gone: nothing was restarted.
fn refusal_modal(f: &mut Frame, area: Rect, app: &App) {
    let Some(id) = &app.refusal else { return };
    let lines = vec![
        Line::from(vec![plain(" "), bold("RECOVERY NO LONGER NEEDED", HEALTHY)]),
        blank(),
        Line::from(format!(" {id}: the latest observation shows the problem is gone.")),
        Line::from(" The workload has recovered or is not running."),
        blank(),
        Line::from(vec![plain(" "), bold("No restart was performed.", theme::TEXT)]),
        Line::from(muted(" The incident is closed as Cleared; nothing was approved, run or verified.")),
        blank(),
        Line::from(vec![plain(" "), bold("[any key] Close", ACCENT)]),
    ];
    let rect = centered(area, 70.min(area.width), lines.len() as u16 + 2);
    f.render_widget(Clear, rect);
    f.render_widget(Paragraph::new(lines).block(modal_block("Safety", HEALTHY)), rect);
}

/// The approve/reject in flight, with the SERVER's current state for that incident. It never
/// predicts: the state shown is whatever the last poll reported.
fn progress_banner(f: &mut Frame, area: Rect, app: &App) {
    let Some(b) = &app.busy else { return };
    let secs = app.now.saturating_duration_since(b.since).as_secs();
    let server_state = match app.busy_incident() {
        Some(i) => state_chip(&i.status),
        None => muted("N/A"),
    };
    let lines = vec![
        Line::from(vec![
            plain(" "),
            bold("Waiting for the server…", WARNING),
            muted(format!("  {} {} sent ({secs}s)", b.kind.verb(), b.incident_id)),
        ]),
        Line::from(vec![
            plain(" "),
            muted("Server state  "),
            server_state,
            muted("   Remediation and verification can take a minute or two."),
        ]),
        hline(area.width as usize),
    ];
    f.render_widget(Paragraph::new(lines), area);
}

// ---- system screens (read-only; every value is the server's) ----------------------------------

fn show(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "none".into(),
        other => other.to_string(),
    }
}

/// `Loading…` / `N/A: why` for a request that has not produced a value (never a default).
fn pending<T>(remote: &Remote<T>, app: &App) -> Option<Line<'static>> {
    match remote {
        Remote::Ready(_) => None,
        Remote::Idle => Some(Line::from(muted(" N/A"))),
        Remote::Loading(since) => {
            let secs = app.now.saturating_duration_since(*since).as_secs();
            Some(Line::from(muted(format!(" Loading… ({secs}s)"))))
        }
        Remote::Failed(why) => Some(Line::from(span(format!(" N/A: {why}"), WARNING))),
    }
}

fn uptime(secs: f64) -> String {
    let s = secs.max(0.0) as u64;
    match (s / 86_400, s / 3600 % 24, s / 60 % 60) {
        (0, 0, m) => format!("{m}m {}s", s % 60),
        (0, h, m) => format!("{h}h {m}m"),
        (d, h, _) => format!("{d}d {h}h"),
    }
}

fn control_plane_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let mut l = vec![rule("Control plane", None, w)];
    let status = match &app.conn {
        Conn::Online => bold("● ONLINE", HEALTHY),
        Conn::Connecting => bold("… CONNECTING", WARNING),
        Conn::Offline { .. } => bold("✗ OFFLINE", CRITICAL),
    };
    l.push(kv("Status", vec![status]));
    l.push(kv("API", vec![plain(app.url.clone())]));
    let snap = app.snapshot.as_ref();
    let info = snap.map(|s| &s.status.info);
    let started = info.and_then(|i| i.started_at.as_deref()).and_then(|at| {
        let t = parse_rfc3339(at)?;
        Some(format!("{}  (up {})", clock_hms(at), uptime(app.wall - t)))
    });
    let last_known = if app.is_stale() { "  (last known)" } else { "" };
    for (label, value) in [
        ("PID", info.and_then(|i| i.pid).map(|p| p.to_string())),
        ("Started", started),
        ("Profile", info.and_then(|i| i.profile.clone())),
        ("Provider", info.and_then(|i| i.provider.clone())),
        ("Workload", info.and_then(|i| i.workload.clone())),
        ("Model", info.and_then(|i| i.model.clone())),
    ] {
        match value {
            Some(v) => l.push(kv(label, vec![plain(v), muted(last_known)])),
            None if label == "Model" => {}
            None => l.push(kv(label, vec![muted("N/A")])),
        }
    }
    if let Some(state) = workload_state(app) {
        l.push(kv("Workload state", vec![match state {
            "running" => span("● running", HEALTHY),
            "stopped" => muted("○ stopped (Workload stopped)"),
            "starting" => span("◔ starting (in its start period)", WARNING),
            "absent" => muted("○ No workload running"),
            _ => span("? unknown (ambiguous or unreadable)", WARNING),
        }]));
    }
    let wd = snap.and_then(|s| s.status.watchdog.as_ref());
    let (badge, detail) = watchdog_badge(wd);
    let mut dog = vec![badge];
    if let Some(d) = detail {
        dog.push(span(format!(" ({d})"), CRITICAL));
    }
    l.push(kv("Watchdog", dog));
    l.push(blank());
    l.push(rule("Lifecycle", None, w));
    l.push(split_row(vec![plain(" "), bold("[ X ] ", ACCENT), plain("Stop control plane")], vec![], w));
    l.push(Line::from(muted(" Stopping ends the control plane and its watchdog; the managed workload is")));
    l.push(Line::from(muted(" not touched. Start it again with `aiops` (or `aiops start`).")));
    l
}

fn settings_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let ms = |v: Option<u64>| v.map_or("N/A".to_string(), |n| format!("{n} ms"));
    let mut l = vec![
        rule("Client", Some(muted("this terminal UI")), w),
        kv("API", vec![plain(app.url.clone())]),
        kv("Refresh", vec![plain(ms(app.client.interval_ms)), muted("   --interval-ms")]),
        kv("Timeout", vec![plain(ms(app.client.timeout_ms)), muted("   --timeout-ms")]),
        Line::from(muted(" No other client preferences exist; the layout adapts to the terminal size.")),
        blank(),
        rule("Control plane configuration", Some(muted("read-only")), w),
    ];
    match pending(&app.config, app) {
        Some(line) => l.push(line),
        None => {
            if let Remote::Ready(c) = &app.config {
                l.push(kv("Source", vec![plain(c.source.clone())]));
                l.push(kv("Status", vec![span("● Valid", HEALTHY), muted("  loaded and validated by the control plane")]));
                l.push(kv("Profile", vec![plain(c.profile.clone())]));
                l.push(kv("Provider", vec![plain(c.provider.clone())]));
                for (name, values) in &c.sections {
                    l.push(blank());
                    l.push(Line::from(muted(format!(" [{name}]"))));
                    for (k, v) in values {
                        l.push(kv(&format!("  {k}"), vec![plain(show(v))]));
                    }
                }
            }
        }
    }
    l.push(blank());
    l.push(Line::from(muted(" Edit the file, then `aiops stop` and `aiops` to apply. This screen never changes it.")));
    l
}

fn check_chip(status: &str) -> Span<'static> {
    match status {
        "PASS" => span("● PASS", HEALTHY),
        "WARN" => bold("● WARN", WARNING),
        "FAIL" => bold("● FAIL", CRITICAL),
        "NOT_APPLICABLE" => muted("○ NOT_APPLICABLE"),
        other => muted(format!("? {other}")),
    }
}

fn check_note(c: &Check) -> &'static str {
    if c.status == "FAIL" && !c.blocking { "  (workload health; does not block start)" } else { "" }
}

fn diagnostics_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let note = match &app.diag {
        Remote::Ready(d) => d.duration_seconds.map(|s| muted(format!("ran in {s:.1}s"))),
        _ => None,
    };
    let mut l = vec![rule("Diagnostics", note, w)];
    match pending(&app.diag, app) {
        Some(line) => l.push(line),
        None => {
            let Remote::Ready(d) = &app.diag else { return l };
            for c in &d.results {
                let room = w.saturating_sub(40).max(10);
                let detail: String = if c.detail.chars().count() > room {
                    format!("{}…", c.detail.chars().take(room - 1).collect::<String>())
                } else {
                    c.detail.clone()
                };
                l.push(Line::from(vec![
                    plain(" "),
                    check_chip(&c.status),
                    plain(" ".repeat(18usize.saturating_sub(c.status.chars().count() + 2))),
                    plain(format!("{:<17}", c.check)),
                    muted(detail),
                ]));
            }
            let count = |st: &str| d.results.iter().filter(|c| c.status == st).count();
            l.push(blank());
            l.push(Line::from(muted(format!(
                " {} PASS · {} WARN · {} FAIL · {} NOT_APPLICABLE",
                count("PASS"), count("WARN"), count("FAIL"), count("NOT_APPLICABLE")
            ))));
            for (title, status, color) in [("Failures", "FAIL", CRITICAL), ("Warnings", "WARN", WARNING)] {
                let items: Vec<&Check> = d.results.iter().filter(|c| c.status == status).collect();
                if items.is_empty() {
                    continue;
                }
                l.push(blank());
                l.push(rule(title, None, w));
                for c in items {
                    l.push(Line::from(vec![plain(" "), bold(c.check.clone(), color), muted(check_note(c))]));
                    l.extend(wrapped(&c.detail, w));
                }
            }
        }
    }
    l
}

fn about_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let mut l = vec![
        rule("About", None, w),
        Line::from(vec![plain(" "), bold("AIOPS", ACCENT), muted("  Autonomous AIOps for LLM inference infrastructure")]),
        kv("Operator UI", vec![plain(env!("CARGO_PKG_VERSION"))]),
    ];
    match pending(&app.version, app) {
        Some(line) => l.push(line),
        None => {
            if let Remote::Ready(v) = &app.version {
                l.push(kv("Version", vec![plain(v.version.clone())]));
                if let Some(rev) = &v.git_revision {
                    l.push(kv("Git revision", vec![plain(rev.clone())]));
                }
                if let Some(p) = &v.python {
                    l.push(kv("Python", vec![plain(p.clone())]));
                }
                if let Some(p) = &v.platform {
                    l.push(kv("Platform", vec![plain(p.clone())]));
                }
            }
        }
    }
    l.push(kv("Connection", vec![match &app.conn {
        Conn::Online => span("● connected", HEALTHY),
        Conn::Connecting => span("… connecting", WARNING),
        Conn::Offline { .. } => span("✗ not connected", CRITICAL),
    }]));
    l
}

/// System: the control plane, its checks, its read-only configuration and About, in one scroll.
fn system(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let mut l = vec![blank()];
    for (n, section) in [control_plane_lines(app, w), diagnostics_lines(app, w), settings_lines(app, w), about_lines(app, w)]
        .into_iter()
        .enumerate()
    {
        if n > 0 {
            l.push(blank());
        }
        l.extend(section);
    }
    f.render_widget(Paragraph::new(l).scroll((app.scroll, 0)), area);
}

fn help(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let mut l = vec![blank(), rule("Keys", None, w)];
    for (key, what) in KEYMAP {
        l.push(Line::from(vec![plain(" "), bold(format!("{key:<9}"), theme::TEXT), muted(what)]));
    }
    l.push(blank());
    l.push(rule("How it fits together", None, w));
    l.push(Line::from(muted(" A problem is found, a fix is proposed, and nothing happens until you approve it (A).")));
    l.push(Line::from(muted(" The server then runs the fix and checks that the model recovered. D shows the")));
    l.push(Line::from(muted(" technical details behind any screen. Stopping the control plane is in System (4).")));
    l.push(blank());
    l.push(rule("Testing", None, w));
    l.push(Line::from(muted(" The Lab (3) is where you try the loop. R runs a recovery test: a simulated model failure,")));
    l.push(Line::from(muted(" detected, diagnosed and recovered with your approval, touching nothing real.")));
    l.push(Line::from(muted(" F (or Ctrl+P) injects a REAL fault: it asks first, pauses the real workload for a")));
    l.push(Line::from(muted(" bounded time and always resumes it.")));
    f.render_widget(Paragraph::new(l).scroll((app.scroll, 0)), area);
}

fn stop_modal(f: &mut Frame, area: Rect, app: &App) {
    let rect = centered(area, 58, 10);
    f.render_widget(Clear, rect);
    let lines = vec![
        Line::from(bold(" Stop control plane?", WARNING)),
        blank(),
        Line::from(" The operator UI will disconnect."),
        Line::from(muted(" The control plane and its watchdog stop.")),
        Line::from(muted(" The managed workload is not touched.")),
        blank(),
        Line::from(vec![
            plain(" "),
            if app.now.saturating_duration_since(app.stop_opened) >= CONFIRM_GUARD {
                span("[Enter] Stop", ACCENT)
            } else {
                muted("[Enter] Stop")
            },
            span("   [Esc] Cancel", ACCENT),
        ]),
    ];
    f.render_widget(Paragraph::new(lines).block(modal_block("Confirm", theme::BORDER_FOCUSED)), rect);
}

fn palette_modal(f: &mut Frame, area: Rect, app: &App) {
    let Some(p) = &app.palette else { return };
    let items = p.matches(app.offer());
    let h = (items.len().max(1) as u16 + 4).min(area.height.saturating_sub(2));
    let w = 56.min(area.width.saturating_sub(4));
    let rect = Rect { x: area.x + (area.width - w) / 2, y: area.y + 2, width: w, height: h };
    f.render_widget(Clear, rect);
    let inner_w = w.saturating_sub(2) as usize;
    let query = if p.query.is_empty() {
        vec![plain(" › "), muted("Search commands…")]
    } else {
        vec![plain(" › "), plain(p.query.clone()), span("▏", ACCENT)]
    };
    let mut lines = vec![Line::from(query), hline(inner_w + 2)];
    if items.is_empty() {
        lines.push(Line::from(muted("   No matching command")));
    }
    for (n, cmd) in items.iter().enumerate() {
        let sel = n == p.selected;
        let spans = vec![if sel { bold(" › ", ACCENT) } else { plain("   ") }, plain(cmd.label())];
        lines.push(if sel { highlighted(spans, inner_w) } else { Line::from(spans) });
    }
    f.render_widget(Paragraph::new(lines).block(modal_block("Commands", theme::BORDER_FOCUSED)), rect);
}

// ---- the pipeline, shared by Home, the incident page and the Lab ---------------------------------

fn pstage_style(st: PStage) -> (Color, Modifier) {
    match st {
        PStage::Done => (HEALTHY, Modifier::empty()),
        PStage::Active => (ACCENT, Modifier::BOLD),
        PStage::Failed => (CRITICAL, Modifier::BOLD),
        PStage::NotRun => (theme::TEXT_MUTED, Modifier::empty()),
        PStage::NotApplicable => (theme::TEXT_MUTED, Modifier::CROSSED_OUT),
    }
}

/// `✓ Observe › ✓ Detect › → Approve › ○ Recover …`: a glyph and a word per stage (never colour
/// alone). On a narrow pane it falls back to the glyphs and the name of the stage the loop is on.
fn pipeline_line(stages: &[PStage; 7], w: usize) -> Line<'static> {
    // exactly as wide as the seven stages need; the leading space goes first when the pane is tight,
    // so that an 80-column terminal (the common one) still gets the full words
    let full: usize = pipeline::NAMES.iter().map(|n| n.len() + 2).sum::<usize>() + 3 * 6;
    let mut spans = if w > full { vec![plain(" ")] } else { Vec::new() };
    if w >= full {
        for (n, (name, st)) in pipeline::NAMES.iter().zip(stages).enumerate() {
            if n > 0 {
                spans.push(span(" › ", BORDER));
            }
            let (color, modifier) = pstage_style(*st);
            let waiting = *st == PStage::Active && n == 4;       // waiting for the operator
            let color = if waiting { WARNING } else { color };
            spans.push(Span::styled(format!("{} {name}", st.glyph()), fg(color).add_modifier(modifier)));
        }
    } else {
        for st in stages {
            let (color, modifier) = pstage_style(*st);
            spans.push(Span::styled(format!("{} ", st.glyph()), fg(color).add_modifier(modifier)));
        }
        if let Some(n) = pipeline::current(stages) {
            spans.push(bold(format!(" {}", pipeline::NAMES[n]), theme::TEXT));
        }
    }
    Line::from(spans)
}

fn observing(app: &App) -> bool {
    matches!(app.conn, Conn::Online) && observation(app).is_some()
}

/// The incident the loop is about: the open one, else the latest.
fn focus_incident(app: &App) -> Option<&Incident> {
    let rows = app.rows();
    let pick = rows.iter().find(|i| !is_closed(&i.status)).or(rows.first()).copied()?;
    // the full incident (with its recorded checks) when the server has sent it
    incident_by_id(app, &pick.incident_id).or(Some(pick))
}

fn ago(app: &App, at: &str) -> String {
    let Some(t) = parse_rfc3339(at) else { return "N/A".into() };
    let secs = (app.wall - t).max(0.0) as u64;
    match secs {
        0..=59 => format!("{secs}s ago"),
        60..=3599 => format!("{}m ago", secs / 60),
        _ => format!("{}h ago", secs / 3600),
    }
}

/// `● STATE`: the glyph, the word and the colour of an incident for the operator.
fn status_badge(status: &str) -> (&'static str, &'static str, Color) {
    match status {
        "DETECTED" | "TRIAGING" | "DIAGNOSED" => ("→", "INVESTIGATING", ACCENT),
        "PROPOSED" | "POLICY_CHECK" => ("!", "NEEDS YOUR OK", WARNING),
        "APPROVED" | "EXECUTING" => ("→", "RECOVERING", ACCENT),
        "VERIFYING" => ("→", "VERIFYING", ACCENT),
        "RESOLVED" => ("✓", "RESOLVED", HEALTHY),
        "UNRESOLVED" => ("✗", "NOT RESOLVED", CRITICAL),
        "EXECUTION_FAILED" => ("✗", "FIX FAILED", CRITICAL),
        "REJECTED" => ("–", "DECLINED", theme::TEXT_MUTED),
        "CLEARED" => ("–", "CLEARED", theme::TEXT_MUTED),
        "INSUFFICIENT_EVIDENCE" => ("!", "NOT ENOUGH EVIDENCE", WARNING),
        _ => ("?", "UNKNOWN", theme::TEXT_MUTED),
    }
}

fn badge_span(status: &str) -> Span<'static> {
    let (glyph, word, color) = status_badge(status);
    bold(format!("{glyph} {word}"), color)
}

fn clock_of(i: &Incident, states: &[&str]) -> Option<String> {
    i.timeline.iter().find(|t| states.contains(&t.state.as_str())).map(|t| clock_hms(&t.at))
}

// ---- Home -----------------------------------------------------------------------------------------

fn home(f: &mut Frame, area: Rect, app: &App) {
    if app.snapshot.is_none() {
        return no_data(f, area, app);
    }
    let w = area.width as usize;
    let mut l = vec![blank()];
    if matches!(app.conn, Conn::Offline { .. }) {
        if let Some(line) = stale_line(app) {
            l.push(line);                       // the values below are the last known ones
            l.push(blank());
        }
    }
    if app.practice {
        l.extend(practice_guide(app));
        l.push(blank());
    }
    l.push(rule("Inference", None, w));
    l.extend(inference_facts(app));
    l.push(blank());
    l.push(rule("Autopilot", None, w));
    l.extend(autopilot_lines(app, w));
    l.push(blank());
    l.push(rule("Quick actions", None, w));
    l.push(quick_actions(app));
    f.render_widget(Paragraph::new(l), area);
}

/// What the backend reports about the workload, and `N/A` for everything it does not.
fn inference_facts(app: &App) -> Vec<Line<'static>> {
    match workload_state(app) {
        Some("absent") => {
            return vec![
                Line::from(vec![plain(" "), bold("NO WORKLOAD CONNECTED", theme::TEXT)]),
                Line::from(" The control plane is running, but no managed inference"),
                Line::from(" workload is currently available."),
                Line::from(muted(" Autopilot will not create or start one automatically.")),
                Line::from(muted(" Go to System (5) for diagnostics.")),
            ]
        }
        Some("stopped") => {
            return vec![
                Line::from(vec![plain(" "), bold("WORKLOAD STOPPED", theme::TEXT)]),
                Line::from(" The managed workload is stopped. Autopilot will not start it;"),
                Line::from(muted(" start it yourself and it is picked up automatically.")),
            ]
        }
        _ => {}
    }
    let info = app.snapshot.as_ref().map(|s| &s.status.info);
    let o = observation(app);
    let name = na(info.and_then(|i| i.workload.clone()).or_else(|| info.and_then(|i| i.model.clone())));
    let state = if workload_state(app) == Some("starting") {
        bold("→ STARTING", WARNING)
    } else {
        match o.and_then(|o| flag(o, "inference_probe_ok")) {
            Some(true) => bold("✓ HEALTHY", HEALTHY),
            Some(false) => bold("✗ NOT ANSWERING", CRITICAL),
            None => muted("○ NO READING YET"),
        }
    };
    let mut head = vec![plain(" "), bold(name.clone(), theme::TEXT), plain("   "), state];
    if let Some(model) = info.and_then(|i| i.model.clone()).filter(|m| *m != name) {
        head.push(muted(format!("   {model}")));
    }
    // a failed probe's duration is a timeout, not a measurement: only a probe that answered has one
    let probe = o.filter(|o| flag(o, "inference_probe_ok") == Some(true))
        .and_then(|o| num(o, "inference_probe_latency_ms")).map(|m| format!("{m:.0} ms"));
    let rpm = app.rates.requests_per_min.map(|r| format!("{r:.0}/min"));
    let lat = app.rates.latency_ms.map(|l| format!("{l:.0} ms"));
    let used = o.and_then(|o| num(o, "gpu_memory_used_bytes"));
    let total = o.and_then(|o| num(o, "gpu_memory_total_bytes")).filter(|t| *t > 0.0);
    let temp = o.and_then(|o| num(o, "gpu_temperature_c"));
    let util = o.and_then(|o| num(o, "gpu_utilization_percent"));
    let memory = match (used, total) {
        (Some(u), Some(t)) => format!("{:.0}% of {} GiB", u / t * 100.0, gib(t)),
        _ => "N/A".into(),
    };
    vec![
        Line::from(head),
        kv("Requests", vec![plain(format!("{}   Mean latency {}   Probe {}", na(rpm), na(lat), na(probe)))]),
        kv("GPU", vec![plain(format!(
            "Memory {memory}   Temp {}   Busy {}",
            na(temp.map(|t| format!("{t:.0}°C"))), na(util.map(|u| format!("{u:.0}%")))
        ))]),
    ]
}

fn autopilot_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let mut l = Vec::new();
    if matches!(app.conn, Conn::Offline { .. }) {
        l.push(Line::from(vec![plain(" "), bold("✗ NOT WATCHING", CRITICAL), plain("  the control plane cannot be reached")]));
        return l;
    }
    let rows = app.rows();
    let age = app.telemetry_age_secs().map(|a| format!("Last observation {} ago", human_age(a)));
    let mut head = vec![plain(" "), bold("✓ WATCHING", HEALTHY)];
    if app.telemetry_stale() || app.is_stale() {
        head = vec![plain(" "), bold("! WATCHING, BUT THE DATA IS OLD", WARNING)];
    }
    if let Some(a) = age {
        head.push(muted(format!("   {a}")));
    }
    l.push(Line::from(head));
    if rows.is_empty() {
        l.push(Line::from(" No active incidents."));
        if !app.practice {
            l.push(Line::from(muted(" Want to see the recovery loop?  [R] Run a recovery test")));
        }
    } else {
        l.extend(attention_rows(app, w));
        if rows.iter().any(|i| !is_closed(&i.status)) {
            l.push(Line::from(muted(" Enter  Review the selected incident")));
        }
    }
    l.push(blank());
    l.push(pipeline_line(&pipeline::stages(focus_incident(app), observing(app)), w));
    l
}

fn quick_actions(app: &App) -> Line<'static> {
    let offer = app.offer();
    let mut spans = vec![plain(" ")];
    let mut item = |key: &str, label: &str| {
        spans.push(bold(format!("[{key}] "), ACCENT));
        spans.push(plain(format!("{label}   ")));
    };
    item("R", if app.practice { "Run the test again" } else { "Run a recovery test" });
    if offer.can_break {
        item("F", "Inject a real fault");
    }
    item("I", "View incidents");
    item("A", "View activity");
    Line::from(spans)
}

// ---- Incidents ------------------------------------------------------------------------------------

fn incident_list(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let rows = app.rows();
    if rows.is_empty() {
        let mut l = vec![
            blank(),
            Line::from(vec![plain(" "), bold("NO ACTIVE INCIDENTS", theme::TEXT)]),
            blank(),
            Line::from(" Autopilot is watching the inference workload."),
            blank(),
            Line::from(muted(" Want to see the recovery loop?")),
            Line::from(vec![plain(" "), bold("[R] ", ACCENT), plain("Run a recovery test")]),
        ];
        if no_workload(app) {
            l[3] = Line::from(" There is no managed workload to watch right now.");
        }
        return f.render_widget(Paragraph::new(l), area);
    }
    let mut lines = vec![blank()];
    for (idx, i) in rows.iter().enumerate() {
        let sel = idx == app.selected;
        let when = match i.status.as_str() {
            "RESOLVED" => format!("Recovered {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
            s if is_closed(s) => format!("Closed {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
            _ => format!("Detected {}", ago(app, &i.timeline.first().map_or(String::new(), |t| t.at.clone()))),
        };
        let head = vec![
            if sel { bold(" › ", ACCENT) } else { plain("   ") },
            badge_span(&i.status),
            plain(format!("   {}", words::problem_title(&i.category))),
        ];
        let tail = vec![muted(format!("{} · {when}", i.incident_id)), plain(" ")];
        let row = split_row(head, tail, w);
        lines.push(if sel { highlighted(row.spans, w) } else { row });
        let diagnosis = i.rca.as_ref().and_then(|r| r.root_cause.as_ref()).map(|c| c.statement.clone()).filter(|s| !s.is_empty());
        match (i.status.as_str(), diagnosis) {
            ("INSUFFICIENT_EVIDENCE", _) => lines.push(Line::from(muted("     Not enough evidence to name a cause."))),
            (_, Some(d)) => lines.push(Line::from(muted(format!("     Diagnosis: {d}")))),
            _ => {}
        }
        let proposal = i.proposal.as_ref().map(|p| words::action_phrase(&p.action));
        let detail = match (i.status.as_str(), proposal) {
            ("PROPOSED" | "POLICY_CHECK", Some(p)) => Some(format!("Action: {p} · Approval: REQUIRED")),
            ("RESOLVED", Some(p)) => Some(format!("Action: {p} · Approved · Recovery verified")),
            ("REJECTED", Some(p)) => Some(format!("Action: {p} · You declined it")),
            ("CLEARED", Some(p)) => Some(format!("Action: {p} · Not needed: the problem went away")),
            ("UNRESOLVED", Some(p)) => Some(format!("Action: {p} · Recovery could not be verified")),
            (_, Some(p)) => Some(format!("Action: {p}")),
            _ => None,
        };
        if let Some(d) = detail {
            lines.push(Line::from(muted(format!("     {d}"))));
        }
        lines.push(blank());
    }
    lines.push(Line::from(muted(" Enter  Review the selected incident   D  Technical details")));
    f.render_widget(Paragraph::new(lines), area);
}

// ---- Lab --------------------------------------------------------------------------------------------

/// Where the next step is, in words, for the line under a story step.
fn lab(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let mut l = vec![blank()];
    if app.practice {
        l.extend(story_lines(app, w));
    } else if let Some(fault) = app.active_fault() {
        l.extend(real_fault_lines(app, fault, w));
    } else {
        l.extend(lab_menu(app, w));
    }
    f.render_widget(Paragraph::new(l).scroll((app.scroll, 0)), area);
}

fn lab_menu(app: &App, w: usize) -> Vec<Line<'static>> {
    let offer = app.offer();
    let mut l = vec![
        Line::from(vec![plain(" "), bold("LAB", theme::TEXT), muted("   Experience the recovery loop before you need it")]),
        blank(),
        rule("Recovery tests", Some(muted("safe · simulated · nothing real is touched")), w),
        Line::from(vec![plain(" "), bold("[R] ", ACCENT), bold("Model becomes unresponsive", theme::TEXT), muted("   Run")]),
        Line::from(muted("     The model stops answering. Autopilot detects it, finds the cause, proposes a")),
        Line::from(muted("     restart, waits for your OK, restarts, and checks the model really recovered.")),
        blank(),
        rule("Real infrastructure", Some(span("affects the running workload", WARNING)), w),
    ];
    if offer.can_break {
        l.push(Line::from(vec![plain(" "), bold("[F] ", ACCENT), bold("Pause the running workload", theme::TEXT), muted("   Inject")]));
        l.push(Line::from(muted("     Pauses the managed model server for up to two minutes so it stops answering.")));
        l.push(Line::from(muted("     It always resumes by itself. Asks you to confirm first.")));
    } else {
        l.push(Line::from(vec![plain(" "), muted("[F] Pause the running workload")]));
        let why = match (&app.conn, workload_state(app), app.snapshot.as_ref().and_then(|s| s.status.faults.as_ref())) {
            (Conn::Online, _, None) => "this control plane does not offer real faults",
            (Conn::Online, Some("absent" | "stopped"), _) => "there is no running workload to pause",
            (Conn::Online, _, Some(f)) if !f.available => "this control plane does not offer real faults",
            (Conn::Online, _, _) => "not available right now",
            _ => "the control plane cannot be reached",
        };
        l.push(Line::from(muted(format!("     Not available: {why}."))));
    }
    l.push(blank());
    l.push(rule("The loop", None, w));
    l.push(pipeline_line(&pipeline::stages(None, observing(app)), w));
    l
}

/// The real fault in progress: what is happening and what happens next, from the server's report.
fn real_fault_lines(app: &App, fault: &crate::model::ActiveFault, w: usize) -> Vec<Line<'static>> {
    let left = (fault.expires_at - app.wall).max(0.0) as u64;
    let mut l = vec![
        split_row(
            vec![plain(" "), bold("REAL FAULT IN PROGRESS", CRITICAL)],
            vec![bold("LIVE", CRITICAL), plain(" ")],
            w,
        ),
        Line::from(muted(" The managed workload is paused on purpose. It resumes by itself, whatever happens here.")),
        blank(),
        pipeline_line(&pipeline::stages(focus_incident(app), observing(app)), w),
        blank(),
    ];
    l.extend(step("✓", HEALTHY, None, "Fault injected",
                  &format!("The workload is paused; it resumes by itself in {}:{:02}.", left / 60, left % 60), w));
    l.extend(incident_steps(app, w));
    l
}

const STEP_INDENT: usize = 33;        // " ✓ 15:02:11  " + a 20-column title

/// One step of the story: `✓ 15:02:11  Detected            what it means`, the explanation wrapped
/// beside the title so the titles line up down the page.
fn step(glyph: &str, color: Color, time: Option<String>, title: &str, detail: &str, w: usize) -> Vec<Line<'static>> {
    let head = |first: Option<&str>| {
        let mut spans = vec![plain(" "), bold(glyph.to_string(), color), plain(" ")];
        spans.push(match &time {
            Some(t) => muted(format!("{t}  ")),
            None => muted("          "),
        });
        spans.push(bold(format!("{title:<20}"), theme::TEXT));
        if let Some(text) = first {
            spans.push(muted(text.to_string()));
        }
        Line::from(spans)
    };
    let room = w.saturating_sub(STEP_INDENT + 1).max(20);
    let parts = wrap_words(detail, room);
    let mut lines = vec![head(parts.first().map(|p| p.as_str()))];
    for part in parts.iter().skip(1) {
        lines.push(Line::from(muted(format!("{}{part}", " ".repeat(STEP_INDENT)))));
    }
    lines
}

fn note(text: &str, w: usize) -> Vec<Line<'static>> {
    wrap_words(text, w.saturating_sub(STEP_INDENT + 1).max(20))
        .into_iter()
        .map(|p| Line::from(muted(format!("{}{p}", " ".repeat(STEP_INDENT)))))
        .collect()
}

/// The recovery test as a story. Every line comes from what the server reported; a step that has
/// not happened yet is shown as not run, never as done. SIMULATION is in the header and the banner.
fn story_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let stage = app.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.clone());
    let mut l = vec![
        Line::from(vec![plain(" "), bold("RECOVERY TEST", theme::TEXT), muted("   Model becomes unresponsive")]),
        blank(),
        pipeline_line(&pipeline::stages(focus_incident(app), true), w),
        blank(),
    ];
    let Some(stage) = stage else {
        l.push(Line::from(muted(" Preparing the recovery test…")));
        return l;
    };
    l.extend(step("✓", HEALTHY, None, "Test started", "The simulated model is healthy.", w));
    match (stage.as_str(), focus_incident(app)) {
        ("healthy", _) => {
            l.extend(step("→", ACCENT, None, "Injecting the fault", "Asking the server to make the model stop answering…", w));
        }
        ("detecting", None) => {
            l.extend(step("✓", HEALTHY, None, "Fault injected", "The simulated model stopped answering test requests.", w));
            l.extend(step("→", ACCENT, None, "Detecting", "Autopilot needs the problem on consecutive checks before it opens an incident.", w));
        }
        _ => {
            l.extend(step("✓", HEALTHY, None, "Fault injected", "The simulated model stopped answering test requests.", w));
            l.extend(incident_steps(app, w));
        }
    }
    l.push(blank());
    let finished = focus_incident(app).is_some_and(|i| is_closed(&i.status));
    l.push(Line::from(muted(if finished {
        " R  Run it again   Enter  View the incident   Esc  Leave the test"
    } else {
        " Enter  Review the incident   Esc  Leave the test"
    })));
    l
}

/// Detected → diagnosis → proposal → decision → recovery → verification, from the incident the
/// server holds. Times are the server's own (the incident timeline).
fn incident_steps(app: &App, w: usize) -> Vec<Line<'static>> {
    let Some(i) = focus_incident(app) else { return Vec::new() };
    let mut l = Vec::new();
    let status = i.status.as_str();
    l.extend(step("✓", HEALTHY, clock_of(i, &["DETECTED", "TRIAGING"]), "Detected",
                  &format!("{} ({})", words::problem_title(&i.category), i.incident_id), w));
    match status {
        "DETECTED" | "TRIAGING" => {
            l.extend(step("→", ACCENT, None, "Diagnosis", "Collecting evidence…", w));
            return l;
        }
        "INSUFFICIENT_EVIDENCE" => {
            l.extend(step("✗", WARNING, clock_of(i, &["DIAGNOSED"]), "Diagnosis", "Not enough evidence to name a cause. Nothing is proposed.", w));
            return l;
        }
        _ => {}
    }
    // the cause statement arrives with the full incident, a poll after the list
    let diagnosis = i.rca.as_ref().and_then(|r| r.root_cause.as_ref()).map(|c| c.statement.clone());
    l.extend(step("✓", HEALTHY, clock_of(i, &["DIAGNOSED"]), "Diagnosis",
                  &diagnosis.unwrap_or_else(|| "Loading the details…".into()), w));
    let action = match i.proposal.as_ref().map(|p| p.action.as_str()).or_else(|| app.proposed_action(&i.incident_id)) {
        Some(a) => words::action_phrase(a),
        None => "A recovery action".to_string(),
    };
    if matches!(status, "PROPOSED" | "POLICY_CHECK") {
        l.extend(step("!", WARNING, clock_of(i, &["PROPOSED", "POLICY_CHECK"]), "Recovery proposed", &action, w));
        for e in i.evidence.iter().filter(|e| e.relation == "supports") {
            if let Some(sentence) = words::evidence_sentence(e) {
                l.extend(note(&format!("• {sentence}"), w));
            }
        }
        l.extend(note("Restart is an allowlisted recovery action; it runs only after you approve.", w));
        l.push(Line::from(vec![plain(" ".repeat(STEP_INDENT)), bold("Needs your OK", WARNING), muted("   Enter  Review and decide")]));
        return l;
    }
    l.extend(step("✓", HEALTHY, clock_of(i, &["PROPOSED", "POLICY_CHECK"]), "Recovery proposed", &action, w));
    match status {
        "REJECTED" => {
            l.extend(step("✗", theme::TEXT_MUTED, clock_of(i, &["REJECTED"]), "You declined", "Nothing was restarted.", w));
            return l;
        }
        "CLEARED" => {
            l.extend(step("–", theme::TEXT_MUTED, clock_of(i, &["CLEARED"]), "No longer needed",
                          "The problem went away before it was acted on. Nothing was restarted.", w));
            return l;
        }
        _ => {}
    }
    l.extend(step("✓", HEALTHY, clock_of(i, &["APPROVED"]), "You approved", "", w));
    if matches!(status, "APPROVED" | "EXECUTING") {
        l.extend(step("→", ACCENT, clock_of(i, &["EXECUTING"]), "Recovering", "The server is restarting the workload…", w));
        return l;
    }
    if status == "EXECUTION_FAILED" {
        l.extend(step("✗", CRITICAL, clock_of(i, &["EXECUTION_FAILED"]), "Recovery failed", "The restart could not be carried out.", w));
        return l;
    }
    l.extend(step("✓", HEALTHY, clock_of(i, &["EXECUTING"]), "Recovered", "The workload was restarted.", w));
    if status == "VERIFYING" {
        l.extend(step("→", ACCENT, clock_of(i, &["VERIFYING"]), "Verifying", "Waiting for the server's result.", w));
        return l;
    }
    let required = i.verification.as_ref().and_then(|v| v.required_completions);
    let checks = i.verification.as_ref().and_then(|v| v.checks.as_ref()).filter(|c| !c.is_empty());
    let (glyph, color, title) = if status == "RESOLVED" { ("✓", HEALTHY, "Verified") } else { ("✗", CRITICAL, "Verification failed") };
    l.extend(step(glyph, color, clock_of(i, &["RESOLVED", "UNRESOLVED"]), title, "", w));
    if let Some(checks) = checks {
        let mut keys: Vec<&String> = checks.keys().collect();
        keys.sort_by_key(|k| (CHECK_ORDER.iter().position(|c| c == k).unwrap_or(99), (*k).clone()));
        for k in keys {
            l.push(Line::from(vec![plain(" ".repeat(STEP_INDENT)), mark(Some(checks[k])), plain(format!(" {}", words::check_phrase(k, required)))]));
        }
    } else if status == "RESOLVED" {
        l.extend(note("Loading the recorded checks…", w));
    }
    l.push(blank());
    l.push(Line::from(vec![plain(" "), if status == "RESOLVED" { bold("INCIDENT RESOLVED", HEALTHY) } else { bold("NOT RESOLVED", CRITICAL) }]));
    l
}
