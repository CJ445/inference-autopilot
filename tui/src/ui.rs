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

use crate::app::{is_closed, ActionKind, App, Conn, Remote, Screen, CONFIRM_GUARD, KEYMAP, SCREEN_ORDER};
use crate::model::{flag, num, text, Check, Evidence, Incident, Obj, Watchdog};
use crate::theme::{self, ACCENT, BORDER, CRITICAL, HEALTHY, TEXT_MUTED, WARNING};
use crate::timeparse::{clock_hms, parse_rfc3339};

pub const MIN_W: u16 = 72;
pub const MIN_H: u16 = 18;
const SIDEBAR_W: u16 = 18;
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

fn dimmed(lines: Vec<Line<'static>>) -> Vec<Line<'static>> {
    lines
        .into_iter()
        .map(|l| {
            Line::from(
                l.spans
                    .into_iter()
                    .map(|s| Span::styled(s.content, s.style.add_modifier(Modifier::DIM)))
                    .collect::<Vec<_>>(),
            )
        })
        .collect()
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
    let body = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(SIDEBAR_W), Constraint::Min(20)])
        .split(main);
    sidebar(f, body[0], app);
    // An approve/reject in flight is shown inline, above the screen it was started from.
    let pane = if app.busy.is_some() && body[1].height > 8 {
        let split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(3), Constraint::Min(1)])
            .split(body[1]);
        progress_banner(f, split[0], app);
        split[1]
    } else {
        body[1]
    };
    match &app.screen {
        Screen::Dashboard => overview(f, pane, app),
        Screen::Incidents => incidents(f, pane, app),
        Screen::Detail(id) => detail(f, pane, app, id),
        Screen::Audit => audit(f, pane, app),
        Screen::ControlPlane => control_plane(f, pane, app),
        Screen::Settings => settings(f, pane, app),
        Screen::Diagnostics => diagnostics(f, pane, app),
        Screen::About => about(f, pane, app),
        Screen::Help => help(f, pane, app),
    }
    footer(f, foot, app);
    if app.confirm.is_some() {
        confirm_modal(f, area, app);
    } else if app.stop_confirm {
        stop_modal(f, area, app);
    } else if app.fault_confirm {
        fault_modal(f, area, app);
    } else if app.palette.is_some() {
        palette_modal(f, area, app);
    }
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
        Conn::Online => {
            let health = app.snapshot.as_ref().map_or("N/A", |s| s.status.health.as_str());
            if health == "HEALTHY" {
                span("● CONTROL ONLINE", HEALTHY)
            } else {
                bold(format!("! {health}"), WARNING)
            }
        }
    };
    let title = split_row(vec![plain(" "), bold("INFERENCE AUTOPILOT", ACCENT)], vec![status], w);

    let mut chips: Vec<Span> = vec![plain(" ")];
    if let Some(s) = &app.snapshot {
        if app.is_stale() {                          // state first: it must never be truncated
            let age = app.data_age().map_or(0, |d| d.as_secs());
            chips.push(bold(format!("STALE {age}s  "), WARNING));
        }
        if s.status.mode.as_deref() == Some("SIMULATION") {
            // a simulated session has no real watchdog or telemetry to report on
            chips.push(muted("simulated session"));
        } else {
            chips.push(muted(format!("{}   ", na(s.status.info.profile.clone()))));
            let armed = s.status.watchdog.as_ref().map(|w| w.state == "armed");
            chips.push(muted("Watchdog "));
            chips.push(match armed {
                Some(true) => span("✓", HEALTHY),
                _ => span("!", CRITICAL),
            });
            chips.push(muted("   Audit "));
            chips.push(mark(s.status.audit.as_ref().map(|a| a.valid)));
            if app.telemetry_stale() {
                chips.push(span(format!("   telemetry stale ({:.0}s)", app.telemetry_age_secs().unwrap_or(0.0)), WARNING));
            }
            if s.poll_ms > 1500 {
                chips.push(span(format!("   SLOW API ({:.1}s)", s.poll_ms as f64 / 1000.0), WARNING));
            }
        }
    } else {
        chips.push(muted("Watchdog ?   Audit ?"));
    }
    if let Conn::Offline { error, .. } = &app.conn {
        chips.push(muted(format!("   Retrying… ({error})")));   // verbose detail last: it may be cut
    }
    let block = Block::default().borders(Borders::BOTTOM).border_style(fg(BORDER));
    f.render_widget(Paragraph::new(vec![title, Line::from(chips)]).block(block), area);
}

