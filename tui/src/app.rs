//! Application state and behaviour. Pure and deterministic: no I/O, no clock reads (the loop sets
//! `now` and `wall`), so every behaviour is testable. The server is the only source of truth.
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::api::ApiError;
use crate::model::{num, Audit, Incident, Status};
use crate::timeparse::parse_rfc3339;

/// Data older than this is shown as stale.
pub const STALE_AFTER: Duration = Duration::from_secs(3);
/// The control plane's own last observation older than this is flagged as stale telemetry.
pub const TELEMETRY_STALE_SECS: f64 = 10.0;
pub const NOTICE_TTL: Duration = Duration::from_secs(10);

/// Statuses the server treats as finished (display grouping only; the server decides).
const CLOSED: [&str; 5] = ["RESOLVED", "UNRESOLVED", "EXECUTION_FAILED", "REJECTED", "CLEARED"];

/// True for a status the server treats as finished.
pub fn is_closed(status: &str) -> bool {
    CLOSED.contains(&status)
}

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub status: Status,
    pub incidents: Vec<Incident>,
    pub detail: Option<Incident>,
    pub audit: Option<Audit>,
    pub poll_ms: u128,
    pub fetched_at: Option<Instant>,
}

impl Snapshot {
    pub fn new(status: Status, incidents: Vec<Incident>) -> Snapshot {
        Snapshot { status, incidents, detail: None, audit: None, poll_ms: 0, fetched_at: None }
    }
}

#[derive(Debug, Clone)]
pub enum Conn {
    Connecting,
    Online,
    Offline { error: String, since: Instant, attempts: u32 },
}

