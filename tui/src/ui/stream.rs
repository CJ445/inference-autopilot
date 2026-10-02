//! The stream: the recovery loop as it happens. One block per stage, appended only when the server
//! reports it; the finished ones collapse to a line, the one the loop is at is open, and the view
//! sticks to the newest. Used by Home (when something is open), the incident page and the Lab.
use super::chrome::{can_decide_here, spinner};
use super::*;

#[derive(Clone, Copy, PartialEq, Debug)]
pub(super) enum St {
    Done,
    Active,
    Waiting,
    Failed,
    NotRun,
}

pub(super) struct Block {
    st: St,
    title: String,
    time: Option<String>,
    summary: String,
    /// Shown when the block is open: the evidence, the checks, the decision.
    detail: Vec<Vec<Span<'static>>>,
}

fn bar_color(st: St) -> Color {
    match st {
        St::Done | St::NotRun => BORDER,
        St::Active => ACCENT,
        St::Waiting => WARNING,
        St::Failed => CRITICAL,
    }
}

fn surface(app: &App, color: Color) -> Style {
    if app.theme.has_surfaces() { Style::default().bg(color) } else { Style::default() }
}

/// A line with a bar at its left edge and, when given, a tonal fill to the full width.
pub(super) fn bar_line(spans: Vec<Span<'static>>, bar: Color, fill: Option<Style>, width: usize) -> Line<'static> {
    let bg = fill.unwrap_or_default();
    let mut out = vec![Span::styled("┃", fg(bar).patch(bg)), Span::styled(" ", bg)];
    out.extend(spans.into_iter().map(|s| Span::styled(s.content, bg.patch(s.style))));
    let used = width_of(&out);
    if fill.is_some() {
        out.push(Span::styled(" ".repeat(width.saturating_sub(used)), bg));
    }
    Line::from(out)
}

/// Muted text wrapped to `w`, the first line led by `lead` and the rest indented under it.
fn wrapped_note(lead: &str, text: &str, w: usize) -> Vec<Vec<Span<'static>>> {
    let indent = " ".repeat(lead.chars().count());
    wrap_words(text, w.saturating_sub(lead.chars().count() + 4).max(16))
        .into_iter()
        .enumerate()
        .map(|(n, part)| vec![muted(format!("{}{part}", if n == 0 { lead } else { indent.as_str() }))])
        .collect()
}

fn evidence_bullets(i: &Incident, w: usize) -> Vec<Vec<Span<'static>>> {
    i.evidence.iter().filter(|e| e.relation == "supports").filter_map(words::evidence_sentence)
        .flat_map(|s| wrapped_note("  • ", &s, w)).collect()
}

fn checks_lines(i: &Incident) -> Vec<Vec<Span<'static>>> {
    let required = i.verification.as_ref().and_then(|v| v.required_completions);
    let Some(checks) = i.verification.as_ref().and_then(|v| v.checks.as_ref()).filter(|c| !c.is_empty()) else { return Vec::new() };
    let mut keys: Vec<&String> = checks.keys().collect();
    keys.sort_by_key(|k| (CHECK_ORDER.iter().position(|c| c == k).unwrap_or(99), (*k).clone()));
    keys.into_iter().map(|k| vec![plain("  "), mark(Some(checks[k])), plain(format!(" {}", words::check_phrase(k, required)))]).collect()
}

