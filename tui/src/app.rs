//! Application state and behaviour. Pure and deterministic: no I/O, no clock reads (the loop sets
//! `now` and `wall`), so every behaviour is testable. The server is the only source of truth.
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::api::ApiError;
use crate::model::{num, Audit, ConfigInfo, Diagnostics, Incident, Status, StopAck, VersionInfo};
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
    ControlPlane,
    Settings,
    Diagnostics,
    About,
    Help,
}

/// The order `Tab` walks and the sidebar lists; `1`..`7` jump to the first seven.
pub const SCREEN_ORDER: [Screen; 8] = [
    Screen::Dashboard,
    Screen::Incidents,
    Screen::Audit,
    Screen::ControlPlane,
    Screen::Settings,
    Screen::Diagnostics,
    Screen::About,
    Screen::Help,
];

/// Something the server was asked for and has (or has not yet) answered. Never a default.
#[derive(Debug, Clone)]
pub enum Remote<T> {
    Idle,
    Loading(Instant),
    Ready(T),
    Failed(String),
}

impl<T> Remote<T> {
    pub fn is_loading(&self) -> bool {
        matches!(self, Remote::Loading(_))
    }
}

/// Everything a command can do. The palette lists only these; each is also reachable by a key.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Command {
    Overview,
    Incidents,
    Audit,
    ControlPlane,
    Settings,
    Diagnostics,
    About,
    Help,
    Refresh,
    StopControlPlane,
    Quit,
}

impl Command {
    pub const ALL: [Command; 11] = [
        Command::Overview,
        Command::Incidents,
        Command::Audit,
        Command::ControlPlane,
        Command::Settings,
        Command::Diagnostics,
        Command::About,
        Command::Help,
        Command::Refresh,
        Command::StopControlPlane,
        Command::Quit,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Command::Overview => "Open Overview",
            Command::Incidents => "Open Incidents",
            Command::Audit => "Open Audit",
            Command::ControlPlane => "Open Control Plane",
            Command::Settings => "Open Settings",
            Command::Diagnostics => "Open Diagnostics",
            Command::About => "Open About",
            Command::Help => "Open Help",
            Command::Refresh => "Refresh",
            Command::StopControlPlane => "Stop control plane…",
            Command::Quit => "Quit",
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct Palette {
    pub query: String,
    pub selected: usize,
}

impl Palette {
    /// Commands whose label contains every word of the query (case-insensitive).
    pub fn matches(&self) -> Vec<Command> {
        let q = self.query.to_lowercase();
        let words: Vec<&str> = q.split_whitespace().collect();
        Command::ALL
            .into_iter()
            .filter(|c| {
                let label = c.label().to_lowercase();
                words.iter().all(|w| label.contains(w))
            })
            .collect()
    }
}

/// Client-local facts the TUI was started with (shown read-only in Settings).
#[derive(Debug, Clone, Default)]
pub struct ClientInfo {
    pub interval_ms: Option<u64>,
    pub timeout_ms: Option<u64>,
}

/// The keys that do something, in the order Help lists them. `tests/help.rs` drives every row.
pub const KEYMAP: [(&str, &str); 15] = [
    ("↑ ↓", "Navigate / scroll"),
    ("Enter", "Inspect the selected incident"),
    ("Esc", "Back / cancel / close"),
    ("Tab →", "Next screen"),
    ("←", "Previous screen"),
    ("1-7", "Jump to a screen (Overview … About)"),
    ("?", "Help"),
    ("Ctrl+P", "Command palette"),
    ("S", "Refresh now"),
    ("A", "Approve (on an incident's page, once it awaits a decision)"),
    ("R", "Reject (on an incident's page, once it awaits a decision)"),
    ("D", "Run diagnostics again (Diagnostics screen)"),
    ("X", "Stop the control plane (Control Plane screen)"),
    ("Q", "Quit (the control plane keeps running)"),
    ("Ctrl+C", "Quit"),
];

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
    pub category: String,
    /// The server's RCA statement (empty if it sent none): why this action is proposed.
    pub why: String,
    pub opened: Instant,
}

/// Enter is ignored for this long after a confirmation opens, so a held or double-tapped Enter
/// (the key that opened the incident) cannot confirm an action nobody read.
pub const CONFIRM_GUARD: Duration = Duration::from_millis(500);

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
    /// Read-only requests: safe to repeat.
    LoadConfig,
    LoadVersion,
    RunDiagnostics,
    /// The one lifecycle request: sent once per confirmation, never retried.
    StopControlPlane,
}

#[derive(Debug)]
pub enum Loaded {
    Config(Result<ConfigInfo, ApiError>),
    Version(Result<VersionInfo, ApiError>),
    Diagnostics(Result<Diagnostics, ApiError>),
    Stop(Result<StopAck, ApiError>),
}

