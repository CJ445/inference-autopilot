//! Rendering. Everything shown as infrastructure state comes from the server's data in `App`;
//! anything the server did not send is `N/A`. Colour only ever means state.
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::{Frame, Terminal};
use serde_json::Value;

use crate::app::{is_closed, App, Conn, Screen};
use crate::model::{num, flag, text, Evidence, Incident, Obj, Watchdog};
use crate::timeparse::clock_hms;

pub const MIN_W: u16 = 72;
pub const MIN_H: u16 = 18;

const OK: Color = Color::Green;
const WARN: Color = Color::Yellow;
const BAD: Color = Color::Red;
const ACCENT: Color = Color::Cyan;
const DIM: Color = Color::DarkGray;

fn fg(color: Color) -> Style {
    Style::default().fg(color)
}
fn span(text: impl Into<String>, color: Color) -> Span<'static> {
    Span::styled(text.into(), fg(color))
}
fn plain(text: impl Into<String>) -> Span<'static> {
    Span::raw(text.into())
}
fn mark(ok: Option<bool>) -> Span<'static> {
    match ok {
        Some(true) => span("✓", OK),
        Some(false) => span("✗", BAD),
        None => span("?", DIM),
    }
}
fn gib(bytes: f64) -> String {
    format!("{:.1}", bytes / 1_073_741_824.0)
}
fn na(value: Option<String>) -> String {
    value.unwrap_or_else(|| "N/A".to_string())
}

// ---- entry points ---------------------------------------------------------------------------

pub fn render(f: &mut Frame, app: &App) {
    let area = f.size();
    if area.width < MIN_W || area.height < MIN_H {
        let msg = format!("Terminal too small: need at least {MIN_W}x{MIN_H} (have {}x{})", area.width, area.height);
        f.render_widget(Paragraph::new(msg).style(fg(WARN)), area);
        return;
    }
    let parts = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(3), Constraint::Min(5), Constraint::Length(1)])
        .split(area);
    header(f, parts[0], app);
    match &app.screen {
        Screen::Dashboard => dashboard(f, parts[1], app),
        Screen::Incidents => incidents(f, parts[1], app),
        Screen::Detail(id) => detail(f, parts[1], app, id),
        Screen::Audit => audit(f, parts[1], app),
    }
    footer(f, parts[2], app);
    if app.confirm.is_some() {
        confirm_modal(f, area, app);
    } else if app.busy.is_some() {
        busy_modal(f, area, app);
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

// ---- header and footer ---------------------------------------------------------------------

fn header(f: &mut Frame, area: Rect, app: &App) {
    let mut spans: Vec<Span> = Vec::new();
    match &app.conn {
        Conn::Connecting => spans.push(span("… CONNECTING", WARN)),
        Conn::Offline { .. } => {
            spans.push(Span::styled("✗ CONTROL PLANE OFFLINE", fg(BAD).add_modifier(Modifier::BOLD)));
            spans.push(plain("  Retrying…"));
        }
        Conn::Online => {
            let health = app.snapshot.as_ref().map_or("N/A", |s| s.status.health.as_str());
            spans.push(if health == "HEALTHY" {
                span("● RUNNING", OK)
            } else {
                span(format!("! {health}"), WARN)
            });
        }
    }
    if let Some(s) = &app.snapshot {
        if app.is_stale() {                          // state first: it must never be truncated
            let age = app.data_age().map_or(0, |d| d.as_secs());
            spans.push(span(format!("  STALE {age}s"), WARN));
        }
        spans.push(plain(format!("   {}", na(s.status.info.profile.clone()))));
        let armed = s.status.watchdog.as_ref().map(|w| w.state == "armed");
        spans.push(plain("   Watchdog "));
        spans.push(match armed {
            Some(true) => span("✓", OK),
            _ => span("!", BAD),
        });
        spans.push(plain("   Audit "));
        spans.push(mark(s.status.audit.as_ref().map(|a| a.valid)));
        if app.telemetry_stale() {
            spans.push(span(format!("   telemetry stale ({:.0}s)", app.telemetry_age_secs().unwrap_or(0.0)), WARN));
        }
        if s.poll_ms > 1500 {
            spans.push(span(format!("   SLOW API ({:.1}s)", s.poll_ms as f64 / 1000.0), WARN));
        }
    } else {
        spans.push(plain("   Watchdog ?   Audit ?"));
    }
    if let Conn::Offline { error, .. } = &app.conn {
        spans.push(span(format!("   ({error})"), DIM));   // verbose detail last: it may be cut
    }
    let block = Block::default().borders(Borders::ALL).title(" INFERENCE AUTOPILOT ").border_style(fg(ACCENT));
    f.render_widget(Paragraph::new(Line::from(spans)).block(block), area);
}

fn footer(f: &mut Frame, area: Rect, app: &App) {
    let line = match app.active_notice() {
        Some(n) => Line::from(span(format!(" {}", n.text), if n.is_error { BAD } else { OK })),
        None => Line::from(span(
            " ↑↓ Navigate  Enter Inspect  A Approve  R Reject  Esc Back  S Refresh  Tab Screens  Q Quit",
            DIM,
        )),
    };
    f.render_widget(Paragraph::new(line), area);
}

// ---- dashboard -----------------------------------------------------------------------------

fn block(title: impl Into<String>) -> Block<'static> {
    Block::default().borders(Borders::ALL).title(title.into()).border_style(fg(DIM))
}

