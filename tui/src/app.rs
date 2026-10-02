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
/// The words the server uses when it refuses an approval because the problem is gone (D-14). The
/// TUI recognises the refusal by this text; `tests/test_stale_approval.py` pins the server's message.
pub const REFUSED_STALE: &str = "no longer present";

/// A success notice fades after this long. An error stays until the operator presses a key.
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
    /// True if this was fetched from the practice (SIMULATION) namespace. A snapshot for the other
    /// namespace (a poll that was in flight when practice started or ended) is discarded.
    pub practice: bool,
}

impl Snapshot {
    pub fn new(status: Status, incidents: Vec<Incident>) -> Snapshot {
        Snapshot { status, incidents, detail: None, audit: None, poll_ms: 0, fetched_at: None, practice: false }
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
    /// The Lab: run a recovery test, or (when offered) inject a real fault.
    Lab,
    Detail(String),
    /// Activity: what the system did and decided.
    Audit,
    /// The control plane, its diagnostics, its read-only configuration and About, in one place.
    System,
    Help,
}

/// The five areas: the order `Tab` walks and the navigation strip lists; `1`..`5` jump to them.
/// Help is not an area: it is on `?`.
pub const SCREEN_ORDER: [Screen; 5] =
    [Screen::Dashboard, Screen::Incidents, Screen::Lab, Screen::Audit, Screen::System];

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
    Lab,
    Audit,
    System,
    Panel,
    Evidence,
    Help,
    Refresh,
    StopControlPlane,
    Practice,
    ExitPractice,
    BreakWorkload,
    ResumeWorkload,
    ToggleDetails,
    Quit,
}

/// What the palette may offer right now (it never lists what cannot be done).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Offer {
    pub practicing: bool,
    /// The server offers the guarded real fault, none is active, and this is the real system.
    pub can_break: bool,
    /// A real fault is active and can be ended now.
    pub can_resume: bool,
    /// An incident is waiting for the operator's decision.
    pub awaiting: bool,
}

/// How long the paused workload is told to stay paused at most (the server bounds this too).
pub const FAULT_SECONDS: u32 = 120;

impl Command {
    pub const ALL: [Command; 16] = [
        Command::Practice,
        Command::BreakWorkload,
        Command::Incidents,
        Command::Audit,
        Command::Lab,
        Command::Overview,
        Command::System,
        Command::Evidence,
        Command::Panel,
        Command::ToggleDetails,
        Command::StopControlPlane,
        Command::ExitPractice,
        Command::ResumeWorkload,
        Command::Help,
        Command::Refresh,
        Command::Quit,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Command::Overview => "Go to Home",
            Command::Incidents => "View incidents",
            Command::Lab => "Open the Lab",
            Command::Audit => "View activity",
            Command::System => "Show system status",
            Command::Help => "Help",
            Command::Refresh => "Refresh",
            Command::StopControlPlane => "Stop control plane…",
            Command::Practice => "Run a recovery test",
            Command::ExitPractice => "Leave the recovery test",
            Command::BreakWorkload => "Inject a real fault (pause the workload)…",
            Command::ResumeWorkload => "Resume the workload now",
            Command::ToggleDetails => "Show technical details",
            Command::Evidence => "Show or hide the evidence",
            Command::Panel => "Show or hide the workload panel",
            Command::Quit => "Quit",
        }
    }
}

