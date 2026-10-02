//! The application shell: the header (where you are), the anchored bar (what to do next), the
//! footer (which keys work now, and what protects you), toasts and the workload panel.
//!
//! The anchored bar is the equivalent of an input line: it is where the eye already is, and it
//! always says, in a sentence, what the situation is and what the next move is.
use super::*;

const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/// A frame every 80 ms; `⋯` when motion is reduced.
pub(super) fn spinner(app: &App) -> &'static str {
    if app.reduce_motion {
        return "⋯";
    }
    let ms = app.now.saturating_duration_since(app.started).as_millis() as usize;
    SPINNER[(ms / 80) % SPINNER.len()]
}

fn surface(app: &App, color: Color) -> Style {
    if app.theme.has_surfaces() { Style::default().bg(color) } else { Style::default() }
}

// ---- header -----------------------------------------------------------------------------------

pub(super) fn header(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    let status = match &app.conn {
        Conn::Connecting => bold("… connecting", WARNING),
        Conn::Offline { .. } => bold("✗ offline", CRITICAL),
        Conn::Online if app.snapshot.is_none() => bold("… loading", WARNING),
        Conn::Online => {
            let health = app.snapshot.as_ref().map_or("N/A", |s| s.status.health.as_str());
            if health == "HEALTHY" { span("● online", HEALTHY) } else { bold(format!("! {}", health.to_lowercase()), WARNING) }
        }
    };
    let mut right = Vec::new();
    if app.snapshot.is_some() {
        right.push(mode_badge(app));
        right.push(plain("   "));
    }
    right.push(status);
    right.push(plain(" "));
    let title = split_row(vec![plain(" "), bold("INFERENCE AUTOPILOT", ACCENT)], right, w);

    // the spaces you can be in: the current one is marked with a glyph and a tone, so it does not rest on colour
    let open = app.rows().iter().filter(|i| !is_closed(&i.status)).count();
    let here = match &app.screen {
        Screen::Detail(_) => &Screen::Incidents,
        other => other,
    };
    let mut nav: Vec<Span> = vec![plain(" ")];
    for (screen, label) in [
        (Screen::Dashboard, "Overview"), (Screen::Incidents, "Incidents"), (Screen::Lab, "Lab"),
        (Screen::Audit, "Activity"), (Screen::System, "System"),
    ] {
        if &screen == here {
            nav.push(Span::styled(format!(" ▸ {label} "), surface(app, theme::ELEMENT).fg(theme::TEXT).add_modifier(Modifier::BOLD)));
        } else {
            nav.push(muted(format!("   {label} ")));
        }
        if screen == Screen::Incidents && open > 0 {
            nav.push(bold(format!("● {open}"), WARNING));
        }
    }
    if app.screen == Screen::Help {
        nav.push(Span::styled(" ▸ Help ", surface(app, theme::ELEMENT).fg(theme::TEXT).add_modifier(Modifier::BOLD)));
    }
    // warnings first, so a narrow row drops everything else before it drops a warning
    let mut warns: Vec<Vec<Span>> = Vec::new();
    if let Some(s) = &app.snapshot {
        if app.is_stale() {
            warns.push(vec![bold(format!("STALE {}", human_age(app.data_age().map_or(0, |d| d.as_secs()) as f64)), WARNING)]);
        }
        let real = s.status.mode.as_deref() != Some("SIMULATION");
        if real && app.telemetry_stale() {
            warns.push(vec![span(format!("telemetry stale ({})", human_age(app.telemetry_age_secs().unwrap_or(0.0))), WARNING)]);
        }
        if real && s.poll_ms > 1500 {
            warns.push(vec![span(format!("slow API ({:.1}s)", s.poll_ms as f64 / 1000.0), WARNING)]);
        }
        if app.details {
            warns.push(vec![muted(na(s.status.info.profile.clone()))]);
        }
    }
    if let Conn::Offline { error, .. } = &app.conn {
        warns.push(vec![muted(format!("retrying · {error}"))]);
    }
    let room = w.saturating_sub(width_of(&nav) + 2);
    let mut warn_spans: Vec<Span> = Vec::new();
    for chip in warns {
        if width_of(&warn_spans) + width_of(&chip) + 3 > room {
            break;
        }
        warn_spans.extend(chip);
        warn_spans.push(plain("   "));
    }
    f.render_widget(Paragraph::new(vec![title, split_row(nav, warn_spans, w)]), area);
}