fn no_data(f: &mut Frame, area: Rect, app: &App) {
    let mut lines = vec![Line::from("")];
    match &app.conn {
        Conn::Offline { error, attempts, .. } => {
            lines.push(Line::from(Span::styled(
                " CONTROL PLANE OFFLINE", fg(BAD).add_modifier(Modifier::BOLD),
            )));
            lines.push(Line::from(format!(" {error}")));
            lines.push(Line::from(format!(" Retrying {} (attempt {attempts})…", app.url)));
            lines.push(Line::from(span(" Start it with `aiops start`; this screen will recover by itself.", DIM)));
        }
        _ => lines.push(Line::from(span(format!(" Connecting to {}…", app.url), WARN))),
    }
    f.render_widget(Paragraph::new(lines).block(block(" CONTROL PLANE ")), area);
}

fn dashboard(f: &mut Frame, area: Rect, app: &App) {
    if app.snapshot.is_none() {
        return no_data(f, area, app);
    }
    let parts = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(6), Constraint::Min(3), Constraint::Length(5)])
        .split(area);
    let top = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(45), Constraint::Percentage(55)])
        .split(parts[0]);
    gpu_panel(f, top[0], app);
    vllm_panel(f, top[1], app);
    incidents_panel(f, parts[1], app);
    workload_panel(f, parts[2], app);
}

fn observation(app: &App) -> Option<&Obj> {
    app.snapshot.as_ref()?.status.last_observation.as_ref()
}

fn gpu_panel(f: &mut Frame, area: Rect, app: &App) {
    let o = observation(app);
    let title = match o.and_then(|o| text(o, "gpu_uuid")) {
        Some(uuid) => format!(" GPU 0 · {} ", uuid.chars().take(17).collect::<String>()),
        None => " GPU N/A ".to_string(),
    };
    let used = o.and_then(|o| num(o, "gpu_memory_used_bytes"));
    let total = o.and_then(|o| num(o, "gpu_memory_total_bytes")).filter(|t| *t > 0.0);
    let vram = match (used, total) {
        (Some(u), Some(t)) => {
            let pct = u / t * 100.0;
            let filled = ((pct / 10.0).round() as usize).min(10);
            let colour = if pct < 70.0 { OK } else if pct < 85.0 { WARN } else { BAD };
            vec![
                plain(" VRAM  "),
                span(format!("{}{}", "█".repeat(filled), "░".repeat(10 - filled)), colour),
                plain(format!("  {pct:.1}%   {} / {} GiB", gib(u), gib(t))),
            ]
        }
        _ => vec![plain(" VRAM  N/A")],
    };
    let temp = o.and_then(|o| num(o, "gpu_temperature_c"));
    let util = o.and_then(|o| num(o, "gpu_utilization_percent"));
    let line2 = vec![
        plain(format!(" TEMP  {}", na(temp.map(|t| format!("{t:.0}°C"))))),
        plain(format!("   UTIL {}", na(util.map(|u| format!("{u:.0}%"))))),
    ];
    let dim = app.is_stale();
    let style = if dim { Style::default().add_modifier(Modifier::DIM) } else { Style::default() };
    f.render_widget(
        Paragraph::new(vec![Line::from(vram), Line::from(line2)]).style(style).block(block(title)),
        area,
    );
}

