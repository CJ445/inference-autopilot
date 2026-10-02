//! Overlays: a tonal panel with a bar at its left edge floating over a dimmed screen. No border.
//! Used by the command palette, the decision, the real-fault confirmation, the refusal and the
//! stop confirmation.
use super::*;

fn surface(app: &App, color: Color) -> Style {
    if app.theme.has_surfaces() { Style::default().bg(color) } else { Style::default() }
}

/// Draws `lines` on a panel a quarter of the way down the screen and returns the panel's rectangle
/// (the caller dims everything outside it once the frame is themed).
pub(super) fn overlay(f: &mut Frame, area: Rect, app: &App, width: u16, lines: Vec<Line<'static>>, tone: Color) -> Rect {
    let width = width.min(area.width.saturating_sub(2)).max(20);
    let height = (lines.len() as u16 + 2).min(area.height);
    let y = (area.y + area.height / 4).min(area.bottom().saturating_sub(height));
    let rect = Rect { x: area.x + (area.width.saturating_sub(width)) / 2, y, width, height };
    let bg = surface(app, theme::PANEL);
    let inner = width as usize - 2;
    let pad = |spans: Vec<Span<'static>>| -> Line<'static> {
        let mut out = vec![Span::styled("┃", fg(tone).patch(bg)), Span::styled("  ", bg)];
        out.extend(spans.into_iter().map(|s| Span::styled(s.content, bg.patch(s.style))));
        let used = width_of(&out);
        out.push(Span::styled(" ".repeat((width as usize).saturating_sub(used)), bg));
        Line::from(out)
    };
    let mut all = vec![pad(vec![])];
    all.extend(lines.into_iter().map(|l| pad(l.spans)));
    all.push(pad(vec![]));
    let _ = inner;
    f.render_widget(Clear, rect);
    f.render_widget(Paragraph::new(all), rect);
    rect
}