// ---- the anchored bar -------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Debug)]
pub(super) enum Tone {
    Info,
    Active,
    Waiting,
    Good,
    Bad,
}

pub(super) struct Bar {
    pub tone: Tone,
    pub title: String,
    pub detail: String,
    pub spin: bool,
}

fn bar(tone: Tone, title: impl Into<String>, detail: impl Into<String>) -> Bar {
    Bar { tone, title: title.into(), detail: detail.into(), spin: tone == Tone::Active }
}

/// What the situation is and what to do next, from the server's state and nothing else.
pub(super) fn bar_for(app: &App) -> Bar {
    if matches!(app.conn, Conn::Offline { .. }) {
        return bar(Tone::Bad, "Cannot reach the control plane", "Retrying. Autopilot cannot watch until it is back.");
    }
    let Some(snap) = &app.snapshot else {
        return bar(Tone::Active, "Connecting", "Waiting for the first answer from the control plane.");
    };
    if let Some(b) = &app.busy {
        let secs = app.now.saturating_duration_since(b.since).as_secs();
        return bar(Tone::Active, "Waiting for the server…", format!("{} {} sent ({secs}s). Recovery and verification can take a minute or two.", b.kind.verb(), b.incident_id));
    }
    // the space you are in, when it is not about an incident
    match &app.screen {
        Screen::Incidents => return bar(Tone::Info, "Incidents", "Enter opens the selected incident. A and R only decide on an incident's own stream."),
        Screen::Audit => return bar(Tone::Info, "Activity", "Everything the system did and decided, oldest first. D shows the raw events."),
        Screen::System => return bar(Tone::Info, "System", "Read-only status. D runs the checks again; X asks to stop the control plane."),
        Screen::Help => return bar(Tone::Info, "Help", "Esc goes back."),
        Screen::Lab if !app.practice && app.active_fault().is_none() => {
            return bar(Tone::Info, "Recovery Lab", "Enter runs the selected test. Esc goes back.");
        }
        _ => {}
    }
    let incident = match &app.screen {
        Screen::Detail(id) => incident_by_id(app, id),
        _ => focus_incident(app),
    };
    let stage = snap.status.practice.as_ref().map(|p| p.stage.as_str());
    match incident {
        None if app.practice => match stage {
            Some("healthy") | None => bar(Tone::Active, "Starting the recovery test", "Asking the server to break the simulated model."),
            _ => bar(Tone::Active, "Detecting", "Autopilot needs the problem on consecutive checks before it opens an incident."),
        },
        None => match app.active_fault() {
            Some(fault) => {
                let left = (fault.expires_at - app.wall).max(0.0) as u64;
                bar(Tone::Waiting, "Real fault in progress", format!("The workload is paused; it resumes by itself in {}:{:02}. Autopilot is waiting to detect it.", left / 60, left % 60))
            }
            None if no_workload(app) => bar(Tone::Info, "Nothing to watch yet", "Autopilot will not start a workload for you. Start one and it is picked up."),
            None => bar(Tone::Info, "Autopilot is watching", "Run a recovery test to watch it detect, diagnose and recover."),
        },
        Some(i) => incident_bar(app, i),
    }
}