fn vllm_panel(f: &mut Frame, area: Rect, app: &App) {
    let info = app.snapshot.as_ref().map(|s| &s.status.info);
    let name = info.and_then(|i| i.model.clone().or_else(|| i.workload.clone()));
    let o = observation(app);
    let metrics = o.and_then(|o| flag(o, "vllm_metrics_available"));
    let metrics_span = match metrics {
        Some(true) => span("Metrics ✓", OK),
        Some(false) => span("Metrics ✗", BAD),
        None => span("Metrics N/A", DIM),
    };
    let line1 = match o.and_then(|o| flag(o, "inference_probe_ok")) {
        Some(true) => {
            let ms = o.and_then(|o| num(o, "inference_probe_latency_ms"));
            vec![
                span(" ● HEALTHY", OK),
                plain("   "),
                span(format!("Probe ✓ {}", na(ms.map(|m| format!("{m:.0} ms")))), OK),
                plain("   "),
                metrics_span,
            ]
        }
        Some(false) => {
            let err = o.and_then(|o| text(o, "inference_probe_error")).unwrap_or_else(|| "failed".into());
            vec![
                Span::styled(" ✗ UNRESPONSIVE", fg(BAD).add_modifier(Modifier::BOLD)),
                plain("   "),
                span(format!("Probe ✗ {err}"), BAD),
                plain("   "),
                metrics_span,
            ]
        }
        None => vec![span(" N/A   Probe N/A", DIM)],
    };
    let count = |key: &str| na(o.and_then(|o| num(o, key)).map(|v| format!("{v:.0}")));
    let rpm = app.rates.requests_per_min.map(|r| format!("{r:.0}"));
    let lat = app.rates.latency_ms.map(|l| format!("{l:.0} ms"));
    let line2 = format!(
        " Req/min {}   Latency {}   Running {}   Waiting {}",
        na(rpm), na(lat), count("vllm_requests_running"), count("vllm_requests_waiting")
    );
    let kv = na(o.and_then(|o| num(o, "vllm_kv_cache_usage")).map(|k| format!("{:.1}%", k * 100.0)));
    let line3 = format!(" KV cache {kv}   Tokens/s N/A");
    let style = if app.is_stale() { Style::default().add_modifier(Modifier::DIM) } else { Style::default() };
    f.render_widget(
        Paragraph::new(vec![Line::from(line1), Line::from(line2), Line::from(line3)])
            .style(style)
            .block(block(format!(" vLLM · {} ", na(name)))),
        area,
    );
}

pub fn stage_label(status: &str) -> String {
    match status {
        "DETECTED" | "TRIAGING" | "DIAGNOSED" => "Diagnosing",
        "PROPOSED" | "POLICY_CHECK" => "Awaiting approval",
        "APPROVED" => "Approved",
        "EXECUTING" => "Executing",
        "VERIFYING" => "Verifying",
        "RESOLVED" => "RESOLVED",
        "UNRESOLVED" => "UNRESOLVED",
        "EXECUTION_FAILED" => "Execution failed",
        "REJECTED" => "Rejected",
        "CLEARED" => "Cleared",
        "INSUFFICIENT_EVIDENCE" => "Insufficient evidence",
        other => other,
    }
    .to_string()
}