fn sidebar(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width.saturating_sub(1) as usize; // the right border takes a column
    let here = match &app.screen {
        Screen::Detail(_) => &Screen::Incidents,
        other => other,
    };
    let active = SCREEN_ORDER.iter().position(|s| s == here).unwrap_or(0);
    let open = app.rows().iter().filter(|i| !is_closed(&i.status)).count();
    let roomy = area.height >= 16;          // blank rows between items only when there is room
    let item = |idx: usize, label: &str, upper: bool| {
        let label = if upper { label.to_uppercase() } else { label.to_string() };
        let mut spans = if idx == active {
            vec![bold(" ▌ ", ACCENT), bold(label, ACCENT)]
        } else {
            vec![plain("   "), plain(label)]
        };
        if idx == 1 && open > 0 {
            spans.push(bold(format!("  {open}"), WARNING)); // the open-incident count, nothing else
        }
        if idx == active { highlighted(spans, w) } else { Line::from(spans) }
    };
    let group = |name: &str| Line::from(muted(format!(" {name}")));
    let mut lines = Vec::new();
    if roomy {
        lines.push(blank());
    }
    for (idx, label) in ["Overview", "Incidents", "Audit"].into_iter().enumerate() {
        lines.push(item(idx, label, true));
        if roomy {
            lines.push(blank());
        }
    }
    lines.push(hline(w + 1));
    lines.push(group("SYSTEM"));
    lines.push(item(3, "Control Plane", false));
    if roomy {
        lines.push(blank());
    }
    lines.push(group("OPERATOR"));
    for (idx, label) in [(4, "Settings"), (5, "Diagnostics"), (6, "About"), (7, "Help")] {
        lines.push(item(idx, label, false));
    }
    let block = Block::default().borders(Borders::RIGHT).border_style(fg(BORDER));
    f.render_widget(Paragraph::new(lines).block(block), area);
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
            // (key, label, active, keep-priority): when the row is too narrow the lowest
            // priorities go first, so Quit is never the one that falls off the edge.
            let mut hints = vec![
                ("↑↓", "Navigate", true, 5), ("Enter", "Inspect", true, 6), ("A", "Approve", live, 7),
                ("R", "Reject", live, 7), ("Esc", "Back", true, 3), ("S", "Refresh", true, 4),
                ("←→", "Screens", true, 2), ("Ctrl+P", "Commands", true, 8), ("?", "Help", true, 1),
                ("Q", "Quit", true, 9),
            ];
            if app.practice {
                let healthy = app.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.as_str()) == Some("healthy");
                hints.push(("F", "Break it", healthy, 8));
                hints.push(("P", "Restart practice", true, 2));
                if app.screen == Screen::Dashboard {
                    hints.retain(|h| h.0 != "Esc");
                    hints.push(("Esc", "Leave practice", true, 6));
                }
            } else {
                hints.push(("P", "Practice", true, 4));
            }
            match app.screen {
                Screen::Diagnostics => hints.push(("D", "Run again", true, 7)),
                Screen::ControlPlane => hints.push(("X", "Stop", true, 7)),
                _ => {}
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
        _ if app.practice => lines.push(Line::from(span(" Preparing the practice session…", WARNING))),
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
    let stale = app.is_stale() || app.telemetry_stale();
    let section = |lines: Vec<Line<'static>>| if stale { dimmed(lines) } else { lines };
    l.extend(section(inference_section(app, w)));
    l.push(blank());
    l.extend(section(gpu_section(app, w)));
    l.push(blank());
    l.extend(safety_section(app, w));
    f.render_widget(Paragraph::new(l), area);
}