fn incident_bar(app: &App, i: &Incident) -> Bar {
    let loaded = app.snapshot.as_ref().and_then(|s| s.detail.as_ref()).is_some_and(|d| d.incident_id == i.incident_id);
    let action = i.proposal.as_ref().map(|p| words::action_phrase(&p.action)).or_else(|| app.proposed_action(&i.incident_id).map(words::action_phrase));
    match i.status.as_str() {
        "DETECTED" | "TRIAGING" | "DIAGNOSED" => bar(Tone::Active, "Diagnosing", "Collecting evidence."),
        "PROPOSED" | "POLICY_CHECK" if !loaded => bar(Tone::Active, "Loading the incident", "The decision needs the full evidence on screen."),
        "PROPOSED" | "POLICY_CHECK" => bar(
            Tone::Waiting,
            "Autopilot needs your decision",
            format!("{}? A approves, R rejects, E shows the evidence.", action.unwrap_or_else(|| "Recovery".into())),
        ),
        "APPROVED" | "EXECUTING" => bar(Tone::Active, "Recovering", "The server is restarting the workload."),
        "VERIFYING" => bar(Tone::Active, "Verifying recovery", "Waiting for the server's result."),
        "RESOLVED" => bar(Tone::Good, "Recovered and verified", if app.practice { "R runs the test again. Esc leaves it." } else { "This incident is closed." }),
        "UNRESOLVED" => bar(Tone::Bad, "Recovery was not verified", "The restart ran, but the checks did not all pass. Look at the evidence."),
        "EXECUTION_FAILED" => bar(Tone::Bad, "The recovery could not run", "Nothing was changed that the server could confirm."),
        "REJECTED" => bar(Tone::Info, "You declined the recovery", "Nothing was restarted."),
        "CLEARED" => bar(Tone::Info, "No longer needed", "The problem went away before it was acted on. Nothing was restarted."),
        "INSUFFICIENT_EVIDENCE" => bar(Tone::Waiting, "Not enough evidence", "Autopilot cannot name a cause, so it proposes nothing."),
        _ => bar(Tone::Info, "Incident", "Esc goes back."),
    }
}

pub(super) fn tone_color(t: Tone) -> Color {
    match t {
        Tone::Info | Tone::Active => ACCENT,
        Tone::Waiting => WARNING,
        Tone::Good => HEALTHY,
        Tone::Bad => CRITICAL,
    }
}

pub(super) fn draw_bar(f: &mut Frame, area: Rect, app: &App) {
    let b = bar_for(app);
    let color = tone_color(b.tone);
    let bg = surface(app, theme::ELEMENT);
    let w = area.width as usize;
    let edge = |s: &str| vec![Span::styled("┃", fg(color).patch(bg)), Span::styled(format!(" {s}"), bg)];
    let pad = |mut spans: Vec<Span<'static>>| {
        let used = width_of(&spans);
        spans.push(Span::styled(" ".repeat(w.saturating_sub(used)), bg));
        Line::from(spans)
    };
    let mut title = edge(" ");
    if b.spin {
        title.push(Span::styled(format!("{} ", spinner(app)), fg(color).patch(bg)));
    }
    title.push(Span::styled(b.title, fg(theme::TEXT).patch(bg).add_modifier(Modifier::BOLD)));
    let mut detail = edge(" ");
    detail.push(Span::styled(truncate(&b.detail, w.saturating_sub(6)), fg(theme::TEXT_MUTED).patch(bg)));
    let blank_row = pad(edge(""));
    f.render_widget(Paragraph::new(vec![blank_row, pad(title), pad(detail)]), area);
}

fn truncate(text: &str, room: usize) -> String {
    if text.chars().count() <= room {
        text.to_string()
    } else {
        format!("{}…", text.chars().take(room.saturating_sub(1)).collect::<String>())
    }
}

// ---- the footer -------------------------------------------------------------------------------

/// The keys that work in this situation, and only those: (key, label, keep-priority).
pub(super) fn footer_keys(app: &App) -> Vec<(&'static str, &'static str, u8)> {
    let offer = app.offer();
    let mut keys: Vec<(&'static str, &'static str, u8)> = Vec::new();
    let decides = app.decision_target().is_some_and(|_| can_decide_here(app));
    match &app.screen {
        Screen::Help => keys.push(("Esc", "Back", 9)),
        Screen::System => {
            keys.push(("D", "Run checks again", 8));
            keys.push(("X", "Stop", 7));
        }
        Screen::Lab if !app.practice && app.active_fault().is_none() => {
            keys.push(("Enter", "Run", 9));
            keys.push(("↑↓", "Navigate", 7));
            keys.push(("Esc", "Back", 6));
        }
        Screen::Incidents | Screen::Audit => {
            if !app.rows().is_empty() && app.screen == Screen::Incidents {
                keys.push(("Enter", "Open", 9));
            }
            keys.push(("↑↓", "Navigate", 7));
            keys.push(("R", "Run test", 6));
        }
        _ => {
            if decides {
                keys.push(("A", "Approve", 10));
                keys.push(("R", "Reject", 10));
                keys.push(("E", "Evidence", 8));
            } else if matches!(app.screen, Screen::Detail(_)) {
                // on an incident's own page R and A are its decision keys (they explain while it
                // loads), so the footer never offers them as quick actions here
                keys.push(("E", "Evidence", 8));
            } else {
                let finished = focus_incident(app).is_some_and(|i| is_closed(&i.status));
                if !app.rows().is_empty() && !finished {
                    keys.push(("Enter", "Open", 9));
                }
                if app.practice {
                    keys.push(("R", "Run again", 8));
                    let healthy = app.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.as_str()) == Some("healthy");
                    if healthy {
                        keys.push(("F", "Break it", 7));   // the manual retry if the automatic fault was refused
                    }
                } else {
                    keys.push(("R", "Run test", 8));
                    if offer.can_break {
                        keys.push(("F", "Inject fault", 7));
                    }
                    keys.push(("I", "Incidents", 4));
                }
                if finished {
                    keys.push(("E", "Evidence", 6));
                }
            }
            if matches!(app.screen, Screen::Detail(_)) {
                keys.push(("Esc", "Back", 9));
            } else if app.practice {
                keys.push(("Esc", "Leave test", 8));
            }
            keys.push(("D", if app.details { "Simple view" } else { "Details" }, 3));
        }
    }
    keys.push(("Ctrl+P", "Commands", 9));
    keys.push(("?", "Help", 9));
    keys
}