/// The blocks for one incident, from its status, timeline, proposal and recorded checks.
fn blocks(app: &App, i: &Incident, w: usize) -> Vec<Block> {
    use pipeline::Stage as P;
    let stg = pipeline::stages(Some(i), true);
    let at = |states: &[&str]| clock_of(i, states);
    let action = i.proposal.as_ref().map(|p| p.action.as_str()).or_else(|| app.proposed_action(&i.incident_id)).map(words::action_phrase);
    let diagnosis = i.rca.as_ref().and_then(|r| r.root_cause.as_ref()).map(|c| c.statement.clone());
    let mut out: Vec<Block> = Vec::new();
    let st_of = |p: P, waiting: bool| match p {
        P::Done => St::Done,
        P::Active if waiting => St::Waiting,
        P::Active => St::Active,
        P::Failed => St::Failed,
        _ => St::NotRun,
    };
    let b = |st: St, title: &str, time: Option<String>, summary: &str, detail: Vec<Vec<Span<'static>>>| Block { st, title: title.into(), time, summary: summary.into(), detail };

    // detect
    if stg[1] != P::NotApplicable {
        out.push(b(st_of(stg[1], false).max_done(), "DETECTED", at(&["DETECTED", "TRIAGING"]), &words::problem_title(&i.category), vec![]));
    }
    // diagnose
    match (stg[2], i.status.as_str()) {
        (P::Failed, _) => out.push(b(St::Waiting, "NOT ENOUGH EVIDENCE", at(&["DIAGNOSED"]), "Autopilot cannot name a cause, so nothing is proposed.", vec![])),
        (P::Active, _) => out.push(b(St::Active, "DIAGNOSING", None, "Collecting evidence.", vec![])),
        (P::Done, _) => out.push(b(St::Done, "DIAGNOSED", at(&["DIAGNOSED"]),
            &diagnosis.clone().unwrap_or_else(|| "Loading the details…".into()), evidence_bullets(i, w))),
        _ => {}
    }
    // proposal and decision
    let waiting = stg[4] == P::Active;
    if waiting {
        let mut detail = evidence_bullets(i, w);
        detail.extend(wrapped_note("  ", "Restart is an allowlisted recovery action; it runs only after you approve.", w));
        if can_decide_here(app) {
            detail.push(vec![]);
            detail.push(vec![plain("  "), bold("[ A ] Approve", ACCENT), plain("    "), bold("[ R ] Reject", ACCENT)]);
        } else {
            detail.push(vec![muted("  Loading the full details…")]);
        }
        out.push(b(St::Waiting, "RECOVERY READY", at(&["PROPOSED", "POLICY_CHECK"]), action.as_deref().unwrap_or("A recovery action"), detail));
    } else {
        match stg[3] {
            P::Done => out.push(b(St::Done, "RECOVERY PROPOSED", at(&["PROPOSED", "POLICY_CHECK"]), action.as_deref().unwrap_or("A recovery action"), vec![])),
            P::Active => out.push(b(St::Active, "CHOOSING A RECOVERY", None, "Checking the proposal against the policy.", vec![])),
            P::NotRun if stg[2] != P::NotApplicable => out.push(b(St::NotRun, "RECOVERY READY", None, "", vec![])),
            _ => {}
        }
        match stg[4] {
            P::Done => out.push(b(St::Done, "APPROVED", at(&["APPROVED"]), "You approved.", vec![])),
            P::Failed => out.push(b(St::Failed, "DECLINED", at(&["REJECTED"]), "You declined. Nothing was restarted.", vec![])),
            _ => {}
        }
    }
    if i.status == "CLEARED" {
        out.push(b(St::Done, "NO LONGER NEEDED", at(&["CLEARED"]), "The problem went away before it was acted on. Nothing was restarted.", vec![]));
    }
    // recover, verify
    match stg[5] {
        P::Active => out.push(b(St::Active, "RECOVERING", at(&["EXECUTING"]), "The server is restarting the workload.", vec![])),
        P::Done => out.push(b(St::Done, "RECOVERED", at(&["EXECUTING"]), "The workload was restarted.", vec![])),
        P::Failed => out.push(b(St::Failed, "RECOVERY FAILED", at(&["EXECUTION_FAILED"]), "The restart could not be carried out.", vec![])),
        P::NotRun => out.push(b(St::NotRun, "RECOVERING", None, "", vec![])),
        _ => {}
    }
    match stg[6] {
        P::Active => out.push(b(St::Active, "VERIFYING", at(&["VERIFYING"]), "Waiting for the server's result.", vec![])),
        P::Done => out.push(b(St::Done, "VERIFIED", at(&["RESOLVED"]), "The recorded checks all passed.", checks_lines(i))),
        P::Failed => out.push(b(St::Failed, "NOT VERIFIED", at(&["UNRESOLVED"]), "The recorded checks did not all pass.", checks_lines(i))),
        P::NotRun => out.push(b(St::NotRun, "VERIFYING", None, "", vec![])),
        _ => {}
    }
    out
}

trait MaxDone {
    fn max_done(self) -> Self;
}
impl MaxDone for St {
    /// The detection stage is either finished or the loop is on it.
    fn max_done(self) -> St {
        if self == St::NotRun { St::Done } else { self }
    }
}

fn glyph(app: &App, st: St) -> Span<'static> {
    match st {
        St::Done => span("✓", HEALTHY),
        St::Active => span(spinner(app), ACCENT),
        St::Waiting => bold("!", WARNING),
        St::Failed => bold("✗", CRITICAL),
        St::NotRun => muted("○"),
    }
}

fn block_lines(app: &App, b: &Block, open: bool, w: usize) -> Vec<Line<'static>> {
    let color = bar_color(b.st);
    let quiet = matches!(b.st, St::Done | St::NotRun);
    let title_span = if quiet { Span::styled(format!("{:<19}", b.title), fg(TEXT_MUTED).add_modifier(Modifier::BOLD)) } else { bold(format!("{:<19}", b.title), theme::TEXT) };
    let time = b.time.clone().map(|t| muted(t)).unwrap_or_else(|| plain(""));
    let head = |summary: Option<&str>| {
        let mut left = vec![glyph(app, b.st), plain(" "), title_span.clone()];
        if let Some(s) = summary.filter(|s| !s.is_empty()) {
            let room = w.saturating_sub(2 + 2 + 19 + 10);
            let s = if s.chars().count() > room { format!("{}…", s.chars().take(room.saturating_sub(1)).collect::<String>()) } else { s.to_string() };
            left.push(if quiet { muted(s) } else { plain(s) });
        }
        let line = split_row(left, vec![time.clone()], w.saturating_sub(2));
        line.spans
    };
    if !open || (b.detail.is_empty() && b.st == St::Done) {
        return vec![bar_line(head(Some(&b.summary)), color, None, w)];
    }
    // open: the tonal fill marks the block the loop is at
    let fill = if matches!(b.st, St::Done | St::NotRun) { None } else { Some(surface(app, theme::PANEL)) };
    let mut lines = vec![bar_line(head(None), color, fill, w)];
    for part in wrap_words(&b.summary, w.saturating_sub(6).max(20)) {
        lines.push(bar_line(vec![plain("  "), plain(part)], color, fill, w));
    }
    for d in &b.detail {
        lines.push(bar_line(d.clone(), color, fill, w));
    }
    lines
}