fn status_colour(status: &str) -> Color {
    match status {
        "RESOLVED" | "CLEARED" => OK,
        "UNRESOLVED" | "EXECUTION_FAILED" => BAD,
        "REJECTED" => DIM,
        _ => WARN,
    }
}

fn first_time(i: &Incident) -> String {
    i.timeline.first().map_or("--:--:--".into(), |t| clock_hms(&t.at))
}

fn last_time(i: &Incident) -> String {
    i.timeline.last().map_or("--:--:--".into(), |t| clock_hms(&t.at))
}

fn incidents_panel(f: &mut Frame, area: Rect, app: &App) {
    let rows = app.rows();
    let mut lines: Vec<Line> = Vec::new();
    let active: Vec<_> = rows.iter().enumerate().filter(|(_, i)| !is_closed(&i.status)).collect();
    let closed: Vec<_> = rows.iter().enumerate().filter(|(_, i)| is_closed(&i.status)).collect();
    let marker = |idx: usize| if idx == app.selected { span("> ", ACCENT) } else { plain("  ") };
    if app.snapshot.is_none() {
        lines.push(Line::from(span(" N/A", DIM)));
    } else if active.is_empty() {
        lines.push(Line::from(span(" No active incidents", OK)));
    }
    for (idx, i) in &active {
        let action = i.proposal.as_ref().map_or("-".to_string(), |p| p.action.clone());
        lines.push(Line::from(vec![
            plain(" "),
            marker(*idx),
            span(format!("{}  ", i.incident_id), ACCENT),
            span(format!("{}  ", i.category), WARN),
            span(format!("{}  ", stage_label(&i.status)), status_colour(&i.status)),
            plain(action),
        ]));
    }
    if !closed.is_empty() {
        lines.push(Line::from(span(" RECENT", DIM)));
        for (idx, i) in &closed {
            lines.push(Line::from(vec![
                plain(" "),
                marker(*idx),
                plain(format!("{}  {}  → ", last_time(i), i.category)),
                span(i.status.clone(), status_colour(&i.status)),
            ]));
        }
    }
    f.render_widget(Paragraph::new(lines).block(block(" INCIDENTS ")), area);
}