/// Whether A and R would open a decision here (the incident on screen is waiting and fully loaded).
pub(super) fn can_decide_here(app: &App) -> bool {
    let Some(id) = app.decision_target() else { return false };
    let waiting = app.snapshot.as_ref().is_some_and(|s| s.incidents.iter().any(|i| i.incident_id == id && i.status == "POLICY_CHECK"));
    let loaded = app.snapshot.as_ref().and_then(|s| s.detail.as_ref()).is_some_and(|d| d.incident_id == id);
    waiting && loaded && app.busy.is_none() && matches!(app.conn, Conn::Online)
}

pub(super) fn footer(f: &mut Frame, area: Rect, app: &App) {
    let w = area.width as usize;
    // what protects you, on the right: a dot per guard, a glyph that differs when it is not fine
    let mut right: Vec<Span> = Vec::new();
    if let Some(s) = &app.snapshot {
        let dot = |ok: Option<bool>, label: &str| -> Vec<Span<'static>> {
            match ok {
                Some(true) => vec![span("•", HEALTHY), muted(format!(" {label}   "))],
                Some(false) => vec![bold("!", CRITICAL), muted(format!(" {label}   "))],
                None => vec![muted(format!("? {label}   "))],
            }
        };
        if s.status.mode.as_deref() == Some("SIMULATION") {
            right.extend(vec![span("•", WARNING), muted(" simulated   ")]);
        } else {
            right.extend(dot(s.status.watchdog.as_ref().map(|w| w.state == "armed"), "watchdog"));
            right.extend(dot(s.status.audit.as_ref().map(|a| a.valid), "audit"));
        }
    }
    let mut keys = footer_keys(app);
    let cost = |k: &(&str, &str, u8)| k.0.chars().count() + k.1.chars().count() + 4;
    while 1 + keys.iter().map(cost).sum::<usize>() + width_of(&right) + 2 > w && keys.len() > 2 {
        let weakest = (0..keys.len()).min_by_key(|&n| keys[n].2).unwrap_or(0);
        keys.remove(weakest);
    }
    let mut left = vec![plain(" ")];
    for (k, label, _) in keys {
        left.push(bold(k.to_string(), theme::TEXT));
        left.push(muted(format!(" {label}   ")));
    }
    f.render_widget(Paragraph::new(split_row(left, right, w)), area);
}

// ---- toasts -----------------------------------------------------------------------------------

/// A notice floats at the top right with a bar at each edge in its colour. Errors wait for a key.
pub(super) fn toast(f: &mut Frame, area: Rect, app: &App) {
    let Some(n) = app.active_notice() else { return };
    let width = (area.width.saturating_sub(6)).min(60) as usize;
    if width < 20 {
        return;
    }
    let color = if n.is_error { CRITICAL } else { HEALTHY };
    let bg = surface(app, theme::PANEL);
    let inner = width.saturating_sub(6);
    let mut lines = vec![Line::from(Span::styled(" ".repeat(width), bg))];
    for part in wrap_words(&n.text, inner) {
        let used = part.chars().count();
        lines.push(Line::from(vec![
            Span::styled("┃", fg(color).patch(bg)),
            Span::styled(format!("  {part}{}", " ".repeat(inner.saturating_sub(used))), fg(theme::TEXT).patch(bg)),
            Span::styled("  ┃", fg(color).patch(bg)),
        ]));
    }
    lines.push(Line::from(Span::styled(" ".repeat(width), bg)));
    let height = (lines.len() as u16).min(area.height.saturating_sub(1));
    // just above the anchored bar, at the right: it never covers what it is about
    let rect = Rect { x: area.right().saturating_sub(width as u16 + 2), y: area.bottom().saturating_sub(height + 1), width: width as u16, height };
    f.render_widget(Clear, rect);
    f.render_widget(Paragraph::new(lines), rect);
}