/// A block that is not about an incident (a test that has started, a fault that was injected).
pub(super) fn note_block(st: St, title: &str, summary: &str) -> Block {
    Block { st, title: title.into(), time: None, summary: summary.into(), detail: Vec::new() }
}

/// The stream of one incident, header first. `w` is the width of the content area.
pub(super) fn stream_lines(app: &App, i: &Incident, w: usize) -> Vec<Line<'static>> {
    stream_with(app, Some(i), Vec::new(), None, w)
}

/// The stream with blocks before the incident's own (and a header of its own, if given): a recovery
/// test or a real fault, whose first steps happen before there is an incident.
pub(super) fn stream_with(app: &App, i: Option<&Incident>, prefix: Vec<Block>, header: Option<(String, Vec<Span<'static>>)>, w: usize) -> Vec<Line<'static>> {
    let mut bs = prefix;
    if let Some(i) = i {
        bs.extend(blocks(app, i, w));
    }
    let Some(i) = i else {
        let focus = bs.iter().rposition(|b| b.st != St::NotRun);
        let mut lines = header_lines(header, w);
        for (n, b) in bs.iter().enumerate() {
            lines.extend(block_lines(app, b, Some(n) == focus, w));
        }
        return lines;
    };
    let focus = bs.iter().rposition(|b| b.st != St::NotRun);
    let when = match i.status.as_str() {
        "RESOLVED" => format!("recovered {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
        s if is_closed(s) => format!("closed {}", ago(app, &i.timeline.last().map_or(String::new(), |t| t.at.clone()))),
        _ => format!("detected {}", ago(app, &i.timeline.first().map_or(String::new(), |t| t.at.clone()))),
    };
    let mut right = vec![muted(format!("{} · {when}", i.incident_id))];
    if app.practice {
        right.insert(0, plain("  "));
        right.insert(0, bold("SIMULATION", WARNING));
    }
    let mut lines = match header {
        Some(h) => header_lines(Some(h), w),
        None => vec![split_row(vec![bold(words::problem_title(&i.category).to_uppercase(), theme::TEXT)], right, w), blank()],
    };
    for (n, b) in bs.iter().enumerate() {
        let open = Some(n) == focus || app.expanded;
        let rendered = block_lines(app, b, open, w);
        let tall = rendered.len() > 1;
        lines.extend(rendered);
        if tall {
            lines.push(blank());
        }
    }
    match i.status.as_str() {
        "RESOLVED" => lines.extend([blank(), Line::from(vec![bold("✓ INCIDENT RESOLVED", HEALTHY)])]),
        "UNRESOLVED" | "EXECUTION_FAILED" => lines.extend([blank(), Line::from(vec![bold("✗ NOT RESOLVED", CRITICAL)])]),
        _ => {}
    }
    lines
}

fn header_lines(header: Option<(String, Vec<Span<'static>>)>, w: usize) -> Vec<Line<'static>> {
    match header {
        Some((title, right)) => vec![split_row(vec![bold(title, theme::TEXT)], right, w), blank()],
        None => Vec::new(),
    }
}

/// Draws `lines` in `area`, sticking to the newest: `app.scroll` counts lines scrolled back.
pub(super) fn render_sticky(f: &mut Frame, area: Rect, lines: Vec<Line<'static>>, scrolled_back: u16) {
    let h = area.height as usize;
    let total = lines.len();
    let max_back = total.saturating_sub(h);
    let back = (scrolled_back as usize).min(max_back);
    let start = max_back - back;
    f.render_widget(Paragraph::new(lines.into_iter().skip(start).take(h).collect::<Vec<_>>()), area);
}

/// The loop at rest: the spine with nothing happening yet, each stage saying what it will do. It
/// teaches the product (what Autopilot does when something goes wrong) and claims nothing but
/// that the workload is being observed.
pub(super) fn idle_lines(app: &App, observing: bool, w: usize) -> Vec<Line<'static>> {
    let first = if observing {
        note_block(St::Done, "OBSERVING", "Watching the workload.")
    } else {
        note_block(St::NotRun, "OBSERVING", "Nothing to observe yet.")
    };
    let rest = [
        ("DETECT", "waits for the problem on consecutive checks"),
        ("DIAGNOSE", "collects evidence and names a cause"),
        ("PROPOSE", "chooses one allowlisted recovery"),
        ("APPROVE", "asks you before anything runs"),
        ("RECOVER", "restarts the workload"),
        ("VERIFY", "checks the model really recovered"),
    ];
    let mut lines = block_lines(app, &first, false, w);
    for (title, what) in rest {
        lines.extend(block_lines(app, &note_block(St::NotRun, title, what), false, w));
    }
    lines
}
