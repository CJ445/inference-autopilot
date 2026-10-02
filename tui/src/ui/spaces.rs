//! The spaces you move between: Overview, Incidents, Lab, Activity. They share the shell; each
//! fills the main region. Overview and the Lab show the stream when there is something to follow.
use super::stream::{self, bar_line, note_block, render_sticky, stream_lines, stream_with, St};
use super::*;

/// The padded content area inside the main region.
pub(super) fn content(area: Rect) -> Rect {
    Rect { x: area.x + 2, y: area.y + 1, width: area.width.saturating_sub(4), height: area.height.saturating_sub(1) }
}

fn surface(app: &App, color: Color) -> Style {
    if app.theme.has_surfaces() { Style::default().bg(color) } else { Style::default() }
}

fn label(text: &str, note: Option<Span<'static>>) -> Line<'static> {
    let mut spans = vec![muted(text.to_uppercase())];
    if let Some(n) = note {
        spans.push(plain("   "));
        spans.push(n);
    }
    Line::from(spans)
}

fn open_incident(app: &App) -> Option<&Incident> {
    focus_incident(app).filter(|i| !is_closed(&i.status))
}

// ---- Overview ---------------------------------------------------------------------------------

pub(super) fn home(f: &mut Frame, area: Rect, app: &App) {
    if app.snapshot.is_none() {
        return no_data(f, area, app);
    }
    let c = content(area);
    let w = c.width as usize;
    if let Some(i) = open_incident(app) {
        return render_sticky(f, c, stream_lines(app, i, w), app.scroll);
    }
    let (headline, mut story) = narrative(app);
    let mut l: Vec<Line<'static>> = Vec::new();
    if matches!(app.conn, Conn::Offline { .. }) {
        if let Some(line) = stale_line(app) {
            l.push(line);
            l.push(blank());
        }
    }
    l.push(Line::from(headline));
    for line in story.drain(..) {
        let text: String = line.spans.iter().map(|s| s.content.to_string()).collect();
        if text.chars().count() <= w {
            l.push(line);
        } else {
            // a long sentence wraps under itself; its colour is the muted one every story line uses
            l.extend(wrap_words(&text, w).into_iter().map(|part| Line::from(muted(part))));
        }
    }
    if app.practice {
        l.push(blank());
        let guide: String = practice_guide(app).iter().flat_map(|ln| ln.spans.iter().map(|s| s.content.to_string())).collect();
        l.extend(wrapped(guide.trim(), w));
    }
    l.push(blank());
    l.push(blank());
    l.push(label("When something goes wrong", None));
    l.push(blank());
    l.extend(stream::idle_lines(app, observing(app), w));
    let rows = app.rows();
    if !rows.is_empty() {
        l.push(blank());
        l.push(blank());
        l.push(label("Recent", None));
        for (idx, i) in rows.iter().enumerate().take(5) {
            l.push(incident_row(app, i, idx == app.selected, w));
        }
    }
    f.render_widget(Paragraph::new(l), c);
}

/// What is going on, in a sentence or two, from the data only.
fn narrative(app: &App) -> (Span<'static>, Vec<Line<'static>>) {
    if matches!(app.conn, Conn::Offline { .. }) {
        return (bold("Cannot reach the control plane.", CRITICAL), vec![Line::from(muted("Showing the last known values. Autopilot cannot watch until it is back."))]);
    }
    match workload_state(app) {
        Some("absent") => {
            return (bold("No workload connected.", theme::TEXT), vec![
                Line::from(muted("The control plane is running, but no managed inference workload is currently available.")),
                Line::from(muted("Autopilot will not create or start one automatically. Go to System for diagnostics.")),
            ])
        }
        Some("stopped") => {
            return (bold("The workload is stopped.", theme::TEXT), vec![
                Line::from(muted("Autopilot will not start it. Start it yourself and it is picked up automatically.")),
            ])
        }
        _ => {}
    }
    let info = app.snapshot.as_ref().map(|s| &s.status.info);
    let o = observation(app);
    let name = na(info.and_then(|i| i.workload.clone()).or_else(|| info.and_then(|i| i.model.clone())));
    let model = info.and_then(|i| i.model.clone()).filter(|m| *m != name).map(|m| format!(" · {m}")).unwrap_or_default();
    if workload_state(app) == Some("starting") {
        return (bold("Autopilot is watching inference health.", theme::TEXT), vec![Line::from(muted(format!("{name}{model} is starting up.")))]);
    }
    match o.and_then(|o| flag(o, "inference_probe_ok")) {
        Some(true) => {
            let ms = o.and_then(|o| num(o, "inference_probe_latency_ms")).map(|m| format!(" in {m:.0} ms")).unwrap_or_default();
            let used = o.and_then(|o| num(o, "gpu_memory_used_bytes"));
            let total = o.and_then(|o| num(o, "gpu_memory_total_bytes")).filter(|t| *t > 0.0);
            let mut story = vec![Line::from(vec![span("✓ ", HEALTHY), plain(format!("{name}{model} is answering{ms}."))])];
            if let (Some(u), Some(t)) = (used, total) {
                story.push(Line::from(muted(format!("GPU memory is {:.0}% used.", u / t * 100.0))));
            }
            (bold("Autopilot is watching inference health.", theme::TEXT), story)
        }
        Some(false) => (
            bold(format!("{name} is not answering."), CRITICAL),
            vec![Line::from(muted("Autopilot needs the problem on consecutive checks before it opens an incident."))],
        ),
        None => (bold("Waiting for the first observation.", theme::TEXT), vec![Line::from(muted("Nothing has been observed yet."))]),
    }
}

// ---- incident rows ----------------------------------------------------------------------------

fn state_bar(status: &str) -> Color {
    match status {
        "PROPOSED" | "POLICY_CHECK" | "INSUFFICIENT_EVIDENCE" => WARNING,
        "RESOLVED" => BORDER,
        "UNRESOLVED" | "EXECUTION_FAILED" => CRITICAL,
        "REJECTED" | "CLEARED" => BORDER,
        _ => ACCENT,
    }
}

/// One incident as a single line with its bar: `┃ ✓ RECOVERED  title   inc_001 · 4m ago`.
fn incident_row(app: &App, i: &Incident, selected: bool, w: usize) -> Line<'static> {
    let when = match i.status.as_str() {
        "RESOLVED" => format!("recovered {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
        s if is_closed(s) => format!("closed {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
        _ => format!("detected {}", ago(app, &i.timeline.first().map_or(String::new(), |t| t.at.clone()))),
    };
    let fill = if selected { Some(surface(app, theme::ELEMENT)) } else { None };
    let left = vec![badge_span(&i.status), plain(format!("  {}", words::problem_title(&i.category)))];
    let row = split_row(left, vec![muted(format!("{} · {when}", i.incident_id))], w.saturating_sub(2));
    let bar = if selected { ACCENT } else { state_bar(&i.status) };
    bar_line(row.spans, bar, fill, w)
}

pub(super) fn incident_list(f: &mut Frame, area: Rect, app: &App) {
    let c = content(area);
    let w = c.width as usize;
    let rows = app.rows();
    if rows.is_empty() {
        let mut l = vec![
            Line::from(bold("No active incidents.", theme::TEXT)),
            Line::from(muted(if no_workload(app) { "There is no managed workload to watch right now." } else { "Autopilot is watching the inference workload." })),
        ];
        if !app.practice {
            l.push(blank());
            l.push(Line::from(vec![muted("Want to see the recovery loop?  "), bold("R", theme::TEXT), muted(" runs a recovery test")]));
        }
        return f.render_widget(Paragraph::new(l), c);
    }
    let mut l: Vec<Line<'static>> = Vec::new();
    for (idx, i) in rows.iter().enumerate() {
        let sel = idx == app.selected;
        l.push(incident_row(app, i, sel, w));
        let fill = if sel { Some(surface(app, theme::ELEMENT)) } else { None };
        let color = if sel { ACCENT } else { state_bar(&i.status) };
        let diagnosis = i.rca.as_ref().and_then(|r| r.root_cause.as_ref()).map(|c| c.statement.clone()).filter(|s| !s.is_empty());
        match (i.status.as_str(), diagnosis) {
            ("INSUFFICIENT_EVIDENCE", _) => l.push(bar_line(vec![muted("   Not enough evidence to name a cause.")], color, fill, w)),
            (_, Some(d)) => l.push(bar_line(vec![muted(format!("   {d}"))], color, fill, w)),
            _ => {}
        }
        let proposal = i.proposal.as_ref().map(|p| words::action_phrase(&p.action)).or_else(|| app.proposed_action(&i.incident_id).map(words::action_phrase));
        let detail = match (i.status.as_str(), proposal) {
            ("PROPOSED" | "POLICY_CHECK", Some(p)) => Some(format!("{p} · needs your OK")),
            ("RESOLVED", Some(p)) => Some(format!("{p} · approved · recovery verified")),
            ("REJECTED", Some(p)) => Some(format!("{p} · you declined it")),
            ("CLEARED", Some(p)) => Some(format!("{p} · not needed: the problem went away")),
            ("UNRESOLVED", Some(p)) => Some(format!("{p} · recovery could not be verified")),
            (_, Some(p)) => Some(p),
            _ => None,
        };
        if let Some(d) = detail {
            l.push(bar_line(vec![muted(format!("   {d}"))], color, fill, w));
        }
        l.push(blank());
    }
    f.render_widget(Paragraph::new(l), c);
}

// ---- Activity ---------------------------------------------------------------------------------

pub(super) fn activity(f: &mut Frame, area: Rect, app: &App) {
    let c = content(area);
    let w = c.width as usize;
    let s = app.snapshot.as_ref();
    let fetched = s.and_then(|s| s.audit.as_ref());
    let valid = fetched.map(|a| a.valid).or_else(|| s.and_then(|s| s.status.audit.as_ref().map(|a| a.valid)));
    let mut lines = vec![match valid {
        Some(true) => Line::from(vec![span("✓ ", HEALTHY), plain("Activity log intact")]),
        Some(false) => Line::from(bold("✗ ACTIVITY LOG INTEGRITY FAILURE", CRITICAL)),
        None => Line::from(muted("Activity log N/A")),
    }, blank()];
    match fetched {
        None => lines.push(Line::from(muted("Waiting for the activity log…"))),
        Some(a) if a.events.is_empty() => {
            lines.push(Line::from(bold("NO RECENT ACTIVITY", theme::TEXT)));
            lines.push(Line::from("Autopilot has not recorded any events yet."));
            if !app.practice {
                lines.push(blank());
                lines.push(Line::from(vec![bold("R ", theme::TEXT), plain("Run a recovery test")]));
            }
        }
        Some(a) => {
            for e in &a.events {
                let phrase = words::event_phrase(e);
                lines.push(bar_line(vec![plain(phrase)], BORDER, None, w));
            }
            lines.push(blank());
            lines.push(Line::from(muted("D  Technical details (event names, hashes)")));
        }
    }
    f.render_widget(Paragraph::new(lines).scroll((app.scroll, 0)), c);
}

// ---- Lab --------------------------------------------------------------------------------------

pub(super) fn lab(f: &mut Frame, area: Rect, app: &App) {
    let c = content(area);
    let w = c.width as usize;
    if app.practice {
        return render_sticky(f, c, test_lines(app, w), app.scroll);
    }
    if let Some(fault) = app.active_fault() {
        return render_sticky(f, c, fault_lines(app, fault, w), app.scroll);
    }
    f.render_widget(Paragraph::new(lab_menu(app, w)), c);
}

fn lab_row(app: &App, selected: bool, bar: Color, title: &str, hint: &str, desc: &[&str], enabled: bool, w: usize) -> Vec<Line<'static>> {
    let fill = if selected { Some(surface(app, theme::ELEMENT)) } else { None };
    let color = if selected { bar } else { BORDER };
    let title_span = if enabled { bold(title.to_string(), theme::TEXT) } else { muted(title.to_string()) };
    let head = split_row(vec![title_span], vec![if selected && enabled { bold(hint.to_string(), bar) } else { muted(hint.to_string()) }], w.saturating_sub(2));
    let mut lines = vec![bar_line(head.spans, color, fill, w)];
    for d in desc {
        lines.push(bar_line(vec![muted(d.to_string())], color, fill, w));
    }
    lines
}

fn lab_menu(app: &App, w: usize) -> Vec<Line<'static>> {
    let offer = app.offer();
    let mut l = vec![
        Line::from(bold("RECOVERY LAB", theme::TEXT)),
        Line::from(muted("Test the autopilot safely, or on the real workload.")),
        blank(),
        label("Safe tests", Some(muted("simulated · nothing real is touched"))),
        blank(),
    ];
    l.extend(lab_row(app, app.lab_cursor == 0, ACCENT, "Model becomes unresponsive", "Run  ⏎",
        &["Watch Autopilot detect it, diagnose it, ask for your OK, recover and verify."], true, w));
    l.push(blank());
    l.push(blank());
    l.push(label("Real infrastructure", Some(span("affects the running workload", WARNING))));
    l.push(blank());
    let why = match (&app.conn, workload_state(app), app.snapshot.as_ref().and_then(|s| s.status.faults.as_ref())) {
        (Conn::Online, _, None) => "this control plane does not offer real faults",
        (Conn::Online, Some("absent" | "stopped"), _) => "there is no running workload to pause",
        (Conn::Online, _, Some(f)) if !f.available => "this control plane does not offer real faults",
        (Conn::Online, _, _) => "not available right now",
        _ => "the control plane cannot be reached",
    };
    if offer.can_break {
        l.extend(lab_row(app, app.lab_cursor == 1, WARNING, "Pause the running workload", "Inject  ⏎",
            &["Pauses the managed model server for up to two minutes so it stops answering.", "It always resumes by itself. You confirm first."], true, w));
    } else {
        l.extend(lab_row(app, false, WARNING, "Pause the running workload", "", &[&format!("Not available: {why}.")], false, w));
    }
    l
}