impl Command {
    /// The group a command is listed under in the palette.
    pub fn category(self) -> &'static str {
        match self {
            Command::Practice | Command::BreakWorkload | Command::ExitPractice | Command::ResumeWorkload => "Test",
            Command::Overview | Command::Incidents | Command::Lab | Command::Audit | Command::System => "Go to",
            Command::Evidence | Command::Panel | Command::ToggleDetails => "View",
            Command::Refresh | Command::StopControlPlane | Command::Help | Command::Quit => "System",
        }
    }

    /// The key that does the same thing, shown at the right of its row (empty when there is none).
    pub fn shortcut(self) -> &'static str {
        match self {
            Command::Practice => "R",
            Command::BreakWorkload => "F",
            Command::Overview => "1",
            Command::Incidents => "2",
            Command::Lab => "3",
            Command::Audit => "4",
            Command::System => "5",
            Command::Evidence => "E",
            Command::Panel => "W",
            Command::ToggleDetails => "D",
            Command::Refresh => "S",
            Command::Help => "?",
            Command::Quit => "Q",
            Command::ExitPractice | Command::ResumeWorkload | Command::StopControlPlane => "",
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
    pub fn matches(&self, offer: Offer) -> Vec<Command> {
        let q = self.query.to_lowercase();
        let words: Vec<&str> = q.split_whitespace().collect();
        let available = |c: &Command| match c {
            Command::ExitPractice => offer.practicing,
            Command::BreakWorkload => offer.can_break,
            Command::ResumeWorkload => offer.can_resume,
            _ => true,
        };
        let mut all: Vec<Command> = Command::ALL.into_iter().filter(available).collect();
        // grouped by category, in a stable order
        let order = ["Test", "Go to", "View", "System"];
        all.sort_by_key(|c| order.iter().position(|o| *o == c.category()).unwrap_or(9));
        if words.is_empty() {
            // the suggestions for this situation come first
            let suggested = Palette::suggested(offer);
            let mut out: Vec<Command> = suggested.clone();
            out.extend(all.into_iter().filter(|c| !suggested.contains(c)));
            return out;
        }
        all.into_iter().filter(|c| { let label = c.label().to_lowercase(); words.iter().all(|w| label.contains(w)) }).collect()
    }

    /// What is most likely wanted right now (listed first when nothing has been typed).
    pub fn suggested(offer: Offer) -> Vec<Command> {
        let mut s = if offer.can_resume {
            vec![Command::ResumeWorkload, Command::Incidents]
        } else if offer.practicing {
            vec![Command::ExitPractice, Command::Incidents]
        } else if offer.awaiting {
            vec![Command::Incidents, Command::Evidence]
        } else {
            vec![Command::Practice, Command::BreakWorkload, Command::Incidents]
        };
        s.retain(|c| match c {
            Command::BreakWorkload => offer.can_break,
            _ => true,
        });
        s
    }
}

/// Client-local facts the TUI was started with (shown read-only in Settings).
#[derive(Debug, Clone, Default)]
pub struct ClientInfo {
    pub interval_ms: Option<u64>,
    pub timeout_ms: Option<u64>,
}

/// The keys that do something, in the order Help lists them. `tests/help.rs` drives every row.
pub const KEYMAP: [(&str, &str); 17] = [
    ("↑ ↓", "Navigate / scroll"),
    ("Enter", "Review the selected incident"),
    ("Esc", "Back / cancel / close (while testing, on Home or the Lab: leave the test)"),
    ("Tab →", "Next area"),
    ("←", "Previous area"),
    ("1-5", "Jump to Home, Incidents, Lab, Activity or System"),
    ("?", "Help"),
    ("Ctrl+P", "Command palette"),
    ("R", "Run a recovery test (simulated; nothing real is touched). On an incident: reject"),
    ("F", "Inject a real fault (asks first). While testing: break the simulated model"),
    ("I", "Go to Incidents"),
    ("D", "Technical details on or off (System: run the checks again)"),
    ("S", "Refresh now"),
    ("A", "Go to Activity. On an incident's page, once it awaits a decision: approve"),
    ("X", "Stop the control plane (System)"),
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
    /// The decision is about a simulated incident: nothing real will be restarted.
    pub practice: bool,
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
    /// Practice (SIMULATION) control. They address only the simulation; nothing real.
    StartPractice,
    InjectFault,
    EndPractice,
    /// A REAL fault: pause the managed workload. Sent only after an explicit confirmation.
    PauseWorkload { target: String, seconds: u32 },
    ResumeWorkload,
}