pub(super) fn fit_sections(area: Rect, core_top: Vec<Line<'static>>, optional: Vec<Vec<Line<'static>>>, decision: Vec<Line<'static>>) -> Vec<Line<'static>> {
    let room = (area.height as usize).saturating_sub(4);
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

fn key_hint(armed: bool, key: &str, label: &str, tone: Color) -> Vec<Span<'static>> {
    vec![if armed { bold(key.to_string(), tone) } else { muted(key.to_string()) }, muted(format!(" {label}"))]
}

// ---- the decision -----------------------------------------------------------------------------

pub(super) fn confirm_modal(f: &mut Frame, area: Rect, app: &App) -> Option<Rect> {
    let c = app.confirm.as_ref()?;
    let width: u16 = 78.min(area.width.saturating_sub(2));
    let room = (width as usize).saturating_sub(8);
    let approve = c.kind == ActionKind::Approve;
    let tone = if approve { WARNING } else { TEXT_MUTED };
    let action = words::action_phrase(&c.action);
    let mut top = vec![split_row(
        vec![bold(if approve { "RECOVERY REQUEST" } else { "DECLINE THIS RECOVERY" }, tone)],
        if c.practice { vec![bold("SIMULATION", WARNING)] } else { vec![bold("LIVE", CRITICAL)] },
        width.saturating_sub(6) as usize,
    )];
    top.push(blank());
    top.push(Line::from(vec![bold(words::problem_title(&c.category), theme::TEXT), muted(format!("   {}", c.incident_id))]));
    for part in wrap_words(if c.why.is_empty() { "No cause statement from the server." } else { &c.why }, room) {
        top.push(Line::from(muted(part)));
    }
    top.push(blank());
    top.push(Line::from(vec![muted("Proposed  "), bold(action, theme::TEXT), muted(format!("   workload {}", c.workload))]));
    let effect = match (c.kind, c.action.as_str()) {
        (ActionKind::Reject, _) => "No action is taken; the proposal is closed.".to_string(),
        (ActionKind::Approve, _) if c.practice => "A simulated restart: nothing real is restarted.".to_string(),
        (ActionKind::Approve, "restart_workload") => "The model is unavailable while it restarts. There is no rollback.".to_string(),
        (ActionKind::Approve, _) => "The server executes the proposed action.".to_string(),
    };
    for part in wrap_words(&effect, room) {
        top.push(Line::from(plain(part)));
    }
    let mut evidence = Vec::new();
    if let Some(i) = incident_by_id(app, &c.incident_id) {
        let found: Vec<String> = i.evidence.iter().filter(|e| e.relation == "supports").filter_map(words::evidence_sentence).collect();
        if !found.is_empty() {
            evidence.push(blank());
            evidence.push(Line::from(muted("Evidence")));
            for e in found {
                for (n, part) in wrap_words(&e, room.saturating_sub(2)).into_iter().enumerate() {
                    evidence.push(Line::from(muted(format!("{} {part}", if n == 0 { "•" } else { " " }))));
                }
            }
        }
    }
    let why_ask = if approve { vec![blank(), Line::from(muted("This action is allowlisted and runs only after you approve it."))] } else { Vec::new() };
    let armed = app.now.saturating_duration_since(c.opened) >= CONFIRM_GUARD;
    let verb = if approve { "Approve" } else { "Decline" };
    let mut keys = key_hint(armed, "[Enter]", verb, ACCENT);
    keys.push(plain("    "));
    keys.extend(key_hint(true, "[Esc]", "Cancel", ACCENT));
    let decision = vec![blank(), Line::from(keys)];
    let lines = fit_sections(area, top, vec![why_ask, evidence], decision);
    Some(overlay(f, area, app, width, lines, tone))
}

pub(super) fn refusal_modal(f: &mut Frame, area: Rect, app: &App) -> Option<Rect> {
    let id = app.refusal.as_ref()?;
    let lines = vec![
        Line::from(bold("RECOVERY NO LONGER NEEDED", HEALTHY)),
        blank(),
        Line::from(plain(format!("{id}: the latest observation shows the problem is gone."))),
        Line::from(muted("The workload has recovered or is not running.")),
        blank(),
        Line::from(bold("No restart was performed.", theme::TEXT)),
        Line::from(muted("The incident is closed as Cleared; nothing was approved, run or verified.")),
        blank(),
        Line::from(vec![bold("[any key]", ACCENT), muted(" Close")]),
    ];
    Some(overlay(f, area, app, 72, lines, HEALTHY))
}

pub(super) fn stop_modal(f: &mut Frame, area: Rect, app: &App) -> Option<Rect> {
    let armed = app.now.saturating_duration_since(app.stop_opened) >= CONFIRM_GUARD;
    let mut keys = key_hint(armed, "[Enter]", "Stop", ACCENT);
    keys.push(plain("    "));
    keys.extend(key_hint(true, "[Esc]", "Cancel", ACCENT));
    let lines = vec![
        Line::from(bold("STOP THE CONTROL PLANE?", WARNING)),
        blank(),
        Line::from(plain("The operator UI will disconnect.")),
        Line::from(muted("The control plane and its watchdog stop.")),
        Line::from(muted("The managed workload is not touched.")),
        blank(),
        Line::from(keys),
    ];
    Some(overlay(f, area, app, 60, lines, WARNING))
}

pub(super) fn fault_modal(f: &mut Frame, area: Rect, app: &App) -> Option<Rect> {
    if !app.fault_confirm {
        return None;
    }
    let minutes = crate::app::FAULT_SECONDS / 60;
    let target = app.snapshot.as_ref().and_then(|s| s.status.info.workload.clone()).unwrap_or_else(|| "N/A".into());
    let armed = app.now.saturating_duration_since(app.fault_opened) >= CONFIRM_GUARD;
    let width = 80.min(area.width.saturating_sub(2));
    let top = vec![
        split_row(vec![bold("REAL INFRASTRUCTURE FAULT", CRITICAL)], vec![bold("LIVE · GPU-REAL", CRITICAL)], width.saturating_sub(6) as usize),
        blank(),
        Line::from(plain(format!("This will pause the managed workload {target}."))),
        Line::from(plain(format!("It stops answering for up to {minutes} minutes, then resumes by itself."))),
    ];
    let flow = vec![blank(), Line::from(muted("Expected flow")), Line::from("Healthy › Paused › Detected › Your approval › Restart › Verified recovery")];
    let safety = vec![
        blank(),
        Line::from(muted("Safety")),
        Line::from("✓ only the managed workload, by its checked identity"),
        Line::from("✓ a recovery lease is saved before anything is paused"),
        Line::from(format!("✓ it is resumed automatically after {minutes} minutes")),
        Line::from("✓ a separate process resumes it even if this one dies"),
    ];
    let mut keys = key_hint(armed, "[Enter]", "Inject fault", ACCENT);
    keys.push(plain("    "));
    keys.extend(key_hint(true, "[Esc]", "Cancel", ACCENT));
    let lines = fit_sections(area, top, vec![safety, flow], vec![blank(), Line::from(keys)]);
    Some(overlay(f, area, app, width, lines, CRITICAL))
}

// ---- the command palette ----------------------------------------------------------------------

pub(super) fn palette_modal(f: &mut Frame, area: Rect, app: &App) -> Option<Rect> {
    let p = app.palette.as_ref()?;
    let offer = app.offer();
    let items = p.matches(offer);
    let width: u16 = 64.min(area.width.saturating_sub(2));
    let inner = width as usize - 6;
    let suggested = if p.query.is_empty() { Palette::suggested(offer) } else { Vec::new() };
    let mut lines = vec![
        split_row(vec![bold("Commands", theme::TEXT)], vec![muted("esc")], inner),
        blank(),
        Line::from(if p.query.is_empty() {
            vec![span("❯ ", ACCENT), muted("Search commands…")]
        } else {
            vec![span("❯ ", ACCENT), plain(p.query.clone()), span("▏", ACCENT)]
        }),
        blank(),
    ];
    if items.is_empty() {
        lines.push(Line::from(muted("No matching command")));
    }
    let mut group: Option<&str> = None;
    // the window of rows that fits, kept around the selection
    let room = (area.height as usize).saturating_sub(10).max(4);
    let first = p.selected.saturating_sub(room.saturating_sub(1));
    for (n, cmd) in items.iter().enumerate().skip(first).take(room) {
        let heading = if suggested.contains(cmd) { "Suggested" } else { cmd.category() };
        if group != Some(heading) {
            if group.is_some() {
                lines.push(blank());
            }
            lines.push(Line::from(bold(heading.to_string(), ACCENT)));
            group = Some(heading);
        }
        let sel = n == p.selected;
        let shortcut = cmd.shortcut();
        let left = format!(" {}", cmd.label());
        let gap = inner.saturating_sub(left.chars().count() + shortcut.chars().count() + 1);
        let text = format!("{left}{}{shortcut} ", " ".repeat(gap));
        lines.push(Line::from(if sel {
            let mut style = Style::default().add_modifier(Modifier::BOLD);
            style = if app.theme.has_surfaces() { style.fg(theme::PAGE).bg(ACCENT) } else { style.add_modifier(Modifier::REVERSED) };
            vec![Span::styled(text, style)]
        } else {
            vec![plain(left), plain(" ".repeat(gap)), muted(format!("{shortcut} "))]
        }));
    }
    Some(overlay(f, area, app, width, lines, ACCENT))
}