#[derive(Debug)]
pub enum Msg {
    Poll(Result<Snapshot, ApiError>),
    Action { kind: ActionKind, id: String, result: Result<Incident, ApiError> },
    Loaded(Loaded),
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
    pub client: ClientInfo,
    pub config: Remote<ConfigInfo>,
    pub version: Remote<VersionInfo>,
    pub diag: Remote<Diagnostics>,
    pub palette: Option<Palette>,
    pub stop_confirm: bool,
    pub stop_opened: Instant,
    pub stop_sent: Option<Instant>,
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
            client: ClientInfo::default(),
            config: Remote::Idle,
            version: Remote::Idle,
            diag: Remote::Idle,
            palette: None,
            stop_confirm: false,
            stop_opened: Instant::now(),
            stop_sent: None,
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

    /// The incident an approve/reject in flight is about, as the server last reported it.
    pub fn busy_incident(&self) -> Option<&Incident> {
        let id = &self.busy.as_ref()?.incident_id;
        let s = self.snapshot.as_ref()?;
        s.detail.as_ref().filter(|d| &d.incident_id == id)
            .or_else(|| s.incidents.iter().find(|i| &i.incident_id == id))
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
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        if ctrl && key.code == KeyCode::Char('c') {
            return vec![Effect::Quit];
        }
        if let Some(c) = self.confirm.clone() {
            // While a confirmation is open ONLY Enter (do it) or Esc (don't) mean anything.
            match key.code {
                KeyCode::Enter if self.now.saturating_duration_since(c.opened) < CONFIRM_GUARD => {}
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
        if self.stop_confirm {
            match key.code {
                KeyCode::Enter if self.now.saturating_duration_since(self.stop_opened) < CONFIRM_GUARD => {}
                KeyCode::Enter => {
                    self.stop_confirm = false;
                    self.stop_sent = Some(self.now);
                    return vec![Effect::StopControlPlane];
                }
                KeyCode::Esc => self.stop_confirm = false,
                _ => {}
            }
            return Vec::new();
        }
        if self.palette.is_some() {
            return self.palette_key(key);
        }
        if ctrl && key.code == KeyCode::Char('p') {
            self.palette = Some(Palette::default());
            return Vec::new();
        }
        if ctrl || key.modifiers.contains(KeyModifiers::ALT) {
            return Vec::new(); // a modified letter is not the plain command
        }
        let mut fx = Vec::new();
        match key.code {
            KeyCode::Char('q') | KeyCode::Char('Q') => return vec![Effect::Quit],
            KeyCode::Char('s') | KeyCode::Char('S') => return vec![Effect::RefreshNow],
            KeyCode::Tab | KeyCode::Right => fx = self.cycle(1),
            KeyCode::Left => fx = self.cycle(-1),
            KeyCode::Char(c @ '1'..='7') => {
                fx = self.go(SCREEN_ORDER[c as usize - '1' as usize].clone());
            }
            KeyCode::Char('?') => fx = self.go(Screen::Help),
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
                _ => fx = self.go(Screen::Dashboard),
            },
            KeyCode::Char('a') | KeyCode::Char('A') => self.begin(ActionKind::Approve),
            KeyCode::Char('r') | KeyCode::Char('R') => self.begin(ActionKind::Reject),
            KeyCode::Char('d') | KeyCode::Char('D') if self.screen == Screen::Diagnostics => {
                fx = self.load_diagnostics(true)
            }
            KeyCode::Char('x') | KeyCode::Char('X') if self.screen == Screen::ControlPlane => {
                self.begin_stop()
            }
            _ => {}
        }
        fx
    }

    /// The next (`1`) or previous (`-1`) sidebar screen, wrapping; a detail view counts as Incidents.
    fn cycle(&mut self, delta: isize) -> Vec<Effect> {
        let here = match &self.screen {
            Screen::Detail(_) => &Screen::Incidents,
            other => other,
        };
        let n = SCREEN_ORDER.len() as isize;
        let at = SCREEN_ORDER.iter().position(|s| s == here).unwrap_or(0) as isize;
        self.go(SCREEN_ORDER[(at + delta).rem_euclid(n) as usize].clone())
    }

    fn palette_key(&mut self, key: KeyEvent) -> Vec<Effect> {
        let Some(p) = self.palette.as_mut() else { return Vec::new() };
        match key.code {
            KeyCode::Esc => self.palette = None,
            KeyCode::Backspace => {
                p.query.pop();
                p.selected = 0;
            }
            KeyCode::Up => p.selected = p.selected.saturating_sub(1),
            KeyCode::Down => {
                let last = p.matches().len().saturating_sub(1);
                p.selected = (p.selected + 1).min(last);
            }
            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                p.query.push(c);
                p.selected = 0;
            }
            KeyCode::Enter => {
                let chosen = p.matches().get(p.selected).copied();
                self.palette = None;
                if let Some(cmd) = chosen {
                    return self.run(cmd);
                }
            }
            _ => {}
        }
        Vec::new()
    }