/// What to do next in the practice, from the stage the SERVER reports (never guessed here).
fn practice_guide(app: &App) -> Vec<Line<'static>> {
    let stage = app.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.as_str());
    let text = match stage {
        None => "Preparing the practice session…".to_string(),
        Some("healthy") => "Everything is healthy. Press F to break the model (simulated).".to_string(),
        Some("detecting") => "The model stopped answering. The detector needs two failed checks in a row before it opens an incident.".to_string(),
        Some("awaiting_approval") => "A restart was proposed. Open the incident (Enter), review it, then approve (A).".to_string(),
        Some("recovering") => "Restarting and verifying the recovery…".to_string(),
        Some("resolved") => "Recovered and verified (simulated). Press P to practice again, or Esc to leave.".to_string(),
        Some(other) => format!("The incident closed as {other}. Press P to practice again, or Esc to leave."),
    };
    vec![Line::from(vec![plain(" "), bold("Practice  ", WARNING), plain(text)])]
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
    let style = Style::default().fg(Color::Black).bg(CRITICAL).add_modifier(Modifier::BOLD);
    // The facts always fit; the hint for ending it sooner is added only when there is room.
    let base = format!(" LIVE · GPU-REAL · FAULT ACTIVE   The real workload is paused; it {when}.");
    let full = format!("{base}  Ctrl+P → Resume the workload now");
    let text = if full.chars().count() <= area.width as usize { full } else { base };
    let pad = " ".repeat((area.width as usize).saturating_sub(text.chars().count()));
    f.render_widget(Paragraph::new(Line::from(Span::styled(format!("{text}{pad}"), style))), area);
}

/// The dialog that stands between the operator and pausing the REAL workload.
fn fault_modal(f: &mut Frame, area: Rect, app: &App) {
    let rect = centered(area, 70, 13);
    f.render_widget(Clear, rect);
    let minutes = crate::app::FAULT_SECONDS / 60;
    let target = app.snapshot.as_ref().and_then(|s| s.status.info.workload.clone()).unwrap_or_else(|| "N/A".into());
    let armed = app.now.saturating_duration_since(app.fault_opened) >= CONFIRM_GUARD;
    let lines = vec![
        Line::from(bold(" Pause the real workload?", WARNING)),
        Line::from(vec![plain(" "), bold("LIVE · GPU-REAL", CRITICAL)]),
        blank(),
        Line::from(" This will pause the real inference workload."),
        Line::from(" It will stop answering until it is automatically resumed"),
        Line::from(format!(" (in {minutes} minutes) or the recovery flow restarts it.")),
        Line::from(muted(" Nothing else is touched.")),
        blank(),
        row_kv("Workload", &target),
        blank(),
        Line::from(vec![
            plain(" "),
            if armed { span("[Enter] Confirm", ACCENT) } else { muted("[Enter] Confirm") },
            span("   [Esc] Cancel", ACCENT),
        ]),
    ];
    f.render_widget(Paragraph::new(lines).block(modal_block("Confirm", CRITICAL)), rect);
}

fn row_kv(label: &str, value: &str) -> Line<'static> {
    Line::from(vec![plain(" "), muted(format!("{label:<10}")), plain(value.to_string())])
}