#[derive(Debug, Clone, PartialEq)]
pub enum Screen {
    Dashboard,
    Incidents,
    Detail(String),
    Audit,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ActionKind {
    Approve,
    Reject,
}

impl ActionKind {
    pub fn verb(self) -> &'static str {
        match self {
            ActionKind::Approve => "Approve",
            ActionKind::Reject => "Reject",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Confirm {
    pub kind: ActionKind,
    pub incident_id: String,
    pub action: String,
    pub workload: String,
}

#[derive(Debug, Clone)]
pub struct Busy {
    pub kind: ActionKind,
    pub incident_id: String,
    pub since: Instant,
}

#[derive(Debug, Clone)]
pub struct Notice {
    pub text: String,
    pub is_error: bool,
    pub at: Instant,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    Approve(String),
    Reject(String),
    RefreshNow,
    Quit,
}

#[derive(Debug)]
pub enum Msg {
    Poll(Result<Snapshot, ApiError>),
    Action { kind: ActionKind, id: String, result: Result<Incident, ApiError> },
}

/// What the poller should fetch on top of the status and the incident list.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Interest {
    pub detail_id: Option<String>,
    pub want_audit: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Rates {
    pub requests_per_min: Option<f64>,
    pub latency_ms: Option<f64>,
}

pub struct App {
    pub url: String,
    pub screen: Screen,
    back: Screen,
    pub selected: usize,
    pub scroll: u16,
    pub confirm: Option<Confirm>,
    pub busy: Option<Busy>,
    pub notice: Option<Notice>,
    pub conn: Conn,
    pub snapshot: Option<Snapshot>,
    pub rates: Rates,
    pub now: Instant,
    pub wall: f64,
    prev: Option<(f64, f64, f64)>,
}

fn wall_clock() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

impl App {
    pub fn new(url: String) -> App {
        App {
            url,
            screen: Screen::Dashboard,
            back: Screen::Dashboard,
            selected: 0,
            scroll: 0,
            confirm: None,
            busy: None,
            notice: None,
            conn: Conn::Connecting,
            snapshot: None,
            rates: Rates::default(),
            now: Instant::now(),
            wall: wall_clock(),
            prev: None,
        }
    }

    // ---- derived, read-only views --------------------------------------------------------------

    /// Active incidents first, then finished ones; newest first within each group.
    pub fn rows(&self) -> Vec<&Incident> {
        let Some(s) = &self.snapshot else { return Vec::new() };
        let (active, closed): (Vec<&Incident>, Vec<&Incident>) =
            s.incidents.iter().rev().partition(|i| !is_closed(&i.status));
        active.into_iter().chain(closed).collect()
    }

    pub fn selected_incident(&self) -> Option<&Incident> {
        if let Screen::Detail(id) = &self.screen {
            let s = self.snapshot.as_ref()?;
            return s.detail.as_ref().filter(|d| &d.incident_id == id)
                .or_else(|| s.incidents.iter().find(|i| &i.incident_id == id));
        }
        self.rows().get(self.selected).copied()
    }

    pub fn is_stale(&self) -> bool {
        let Some(s) = &self.snapshot else { return false };
        matches!(self.conn, Conn::Offline { .. })
            || s.fetched_at.map_or(false, |t| self.now.saturating_duration_since(t) > STALE_AFTER)
    }

    pub fn data_age(&self) -> Option<Duration> {
        let t = self.snapshot.as_ref()?.fetched_at?;
        Some(self.now.saturating_duration_since(t))
    }

    /// Seconds since the control plane last observed the infrastructure (its own timestamp).
    pub fn telemetry_age_secs(&self) -> Option<f64> {
        let at = self.snapshot.as_ref()?.status.last_observed_at.as_deref()?;
        Some((self.wall - parse_rfc3339(at)?).max(0.0))
    }

    pub fn telemetry_stale(&self) -> bool {
        self.telemetry_age_secs().map_or(false, |a| a > TELEMETRY_STALE_SECS)
    }

    pub fn interest(&self) -> Interest {
        Interest {
            detail_id: match &self.screen {
                Screen::Detail(id) => Some(id.clone()),
                _ => None,
            },
            want_audit: self.screen == Screen::Audit,
        }
    }

    pub fn active_notice(&self) -> Option<&Notice> {
        self.notice.as_ref().filter(|n| self.now.saturating_duration_since(n.at) < NOTICE_TTL)
    }

    // ---- input ---------------------------------------------------------------------------------

    fn say(&mut self, text: &str, is_error: bool) {
        self.notice = Some(Notice { text: text.to_string(), is_error, at: self.now });
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> Vec<Effect> {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            return vec![Effect::Quit];
        }
        if let Some(c) = self.confirm.clone() {
            // While a confirmation is open ONLY Enter (do it) or Esc (don't) mean anything.
            match key.code {
                KeyCode::Enter => {
                    self.confirm = None;
                    self.busy = Some(Busy { kind: c.kind, incident_id: c.incident_id.clone(), since: self.now });
                    return vec![match c.kind {
                        ActionKind::Approve => Effect::Approve(c.incident_id),
                        ActionKind::Reject => Effect::Reject(c.incident_id),
                    }];
                }
                KeyCode::Esc => self.confirm = None,
                _ => {}
            }
            return Vec::new();
        }
        match key.code {
            KeyCode::Char('q') | KeyCode::Char('Q') => return vec![Effect::Quit],
            KeyCode::Char('s') | KeyCode::Char('S') => return vec![Effect::RefreshNow],
            KeyCode::Tab => self.go(match self.screen {
                Screen::Dashboard => Screen::Incidents,
                Screen::Incidents | Screen::Detail(_) => Screen::Audit,
                Screen::Audit => Screen::Dashboard,
            }),
            KeyCode::Char('1') => self.go(Screen::Dashboard),
            KeyCode::Char('2') => self.go(Screen::Incidents),
            KeyCode::Char('3') => self.go(Screen::Audit),
            KeyCode::Up => self.step(-1),
            KeyCode::Down => self.step(1),
            KeyCode::PageUp => self.step(-8),
            KeyCode::PageDown => self.step(8),
            KeyCode::Enter => {
                if matches!(self.screen, Screen::Dashboard | Screen::Incidents) {
                    if let Some(id) = self.selected_incident().map(|i| i.incident_id.clone()) {
                        self.back = self.screen.clone();
                        self.screen = Screen::Detail(id);
                        self.scroll = 0;
                    }
                }
            }
            KeyCode::Esc => match self.screen {
                Screen::Detail(_) => {
                    self.screen = self.back.clone();
                    self.scroll = 0;
                }
                Screen::Dashboard => {}
                _ => self.go(Screen::Dashboard),
            },
            KeyCode::Char('a') | KeyCode::Char('A') => self.begin(ActionKind::Approve),
            KeyCode::Char('r') | KeyCode::Char('R') => self.begin(ActionKind::Reject),
            _ => {}
        }
        Vec::new()
    }

    fn go(&mut self, screen: Screen) {
        self.screen = screen;
        self.scroll = 0;
    }

    fn step(&mut self, delta: i32) {
        match self.screen {
            Screen::Detail(_) | Screen::Audit => {
                self.scroll = (self.scroll as i32 + delta).clamp(0, u16::MAX as i32) as u16;
            }
            _ => {
                let last = self.rows().len().saturating_sub(1) as i32;
                self.selected = (self.selected as i32 + delta).clamp(0, last) as usize;
            }
        }
    }

    /// Opens a confirmation. Nothing is sent until the operator presses Enter on it.
    fn begin(&mut self, kind: ActionKind) {
        if self.busy.is_some() {
            return self.say("an action is already in progress; wait for the server's answer", true);
        }
        if !matches!(self.conn, Conn::Online) {
            return self.say("control plane unreachable: nothing can be approved or rejected", true);
        }
        let Some(incident) = self.selected_incident() else {
            return self.say("no incident selected", true);
        };
        let Some(proposal) = incident.proposal.as_ref().filter(|_| incident.status == "POLICY_CHECK") else {
            return self.say("this incident has no proposal awaiting a decision", true);
        };
        let confirm = Confirm {
            kind,
            incident_id: incident.incident_id.clone(),
            action: proposal.action.clone(),
            workload: proposal
                .parameters
                .get("workload")
                .and_then(|v| v.as_str())
                .unwrap_or("N/A")
                .to_string(),
        };
        self.confirm = Some(confirm);
    }

    // ---- messages from the poller and from actions -------------------------------------------

    pub fn apply(&mut self, msg: Msg) {
        match msg {
            Msg::Poll(Ok(mut snapshot)) => {
                snapshot.fetched_at = Some(self.now);
                self.update_rates(&snapshot.status);
                self.conn = Conn::Online;
                self.snapshot = Some(snapshot);
                self.selected = self.selected.min(self.rows().len().saturating_sub(1));
            }
            Msg::Poll(Err(e)) => {
                self.conn = match &self.conn {
                    Conn::Offline { since, attempts, .. } => {
                        Conn::Offline { error: e.to_string(), since: *since, attempts: attempts + 1 }
                    }
                    _ => Conn::Offline { error: e.to_string(), since: self.now, attempts: 1 },
                };
            }
            Msg::Action { kind, id, result } => {
                self.busy = None;
                match result {
                    Ok(incident) => self.say(
                        &format!("{} sent for {id}: the server reports {}", kind.verb(), incident.status),
                        false,
                    ),
                    Err(ApiError::Timeout) => self.say(
                        &format!("no answer for {id} yet; the server may still be working: check its state"),
                        true,
                    ),
                    Err(e) => self.say(&format!("{} failed for {id}: {e}", kind.verb()), true),
                }
            }
        }
    }

    fn update_rates(&mut self, status: &Status) {
        let observed = status.last_observation.as_ref().and_then(|o| {
            let t = parse_rfc3339(status.last_observed_at.as_deref()?)?;
            Some((t, num(o, "vllm_e2e_latency_seconds_count")?, num(o, "vllm_e2e_latency_seconds_sum")?))
        });
        let Some((t, count, sum)) = observed else {
            self.rates = Rates::default();
            self.prev = None;
            return;
        };
        match self.prev {
            Some((pt, ..)) if t == pt => return, // the same observation, polled again
            Some((pt, pc, ps)) if t > pt => {
                let dc = count - pc;
                self.rates = if dc < 0.0 || sum < ps {
                    Rates::default() // a counter reset (workload restarted): no negative rates
                } else {
                    Rates {
                        requests_per_min: Some(dc / (t - pt) * 60.0),
                        latency_ms: if dc > 0.0 { Some((sum - ps) / dc * 1000.0) } else { None },
                    }
                };
            }
            _ => self.rates = Rates::default(),
        }
        self.prev = Some((t, count, sum));
    }
}