#[derive(Debug)]
pub enum Loaded {
    Config(Result<ConfigInfo, ApiError>),
    Version(Result<VersionInfo, ApiError>),
    Diagnostics(Result<Diagnostics, ApiError>),
    Stop(Result<StopAck, ApiError>),
    PracticeStarted(Result<(), ApiError>),
    PracticeFault(Result<(), ApiError>),
    PracticeStopped(Result<(), ApiError>),
    FaultPaused(Result<(), ApiError>),
    FaultResumed(Result<(), ApiError>),
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
    /// Poll the practice (SIMULATION) namespace instead of the real one.
    pub practice: bool,
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
    /// True while a practice (SIMULATION) session is on screen. Everything shown is simulated.
    pub practice: bool,
    /// Show the technical views (exact names, raw states, metrics) instead of the plain ones.
    pub details: bool,
    /// Effects the app itself asks for (not a key press): drained by the event loop.
    pub pending: Vec<Effect>,
    /// A recovery test was just started: inject its simulated fault once the server says healthy.
    auto_fault: bool,
    /// How colours are shown. `App::new` is the semantic (Dark) palette the tests inspect; the
    /// binary chooses the real one from `--theme` / `NO_COLOR`.
    pub theme: crate::theme::Theme,
    pub stop_confirm: bool,
    pub stop_opened: Instant,
    /// The dialog that asks before a REAL workload is paused.
    pub fault_confirm: bool,
    /// The server refused an approval because the problem was gone (D-14): said in a dialog, not
    /// in a line that fades, so that "nothing was restarted" is read.
    pub refusal: Option<String>,
    /// When the app started: drives the spinner (a frame every 80 ms).
    pub started: Instant,
    /// Show the evidence under every stage of the stream (E), not only the one the loop is at.
    pub expanded: bool,
    /// The workload panel: `None` is automatic (open when the terminal is wide), `Some` is the operator's choice.
    pub panel: Option<bool>,
    /// The row the cursor is on in the Lab's list of tests.
    pub lab_cursor: usize,
    /// Reduced motion: no spinner animation (a static `⋯`).
    pub reduce_motion: bool,
    /// The terminal's width in columns, as last drawn (the event loop sets it): the workload panel
    /// opens by itself only on a wide terminal, so W needs to know which way to toggle.
    pub width: u16,
    /// The action the server proposed for each incident, remembered from earlier polls: once an
    /// incident is approved the server no longer lists its proposal, but the story still names it.
    actions: std::collections::HashMap<String, String>,
    pub fault_opened: Instant,
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
            practice: false,
            details: false,
            pending: Vec::new(),
            auto_fault: false,
            theme: crate::theme::Theme::Dark,
            stop_confirm: false,
            stop_opened: Instant::now(),
            fault_confirm: false,
            refusal: None,
            started: Instant::now(),
            expanded: false,
            panel: None,
            lab_cursor: 0,
            reduce_motion: false,
            width: 120,
            actions: std::collections::HashMap::new(),
            fault_opened: Instant::now(),
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
                // the recovery test's story needs the recorded checks of its incident
                Screen::Lab | Screen::Dashboard => self.decision_target(),
                _ => None,
            },
            want_audit: self.screen == Screen::Audit,
            practice: self.practice,
        }
    }

    pub fn active_notice(&self) -> Option<&Notice> {
        self.notice.as_ref().filter(|n| n.is_error || self.now.saturating_duration_since(n.at) < NOTICE_TTL)
    }

    // ---- input ---------------------------------------------------------------------------------

    fn say(&mut self, text: &str, is_error: bool) {
        self.notice = Some(Notice { text: text.to_string(), is_error, at: self.now });
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> Vec<Effect> {
        if self.notice.as_ref().is_some_and(|n| n.is_error) {
            self.notice = None;                 // an error was read: the next key acknowledges it
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        if ctrl && key.code == KeyCode::Char('c') {
            return vec![Effect::Quit];
        }
        if self.refusal.take().is_some() {
            return Vec::new();                  // any key reads the refusal away
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
        if self.fault_confirm {
            match key.code {
                KeyCode::Enter if self.now.saturating_duration_since(self.fault_opened) < CONFIRM_GUARD => {}
                KeyCode::Enter => {
                    self.fault_confirm = false;
                    return match self.fault_target() {
                        Some(target) => vec![Effect::PauseWorkload { target, seconds: FAULT_SECONDS }],
                        None => Vec::new(),
                    };
                }
                KeyCode::Esc => self.fault_confirm = false,
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
            KeyCode::Char(c @ '1'..='5') => {
                fx = self.go(SCREEN_ORDER[c as usize - '1' as usize].clone());
            }
            KeyCode::Char('?') => fx = self.go(Screen::Help),
            KeyCode::Up => self.step(-1),
            KeyCode::Down => self.step(1),
            KeyCode::PageUp => self.step(-8),
            KeyCode::PageDown => self.step(8),
            KeyCode::Enter => {
                if self.screen == Screen::Lab && !self.practice && self.active_fault().is_none() {
                    // the Lab's list: Enter runs the test the cursor is on
                    if self.lab_cursor == 0 {
                        fx = self.begin_practice();
                    } else {
                        self.begin_fault();
                    }
                } else if matches!(self.screen, Screen::Dashboard | Screen::Incidents | Screen::Lab) {
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
                Screen::Dashboard | Screen::Lab if self.practice => fx = vec![Effect::EndPractice],
                Screen::Dashboard => {}
                _ => fx = self.go(Screen::Dashboard),
            },
            KeyCode::Char('i') | KeyCode::Char('I') => fx = self.go(Screen::Incidents),
            KeyCode::Char('e') | KeyCode::Char('E') => self.expanded = !self.expanded,
            KeyCode::Char('w') | KeyCode::Char('W') => self.panel = Some(!self.panel_open(self.width)),
            KeyCode::Char('f') | KeyCode::Char('F') if self.practice => fx = self.inject_fault(),
            // F opens the real-fault DIALOG (nothing is sent until Enter after the guard), and only
            // on the screens that show the action, so a stray F elsewhere does nothing.
            KeyCode::Char('f') | KeyCode::Char('F') if matches!(self.screen, Screen::Dashboard | Screen::Lab) => {
                self.begin_fault()
            }
            // A and R decide an incident only on its page (the Incidents list explains); on the
            // other screens they are the quick actions: Activity, and run a recovery test.
            KeyCode::Char('a') | KeyCode::Char('A') if self.decides_here() => self.begin(ActionKind::Approve),
            KeyCode::Char('r') | KeyCode::Char('R') if self.decides_here() => self.begin(ActionKind::Reject),
            KeyCode::Char('a') | KeyCode::Char('A') => fx = self.go(Screen::Audit),
            KeyCode::Char('r') | KeyCode::Char('R') => fx = self.begin_practice(),
            KeyCode::Char('d') | KeyCode::Char('D') if self.screen == Screen::System => {
                fx = self.load_diagnostics(true)
            }
            KeyCode::Char('d') | KeyCode::Char('D') => self.details = !self.details,
            KeyCode::Char('x') | KeyCode::Char('X') if self.screen == Screen::System => {
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
        let offer = self.offer();
        let Some(p) = self.palette.as_mut() else { return Vec::new() };
        match key.code {
            KeyCode::Esc => self.palette = None,
            KeyCode::Backspace => {
                p.query.pop();
                p.selected = 0;
            }
            KeyCode::Up => p.selected = p.selected.saturating_sub(1),
            KeyCode::Down => {
                let last = p.matches(offer).len().saturating_sub(1);
                p.selected = (p.selected + 1).min(last);
            }
            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                p.query.push(c);
                p.selected = 0;
            }
            KeyCode::Enter => {
                let chosen = p.matches(offer).get(p.selected).copied();
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
            Command::Lab => self.go(Screen::Lab),
            Command::Panel => {
                self.panel = Some(!self.panel_open(self.width));
                Vec::new()
            }
            Command::Evidence => {
                self.expanded = !self.expanded;
                Vec::new()
            }
            Command::Incidents => self.go(Screen::Incidents),
            Command::Audit => self.go(Screen::Audit),
            Command::System => self.go(Screen::System),
            Command::Help => self.go(Screen::Help),
            Command::Refresh => vec![Effect::RefreshNow],
            Command::StopControlPlane => {
                self.begin_stop();
                Vec::new()
            }
            Command::Practice => self.begin_practice(),
            Command::ExitPractice => vec![Effect::EndPractice],
            Command::BreakWorkload => {
                self.begin_fault();
                Vec::new()
            }
            Command::ResumeWorkload => {
                if self.offer().can_resume {
                    vec![Effect::ResumeWorkload]
                } else {
                    self.say("no fault is active", true);
                    Vec::new()
                }
            }
            Command::ToggleDetails => {
                self.details = !self.details;
                Vec::new()
            }
            Command::Quit => vec![Effect::Quit],
        }
    }

    pub fn offer(&self) -> Offer {
        let faults = self.snapshot.as_ref().and_then(|s| s.status.faults.as_ref());
        let online = matches!(self.conn, Conn::Online);
        Offer {
            practicing: self.practice,
            can_break: online
                && !self.practice
                && faults.map_or(false, |f| f.available && f.active.is_none()),
            can_resume: online && !self.practice && faults.map_or(false, |f| f.active.is_some()),
            awaiting: self.snapshot.as_ref().is_some_and(|s| s.incidents.iter().any(|i| i.status == "POLICY_CHECK")),
        }
    }

    /// The workload the server says it manages: the only thing a fault may ever name.
    fn fault_target(&self) -> Option<String> {
        self.snapshot.as_ref()?.status.info.workload.clone()
    }

    /// Opens the confirmation. Nothing is sent until the operator presses Enter on it.
    fn begin_fault(&mut self) {
        if !self.offer().can_break {
            return self.say("a real fault is not available right now", true);
        }
        if self.fault_target().is_none() {
            return self.say("the control plane did not name its workload; nothing to pause", true);
        }
        self.fault_confirm = true;
        self.fault_opened = self.now;
    }

    /// The fault in progress, if any, from the server's last report.
    pub fn active_fault(&self) -> Option<&crate::model::ActiveFault> {
        if self.practice {
            return None;
        }
        self.snapshot.as_ref()?.status.faults.as_ref()?.active.as_ref()
    }

    /// Asks the server for a fresh practice session (replacing any current one).
    fn begin_practice(&mut self) -> Vec<Effect> {
        if !matches!(self.conn, Conn::Online) {
            self.say("control plane unreachable: practice needs its API", true);
            return Vec::new();
        }
        if self.busy.is_some() {
            self.say("an action is already in progress; wait for the server's answer", true);
            return Vec::new();
        }
        vec![Effect::StartPractice]
    }

    /// The simulated fault, only into a healthy simulated model (the server enforces it too).
    fn inject_fault(&mut self) -> Vec<Effect> {
        let stage = self.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.as_str());
        if stage == Some("healthy") {
            return vec![Effect::InjectFault];
        }
        self.say("the simulated fault can only be injected while the model is healthy", true);
        Vec::new()
    }

    fn enter_practice(&mut self) {
        self.practice = true;
        self.reset_view();
    }

    fn leave_practice(&mut self) {
        self.practice = false;
        self.auto_fault = false;
        self.reset_view();
    }

    /// A different namespace is a different world: nothing from the old one may stay on screen.
    fn reset_view(&mut self) {
        self.actions.clear();               // a different namespace: nothing remembered carries over
        self.snapshot = None;
        self.screen = Screen::Dashboard;
        self.selected = 0;
        self.scroll = 0;
        self.confirm = None;
        self.busy = None;
        self.palette = None;
    }

    /// Switches screen and asks (read-only) for what the new screen needs and does not have yet.
    fn go(&mut self, screen: Screen) -> Vec<Effect> {
        self.screen = screen;
        self.scroll = 0;
        match self.screen {
            Screen::System => {
                let mut fx = self.load_config();
                fx.extend(self.load_version());
                fx.extend(self.load_diagnostics(false));
                fx
            }
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
            Screen::Lab if !self.practice && self.active_fault().is_none() => {
                self.lab_cursor = (self.lab_cursor as i32 + delta).clamp(0, 1) as usize;
            }
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
    /// The incident a decision key would be about, where the stream of one incident is on screen:
    /// its own page, Home (the open incident) or the Lab (the test or fault's incident).
    pub fn decision_target(&self) -> Option<String> {
        match &self.screen {
            Screen::Detail(id) => Some(id.clone()),
            Screen::Dashboard => self.rows().into_iter().find(|i| !is_closed(&i.status)).map(|i| i.incident_id.clone()),
            // the Lab's menu is not about any incident; its stream (a test or a real fault) is
            Screen::Lab if self.practice || self.active_fault().is_some() => self.rows().first().map(|i| i.incident_id.clone()),
            _ => None,
        }
    }

    /// A and R decide here only when an incident is on screen that is waiting for a decision (or
    /// on the Incidents list, where they explain).
    fn decides_here(&self) -> bool {
        match &self.screen {
            Screen::Incidents => true,
            Screen::Detail(_) => true,
            Screen::Dashboard | Screen::Lab => self.decision_target().is_some_and(|id| {
                self.snapshot.as_ref().is_some_and(|s| s.incidents.iter().any(|i| i.incident_id == id && i.status == "POLICY_CHECK"))
            }),
            _ => false,
        }
    }

    /// Whether the workload panel is open on a terminal `width` columns wide.
    pub fn panel_open(&self, width: u16) -> bool {
        self.panel.unwrap_or(width >= 110)
    }

    fn begin(&mut self, kind: ActionKind) {
        if self.busy.is_some() {
            return self.say("an action is already in progress; wait for the server's answer", true);
        }
        if !matches!(self.conn, Conn::Online) {
            return self.say("control plane unreachable: nothing can be approved or rejected", true);
        }
        let Some(target) = self.decision_target().filter(|_| !matches!(self.screen, Screen::Incidents)) else {
            return self.say("open the incident (Enter) and review its evidence before deciding", true);
        };
        let id = &target;
        // The list does not carry the RCA; only the detail does. A decision needs the reason on screen.
        let loaded = self.snapshot.as_ref().and_then(|s| s.detail.as_ref()).is_some_and(|d| &d.incident_id == id);
        if !loaded {
            return self.say("still loading this incident's details; try again in a moment", true);
        }
        let Some(incident) = self.snapshot.as_ref().and_then(|s| s.detail.as_ref()).filter(|d| &d.incident_id == id) else {
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
            practice: self.practice,
            opened: self.now,
        };
        self.confirm = Some(confirm);
    }

    // ---- messages from the poller and from actions -------------------------------------------

    /// The action the server proposed for this incident (now, or when it last listed it).
    pub fn proposed_action(&self, id: &str) -> Option<&str> {
        self.actions.get(id).map(String::as_str)
    }

    /// The effects the app asked for on its own (see `pending`).
    pub fn take_effects(&mut self) -> Vec<Effect> {
        std::mem::take(&mut self.pending)
    }

    /// A recovery test is one key: once the server reports the simulated model healthy, ask it to
    /// break it. Two existing calls in sequence; the server still enforces "only into a healthy
    /// model", and nothing is shown that the server did not report.
    fn maybe_inject_test_fault(&mut self) {
        if !self.auto_fault || !self.practice {
            return;
        }
        match self.snapshot.as_ref().and_then(|s| s.status.practice.as_ref()).map(|p| p.stage.as_str()) {
            Some("healthy") => {
                self.auto_fault = false;
                self.pending.push(Effect::InjectFault);
            }
            Some(_) => self.auto_fault = false,     // already past healthy: nothing to inject
            None => {}
        }
    }

    pub fn apply(&mut self, msg: Msg) {
        match msg {
            Msg::Poll(Ok(snapshot)) if snapshot.practice != self.practice => {} // other namespace
            Msg::Poll(Err(ApiError::Server { ref code, .. })) if self.practice && code == "NO_PRACTICE" => {
                self.leave_practice();
                self.say("the practice session ended; this is the real system again", false);
            }
            Msg::Poll(Ok(mut snapshot)) => {
                snapshot.fetched_at = Some(self.now);
                for i in &snapshot.incidents {
                    if let Some(p) = &i.proposal {
                        self.actions.insert(i.incident_id.clone(), p.action.clone());
                    }
                }
                self.update_rates(&snapshot.status);
                self.conn = Conn::Online;
                self.snapshot = Some(snapshot);
                self.selected = self.selected.min(self.rows().len().saturating_sub(1));
                self.maybe_inject_test_fault();
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
                    Err(ApiError::Server { ref message, .. }) if message.contains(REFUSED_STALE) => {
                        self.refusal = Some(id);
                    }
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
            Loaded::PracticeStarted(Ok(())) => {
                self.enter_practice();
                self.screen = Screen::Lab;
                self.auto_fault = true;      // the fault follows as soon as the server says healthy
                self.say("recovery test started: everything here is simulated", false);
            }
            Loaded::PracticeStarted(Err(e)) => self.say(&format!("could not start the recovery test: {e}"), true),
            Loaded::PracticeFault(Ok(())) => self.say("simulated fault injected: watch the detector", false),
            Loaded::PracticeFault(Err(e)) => self.say(&format!("could not inject the fault: {e}"), true),
            Loaded::PracticeStopped(result) => {
                self.leave_practice();     // leave either way: the simulation holds nothing real
                match result {
                    Ok(()) => self.say("left practice: this is the real system again", false),
                    Err(e) => self.say(&format!("left practice (the server did not confirm: {e})"), true),
                }
            }
            Loaded::FaultPaused(Ok(())) => self.say(
                "the real workload is paused; it resumes by itself (Ctrl+P, then Resume the workload now, ends it sooner)",
                false,
            ),
            Loaded::FaultPaused(Err(ApiError::Server { message, .. })) => {
                self.say(&format!("the fault was refused: {message}"), true)
            }
            Loaded::FaultPaused(Err(_)) => self.say(
                "no answer to the fault request: check the workload; a fault that did start ends by itself",
                true,
            ),
            Loaded::FaultResumed(Ok(())) => self.say("the workload was resumed", false),
            Loaded::FaultResumed(Err(e)) => self.say(&format!("could not resume the workload: {e}"), true),
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