fn watchdog_badge(w: Option<&Watchdog>) -> (Span<'static>, Option<String>) {
    match w {
        Some(w) if w.state == "armed" => (span("● ARMED", OK), None),
        Some(w) => {
            let mut detail = w.state.clone();
            if let Some(r) = w.reason.as_ref().filter(|r| !r.is_empty()) {
                detail = format!("{detail}: {}", r.join(", "));
            }
            if let Some(e) = &w.error {
                detail = format!("{detail}: {e}");
            }
            (Span::styled("! UNAVAILABLE", fg(BAD).add_modifier(Modifier::BOLD)), Some(detail))
        }
        None => (
            Span::styled("! UNAVAILABLE", fg(BAD).add_modifier(Modifier::BOLD)),
            Some("not reported".to_string()),
        ),
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

fn workload_panel(f: &mut Frame, area: Rect, app: &App) {
    let s = app.snapshot.as_ref();
    let o = observation(app);
    let name = na(s.and_then(|s| s.status.info.workload.clone()));
    let w = s.and_then(|s| s.status.watchdog.as_ref());
    let line1 = Line::from(vec![
        plain(" Probe "),
        mark(o.and_then(|o| flag(o, "inference_probe_ok"))),
        plain("  GPU "),
        mark(o.and_then(|o| o.contains_key("gpu_uuid").then_some(true))), // absent = unknown, not failed
        plain("  Metrics "),
        mark(o.and_then(|o| flag(o, "vllm_metrics_available"))),
        plain("  Watchdog "),
        mark(w.map(|w| w.state == "armed")),
        plain("  Audit "),
        mark(s.and_then(|s| s.status.audit.as_ref().map(|a| a.valid))),
    ]);
    let (badge, detail) = watchdog_badge(w);
    let mut l2 = vec![plain(" WATCHDOG  "), badge];
    if let Some(d) = detail {
        l2.push(span(format!(" ({d})"), BAD));
    } else if let Some(w) = w {
        let pid = s.and_then(|s| s.status.info.pid);
        l2.push(plain("   Identity "));
        l2.push(mark(match (w.protected_pid, pid) {
            (Some(a), Some(b)) => Some(a == b),
            _ => None,
        }));
        l2.push(plain("   Budgets "));
        match budget_state(w) {
            Ok(()) => l2.push(span("✓", OK)),
            Err(over) if over.is_empty() => l2.push(span("?", DIM)),
            Err(over) => l2.push(span(format!("✗ {}", over.join(", ")), BAD)),
        }
    }
    let audit_line = match s.and_then(|s| s.status.audit.as_ref()) {
        Some(a) if a.valid => Line::from(span(format!(" AUDIT ✓ VERIFIED ({} events)", a.events), OK)),
        Some(_) => Line::from(Span::styled(" AUDIT ✗ INTEGRITY FAILURE", fg(BAD).add_modifier(Modifier::BOLD))),
        None => Line::from(span(" AUDIT N/A", DIM)),
    };
    f.render_widget(
        Paragraph::new(vec![line1, Line::from(l2), audit_line]).block(block(format!(" WORKLOAD · {name} "))),
        area,
    );
}

// ---- incidents screen ----------------------------------------------------------------------

fn incidents(f: &mut Frame, area: Rect, app: &App) {
    let rows = app.rows();
    let mut lines = vec![Line::from(span(
        format!(
            "   {:<10} {:<24} {:<22} {:<9} {:<9} {:<18} {}",
            "ID", "CATEGORY", "STATE", "WORKLOAD", "CREATED", "PROPOSAL", "REMEDIATION"
        ),
        DIM,
    ))];
    if rows.is_empty() {
        lines.push(Line::from(span(" No incidents", DIM)));
    }
    for (idx, i) in rows.iter().enumerate() {
        let selected = idx == app.selected;
        let action = i.proposal.as_ref().map_or("-".to_string(), |p| p.action.clone());
        let row = format!(
            " {} {:<10} {:<24} {:<22} {:<9} {:<9} {:<18} {}",
            if selected { ">" } else { " " },
            i.incident_id, i.category, i.status,
            i.service.clone().unwrap_or_else(|| "-".into()), first_time(i), action, stage_label(&i.status)
        );
        let style = if selected { fg(ACCENT).add_modifier(Modifier::BOLD) } else { fg(status_colour(&i.status)) };
        lines.push(Line::from(Span::styled(row, style)));
    }
    f.render_widget(Paragraph::new(lines).block(block(" INCIDENTS ")), area);
}

// ---- incident detail -----------------------------------------------------------------------

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

fn evidence_summary(e: &Evidence) -> String {
    let compact = |v: &Value| {
        let s = v.to_string();
        if s.chars().count() > 40 { format!("{}…", s.chars().take(40).collect::<String>()) } else { s }
    };
    match (e.metric.as_str(), &e.value) {
        ("inference_probe", Value::Object(o)) => {
            if o.get("ok").and_then(Value::as_bool) == Some(false) {
                format!("inference failed ({})", o.get("error").and_then(Value::as_str).unwrap_or("no detail"))
            } else {
                format!("inference ok ({:.0} ms)", o.get("latency_ms").and_then(Value::as_f64).unwrap_or(0.0))
            }
        }
        ("vllm_metrics", Value::Object(o)) => {
            if o.get("available").and_then(Value::as_bool) == Some(true) {
                "metrics readable".into()
            } else {
                "metrics unavailable".into()
            }
        }
        ("gpu_memory_used_bytes", Value::Number(n)) => {
            format!("GPU memory used {} GiB", gib(n.as_f64().unwrap_or(0.0)))
        }
        (metric, v) => format!("{metric} {}", compact(v)),
    }
}

fn heading(title: &str) -> Line<'static> {
    Line::from(Span::styled(title.to_string(), fg(ACCENT).add_modifier(Modifier::BOLD)))
}

fn detail_lines(i: &Incident) -> Vec<Line<'static>> {
    let mut l: Vec<Line> = Vec::new();
    l.push(Line::from(vec![
        Span::styled(i.category.clone(), fg(WARN).add_modifier(Modifier::BOLD)),
        plain("   "),
        span(i.status.clone(), status_colour(&i.status)),
    ]));
    l.push(Line::from(format!("Workload  {}", i.service.clone().unwrap_or_else(|| "N/A".into()))));
    l.push(Line::from(format!("Created   {}", first_time(i))));
    l.push(Line::from(""));

    l.push(heading("EVIDENCE"));
    if i.evidence.is_empty() {
        l.push(Line::from(span(" none recorded", DIM)));
    }
    for e in &i.evidence {
        let sym = match e.relation.as_str() {
            "supports" => span("✓", OK),
            "contradicts" => span("✗", BAD),
            _ => span("·", DIM),
        };
        l.push(Line::from(vec![plain(" "), sym, plain(format!(" {:<14} {}", e.source, evidence_summary(e)))]));
    }
    l.push(Line::from(""));

    l.push(heading("RCA"));
    match &i.rca {
        Some(r) if r.insufficient_evidence || r.root_cause.is_none() => {
            l.push(Line::from(" Insufficient evidence to determine a root cause."));
        }
        Some(r) => l.push(Line::from(format!(" {}", r.root_cause.as_ref().map_or("", |c| c.statement.as_str())))),
        None => l.push(Line::from(span(" N/A", DIM))),
    }
    l.push(Line::from(""));

    l.push(heading("PROPOSAL"));
    match &i.proposal {
        Some(p) => {
            let params: Vec<String> = p
                .parameters
                .iter()
                .map(|(k, v)| format!("{k}={}", v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())))
                .collect();
            l.push(Line::from(format!(" {}  {}", p.action, params.join(" "))));
        }
        None => l.push(Line::from(span(" No proposal", DIM))),
    }
    l.push(Line::from(""));

    l.push(heading("POLICY"));
    let (policy, colour) = match i.status.as_str() {
        "POLICY_CHECK" | "PROPOSED" if i.proposal.is_some() => ("AWAITING APPROVAL", WARN),
        "REJECTED" => ("REJECTED", DIM),
        "APPROVED" | "EXECUTING" | "VERIFYING" | "RESOLVED" | "UNRESOLVED" | "EXECUTION_FAILED" => ("APPROVED", OK),
        _ => ("NOT APPLICABLE", DIM),
    };
    l.push(Line::from(span(format!(" {policy}"), colour)));
    l.push(Line::from(""));

    if let Some(r) = &i.remediation {
        l.push(heading("REMEDIATION"));
        let extra = r.error.as_ref().map(|e| format!(" ({})", e.as_str().unwrap_or("see audit"))).unwrap_or_default();
        l.push(Line::from(format!(" {}{extra}", r.state)));
        l.push(Line::from(""));
    }

    let checks = i.verification.as_ref().and_then(|v| v.checks.as_ref()).filter(|c| !c.is_empty());
    if let Some(checks) = checks {
        l.push(heading("RECOVERY"));
        let mut keys: Vec<&String> = checks.keys().collect();
        keys.sort_by_key(|k| (CHECK_ORDER.iter().position(|c| c == k).unwrap_or(99), (*k).clone()));
        for k in keys {
            l.push(Line::from(vec![plain(" "), mark(Some(checks[k])), plain(format!(" {}", check_label(k)))]));
        }
        l.push(Line::from(""));
        l.push(heading("RESULT"));
        l.push(Line::from(Span::styled(
            format!(" {}", i.status),
            fg(status_colour(&i.status)).add_modifier(Modifier::BOLD),
        )));
    } else {
        l.push(heading("VERIFICATION"));
        l.push(Line::from(match i.status.as_str() {
            "VERIFYING" => " In progress (waiting for the server's result)",
            "APPROVED" | "EXECUTING" => " Not started: the remediation is running",
            _ => " Not started",
        }));
    }
    l.push(Line::from(""));

    l.push(heading("TIMELINE"));
    for t in &i.timeline {
        l.push(Line::from(format!("{}  {}", clock_hms(&t.at), t.state)));
    }
    l
}