/// The loud one-line label shown on every screen while a simulation is on screen.
fn practice_banner(f: &mut Frame, area: Rect, app: &App) {
    let real_system_screen = matches!(
        app.screen,
        Screen::ControlPlane | Screen::Settings | Screen::Diagnostics | Screen::About
    );
    let note = if real_system_screen {
        "These screens show the real system; the practice itself touches nothing real."
    } else {
        "Nothing here touches your GPU, containers, or real workload."
    };
    let style = Style::default().fg(Color::Black).bg(WARNING).add_modifier(Modifier::BOLD);
    let text = format!(" PRACTICE · SIMULATION   {note}");
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
            lines.push(Line::from(muted("   Press P to practice an incident (simulated; nothing real is touched)")));
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
            Stage::Skipped => Span::styled(name.to_string(), fg(BORDER).add_modifier(Modifier::CROSSED_OUT)),
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
        Some(i) => detail_lines(i, area.width as usize),
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

fn confirm_modal(f: &mut Frame, area: Rect, app: &App) {
    let Some(c) = &app.confirm else { return };
    let width: u16 = 74.min(area.width);
    let room = (width as usize).saturating_sub(16);
    let row = |label: &str, value: String| {
        Line::from(vec![plain(" "), muted(format!("{label:<10}")), plain(value)])
    };
    let mut lines = vec![Line::from(bold(format!(" {} {}?", c.kind.verb(), c.action), WARNING))];
    if c.practice {
        lines.push(Line::from(vec![plain(" "), bold("PRACTICE · SIMULATION", WARNING), muted("  nothing real is touched")]));
    }
    lines.extend(vec![
        blank(),
        row("Incident", format!("{}  {}", c.incident_id, c.category)),
        row("Workload", c.workload.clone()),
    ]);
    if c.why.is_empty() {
        lines.push(row("Why", "no RCA statement from the server".into()));
    } else {
        for (n, part) in wrap_words(&c.why, room).into_iter().enumerate() {
            lines.push(row(if n == 0 { "Why" } else { "" }, part));
        }
    }
    let effect = match (c.kind, c.action.as_str()) {
        (ActionKind::Reject, _) if c.practice => "No action is taken; the simulated proposal is closed.".to_string(),
        (ActionKind::Approve, _) if c.practice => "A simulated restart: nothing real is restarted.".to_string(),
        (ActionKind::Reject, _) => "No action is taken on the workload; the proposal is closed.".to_string(),
        (ActionKind::Approve, "restart_workload") => {
            "Restarts the workload: inference is unavailable until it recovers. There is no rollback.".to_string()
        }
        (ActionKind::Approve, _) => "The server executes the proposed action.".to_string(),
    };
    for (n, part) in wrap_words(&effect, room).into_iter().enumerate() {
        lines.push(row(if n == 0 { "Effect" } else { "" }, part));
    }
    lines.push(blank());
    let armed = app.now.saturating_duration_since(c.opened) >= CONFIRM_GUARD;
    lines.push(Line::from(vec![
        plain(" "),
        if armed { span("[Enter] Confirm", ACCENT) } else { muted("[Enter] Confirm") },
        span("   [Esc] Cancel", ACCENT),
    ]));
    let rect = centered(area, width, lines.len() as u16 + 2);
    f.render_widget(Clear, rect);
    f.render_widget(
        Paragraph::new(lines).block(modal_block("Confirm", theme::BORDER_FOCUSED)),
        rect,
    );
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

fn control_plane(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let mut l = vec![blank(), rule("Control plane", None, w)];
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
    f.render_widget(Paragraph::new(l).scroll((app.scroll, 0)), area);
}

fn settings(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let ms = |v: Option<u64>| v.map_or("N/A".to_string(), |n| format!("{n} ms"));
    let mut l = vec![
        blank(),
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
    f.render_widget(Paragraph::new(l).scroll((app.scroll, 0)), area);
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

fn diagnostics(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let note = match &app.diag {
        Remote::Ready(d) => d.duration_seconds.map(|s| muted(format!("ran in {s:.1}s"))),
        _ => None,
    };
    let mut l = vec![blank(), rule("Diagnostics", note, w)];
    match pending(&app.diag, app) {
        Some(line) => l.push(line),
        None => {
            let Remote::Ready(d) = &app.diag else { return };
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
    f.render_widget(Paragraph::new(l).scroll((app.scroll, 0)), area);
}

fn about(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let mut l = vec![
        blank(),
        Line::from(vec![plain(" "), bold("AIOPS", ACCENT)]),
        Line::from(muted(" Autonomous AIOps for LLM inference infrastructure")),
        blank(),
        rule("Operator UI", None, w),
        kv("Version", vec![plain(env!("CARGO_PKG_VERSION"))]),
        blank(),
        rule("Control plane", None, w),
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
    l.push(Line::from(muted(" Workload remediation: incident → policy → your approval (A) → the server")));
    l.push(Line::from(muted(" executes and verifies it. Stopping the control plane is separate (screen 4).")));
    l.push(blank());
    l.push(rule("Testing the real system", None, w));
    l.push(Line::from(muted(" Ctrl+P, then \"Break the real workload (pause)…\" asks first, pauses the real")));
    l.push(Line::from(muted(" workload for a bounded time and always resumes it. Use Practice (P) to try safely.")));
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