    /// A palette command does exactly what its own key does; it adds no capability.
    pub fn run(&mut self, cmd: Command) -> Vec<Effect> {
        match cmd {
            Command::Overview => self.go(Screen::Dashboard),
            Command::Incidents => self.go(Screen::Incidents),
            Command::Audit => self.go(Screen::Audit),
            Command::ControlPlane => self.go(Screen::ControlPlane),
            Command::Settings => self.go(Screen::Settings),
            Command::Diagnostics => self.go(Screen::Diagnostics),
            Command::About => self.go(Screen::About),
            Command::Help => self.go(Screen::Help),
            Command::Refresh => vec![Effect::RefreshNow],
            Command::StopControlPlane => {
                self.begin_stop();
                Vec::new()
            }
            Command::Quit => vec![Effect::Quit],
        }
    }

    /// Switches screen and asks (read-only) for what the new screen needs and does not have yet.
    fn go(&mut self, screen: Screen) -> Vec<Effect> {
        self.screen = screen;
        self.scroll = 0;
        match self.screen {
            Screen::Settings => self.load_config(),
            Screen::About => self.load_version(),
            Screen::Diagnostics => self.load_diagnostics(false),
            _ => Vec::new(),
        }
    }

    fn load_config(&mut self) -> Vec<Effect> {
        if matches!(self.config, Remote::Idle | Remote::Failed(_)) {
            self.config = Remote::Loading(self.now);
            return vec![Effect::LoadConfig];
        }
        Vec::new()
    }

    fn load_version(&mut self) -> Vec<Effect> {
        if matches!(self.version, Remote::Idle | Remote::Failed(_)) {
            self.version = Remote::Loading(self.now);
            return vec![Effect::LoadVersion];
        }
        Vec::new()
    }

    /// The first visit runs the diagnostics once; `D` runs them again (never two at a time).
    fn load_diagnostics(&mut self, again: bool) -> Vec<Effect> {
        if self.diag.is_loading() || !(again || matches!(self.diag, Remote::Idle | Remote::Failed(_))) {
            return Vec::new();
        }
        if !matches!(self.conn, Conn::Online) {
            self.say("control plane unreachable: diagnostics need its API", true);
            return Vec::new();
        }
        self.diag = Remote::Loading(self.now);
        vec![Effect::RunDiagnostics]
    }

    fn step(&mut self, delta: i32) {
        match self.screen {
            Screen::Dashboard | Screen::Incidents => {
                let last = self.rows().len().saturating_sub(1) as i32;
                self.selected = (self.selected as i32 + delta).clamp(0, last) as usize;
            }
            _ => {
                self.scroll = (self.scroll as i32 + delta).clamp(0, u16::MAX as i32) as u16;
            }
        }
    }

    /// Opens the stop confirmation. Nothing is sent until the operator presses Enter on it.
    fn begin_stop(&mut self) {
        if self.busy.is_some() {
            return self.say("an action is already in progress; wait for the server's answer", true);
        }
        if !matches!(self.conn, Conn::Online) {
            return self.say("control plane unreachable: there is nothing to stop", true);
        }
        self.stop_confirm = true;
        self.stop_opened = self.now;
    }

    /// Opens a confirmation. Nothing is sent until the operator presses Enter on it.
    fn begin(&mut self, kind: ActionKind) {
        if self.busy.is_some() {
            return self.say("an action is already in progress; wait for the server's answer", true);
        }
        if !matches!(self.conn, Conn::Online) {
            return self.say("control plane unreachable: nothing can be approved or rejected", true);
        }
        let Screen::Detail(id) = &self.screen else {
            return self.say("open the incident (Enter) and review its evidence before deciding", true);
        };
        // The list does not carry the RCA; only the detail does. A decision needs the reason on screen.
        let loaded = self.snapshot.as_ref().and_then(|s| s.detail.as_ref()).is_some_and(|d| &d.incident_id == id);
        if !loaded {
            return self.say("still loading this incident's details; try again in a moment", true);
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
            category: incident.category.clone(),
            why: incident
                .rca
                .as_ref()
                .and_then(|r| r.root_cause.as_ref())
                .map(|c| c.statement.clone())
                .unwrap_or_default(),
            opened: self.now,
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
            Msg::Loaded(loaded) => self.loaded(loaded),
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

    fn loaded(&mut self, loaded: Loaded) {
        fn settle<T>(slot: &mut Remote<T>, result: Result<T, ApiError>) {
            *slot = match result {
                Ok(v) => Remote::Ready(v),
                Err(e) => Remote::Failed(e.to_string()),
            };
        }
        match loaded {
            Loaded::Config(r) => settle(&mut self.config, r),
            Loaded::Version(r) => settle(&mut self.version, r),
            Loaded::Diagnostics(r) => settle(&mut self.diag, r),
            Loaded::Stop(Ok(_)) => self.say(
                "stop requested: the control plane is shutting down; run `aiops` to start it again",
                false,
            ),
            // No answer is not a failure: the server may already be stopping. The TUI never
            // re-sends it; the connection state shows what really happened.
            Loaded::Stop(Err(ApiError::Server { code, .. })) => {
                self.stop_sent = None;
                self.say(&format!("the control plane refused to stop: {code}"), true)
            }
            Loaded::Stop(Err(_)) => self.say(
                "no answer to the stop request: the control plane may already be stopping; check its state",
                true,
            ),
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