// ---- the workload panel -----------------------------------------------------------------------

/// Context about the subject (the workload), at the right, secondary to the stream.
pub(super) fn workload_panel(f: &mut Frame, area: Rect, app: &App) {
    let bg = surface(app, theme::PANEL);
    f.render_widget(Block::default().style(bg), area);
    let inner = Rect { x: area.x + 2, y: area.y + 1, width: area.width.saturating_sub(4), height: area.height.saturating_sub(1) };
    let w = inner.width as usize;
    let info = app.snapshot.as_ref().map(|s| &s.status.info);
    let o = observation(app);
    let mut l: Vec<Line> = Vec::new();
    let name = na(info.and_then(|i| i.workload.clone()).or_else(|| info.and_then(|i| i.model.clone())));
    l.push(Line::from(muted(if app.practice { "WORKLOAD · SIMULATED" } else { "WORKLOAD" })));
    if no_workload(app) {
        l.push(Line::from(bold("No workload", theme::TEXT)));
        l.push(Line::from(muted("Autopilot will not start one.")));
    } else {
        l.push(Line::from(bold(name.clone(), theme::TEXT)));
        if let Some(model) = info.and_then(|i| i.model.clone()).filter(|m| *m != name) {
            l.push(Line::from(muted(model)));
        }
        l.push(Line::from(match (workload_state(app), o.and_then(|o| flag(o, "inference_probe_ok"))) {
            (Some("starting"), _) => bold("→ STARTING", WARNING),
            (_, Some(true)) => bold("✓ HEALTHY", HEALTHY),
            (_, Some(false)) => bold("✗ NOT ANSWERING", CRITICAL),
            _ => muted("○ NO READING YET"),
        }));
        l.push(blank());
        let probe = o.filter(|o| flag(o, "inference_probe_ok") == Some(true)).and_then(|o| num(o, "inference_probe_latency_ms")).map(|m| format!("{m:.0} ms"));
        let used = o.and_then(|o| num(o, "gpu_memory_used_bytes"));
        let total = o.and_then(|o| num(o, "gpu_memory_total_bytes")).filter(|t| *t > 0.0);
        let mem = match (used, total) {
            (Some(u), Some(t)) => format!("{} / {} GiB", gib(u), gib(t)),
            _ => "N/A".into(),
        };
        let row = |k: &str, v: String| Line::from(vec![muted(format!("{k:<10}")), plain(v)]);
        l.push(row("probe", na(probe)));
        l.push(row("requests", na(app.rates.requests_per_min.map(|r| format!("{r:.0}/min")))));
        l.push(row("latency", na(app.rates.latency_ms.map(|v| format!("{v:.0} ms")))));
        l.push(row("gpu", na(o.and_then(|o| num(o, "gpu_utilization_percent")).map(|u| format!("{u:.0}% busy")))));
        l.push(row("vram", mem));
        l.push(row("temp", na(o.and_then(|o| num(o, "gpu_temperature_c")).map(|t| format!("{t:.0}°C")))));
    }
    l.push(blank());
    l.push(Line::from(muted("AUTOPILOT")));
    if matches!(app.conn, Conn::Offline { .. }) {
        l.push(Line::from(bold("✗ not watching", CRITICAL)));
    } else {
        l.push(Line::from(bold("✓ watching", HEALTHY)));
        if let Some(a) = app.telemetry_age_secs() {
            l.push(Line::from(muted(format!("observed {} ago", human_age(a)))));
        }
    }
    for line in &mut l {
        for s in &mut line.spans {
            s.style = s.style.patch(bg);
        }
    }
    let _ = w;
    f.render_widget(Paragraph::new(l), inner);
}