fn detail(f: &mut Frame, area: Rect, app: &App, id: &str) {
    let title = format!(" INCIDENT {id} ");
    let lines = match app.selected_incident() {
        Some(i) => detail_lines(i),
        None => vec![Line::from(span("This incident is no longer reported by the control plane (Esc to go back).", WARN))],
    };
    f.render_widget(Paragraph::new(lines).scroll((app.scroll, 0)).block(block(title)), area);
}

// ---- audit ---------------------------------------------------------------------------------

fn audit(f: &mut Frame, area: Rect, app: &App) {
    let s = app.snapshot.as_ref();
    let fetched = s.and_then(|s| s.audit.as_ref());
    let valid = fetched.map(|a| a.valid).or_else(|| s.and_then(|s| s.status.audit.as_ref().map(|a| a.valid)));
    let mut lines = vec![match valid {
        Some(true) => Line::from(span(" AUDIT ✓ VERIFIED", OK)),
        Some(false) => Line::from(Span::styled(" AUDIT ✗ INTEGRITY FAILURE", fg(BAD).add_modifier(Modifier::BOLD))),
        None => Line::from(span(" AUDIT N/A", DIM)),
    }];
    match fetched {
        None => lines.push(Line::from(span(" waiting for the audit log…", DIM))),
        Some(a) => {
            let width = area.width.saturating_sub(48) as usize;
            for e in &a.events {
                let data = e.data.to_string();
                let data: String = data.chars().take(width.max(10)).collect();
                lines.push(Line::from(format!(" {:>4}  {:<22} {:<12}  {data}", e.seq, e.event, e.hash)));
            }
        }
    }
    f.render_widget(Paragraph::new(lines).scroll((app.scroll, 0)).block(block(" AUDIT LOG ")), area);
}