/// The recovery test as a live session: it opens with the steps before there is an incident.
fn test_lines(app: &App, w: usize) -> Vec<Line<'static>> {
    let stage = app.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.clone());
    let header = ("RECOVERY TEST  ·  Model becomes unresponsive".to_string(), vec![bold("SIMULATION", WARNING), plain(" ")]);
    let Some(stage) = stage else {
        return vec![Line::from(bold("RECOVERY TEST", theme::TEXT)), blank(), Line::from(muted("Preparing the recovery test…"))];
    };
    let mut prefix = vec![note_block(St::Done, "TEST STARTED", "The simulated model is healthy.")];
    let incident = focus_incident(app);
    match (stage.as_str(), incident) {
        ("healthy", _) => prefix.push(note_block(St::Active, "INJECTING THE FAULT", "Asking the server to make the model stop answering.")),
        ("detecting", None) => {
            prefix.push(note_block(St::Done, "FAULT INJECTED", "The simulated model stopped answering."));
            prefix.push(note_block(St::Active, "DETECTING", "Autopilot needs the problem on consecutive checks before it opens an incident."));
        }
        _ => prefix.push(note_block(St::Done, "FAULT INJECTED", "The simulated model stopped answering.")),
    }
    stream_with(app, incident, prefix, Some(header), w)
}

fn fault_lines(app: &App, fault: &crate::model::ActiveFault, w: usize) -> Vec<Line<'static>> {
    let left = (fault.expires_at - app.wall).max(0.0) as u64;
    let header = ("REAL FAULT IN PROGRESS".to_string(), vec![bold("LIVE", CRITICAL), plain(" ")]);
    let incident = focus_incident(app);
    let mut prefix = vec![note_block(St::Done, "FAULT INJECTED", &format!("The workload is paused; it resumes by itself in {}:{:02}.", left / 60, left % 60))];
    if incident.is_none() {
        prefix.push(note_block(St::Active, "WAITING TO DETECT", "Autopilot needs the problem on consecutive checks before it opens an incident."));
    }
    stream_with(app, incident, prefix, Some(header), w)
}