// ---- modals --------------------------------------------------------------------------------

fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width);
    let h = h.min(area.height);
    Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h }
}

fn confirm_modal(f: &mut Frame, area: Rect, app: &App) {
    let Some(c) = &app.confirm else { return };
    let rect = centered(area, 56, 11);
    f.render_widget(Clear, rect);
    let lines = vec![
        Line::from(Span::styled(format!(" {} {}?", c.kind.verb(), c.action), fg(WARN).add_modifier(Modifier::BOLD))),
        Line::from(""),
        Line::from(" Workload:"),
        Line::from(format!(" {}", c.workload)),
        Line::from(""),
        Line::from(" Action:"),
        Line::from(format!(" {}", c.action)),
        Line::from(""),
        Line::from(span(" [Enter] Confirm   [Esc] Cancel", ACCENT)),
    ];
    f.render_widget(
        Paragraph::new(lines).block(Block::default().borders(Borders::ALL).title(" Confirm ").border_style(fg(WARN))),
        rect,
    );
}

fn busy_modal(f: &mut Frame, area: Rect, app: &App) {
    let Some(b) = &app.busy else { return };
    let rect = centered(area, 64, 7);
    f.render_widget(Clear, rect);
    let secs = app.now.saturating_duration_since(b.since).as_secs();
    let lines = vec![
        Line::from(Span::styled(" Waiting for the server…", fg(WARN).add_modifier(Modifier::BOLD))),
        Line::from(""),
        Line::from(format!(" {} {} sent ({secs}s).", b.kind.verb(), b.incident_id)),
        Line::from(" Remediation and verification can take a minute or two."),
        Line::from(" The server stays authoritative; this screen follows its state."),
    ];
    f.render_widget(
        Paragraph::new(lines).block(Block::default().borders(Borders::ALL).title(" In progress ").border_style(fg(WARN))),
        rect,
    );
}
