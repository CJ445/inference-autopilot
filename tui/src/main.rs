//! `aiops-tui`: the operator interface to a running Inference Autopilot control plane.
//!
//! It only talks HTTP to the local control-plane API. It never touches the container runtime,
//! the cluster, the GPU, the inference server, the database or the watchdog, and it cannot decide
//! anything: approve/reject are requests that the server's policy path answers.
use std::io;
use std::process::ExitCode;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use aiops_tui::api::{ApiError, Client};
use aiops_tui::app::{ActionKind, App, ClientInfo, Effect, Interest, Loaded, Msg, Screen, Snapshot};
use aiops_tui::theme::Theme;
use aiops_tui::ui;
use crossterm::event::{self, Event, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

const DEFAULT_URL: &str = "http://127.0.0.1:8080";
const AUDIT_LIMIT: u32 = 200;
/// An approval returns only once remediation AND verification finish (up to minutes).
const ACTION_TIMEOUT: Duration = Duration::from_secs(300);
const DIAGNOSTICS_TIMEOUT: Duration = Duration::from_secs(120);

const HELP: &str = "aiops-tui: operator interface for the Inference Autopilot control plane

USAGE: aiops-tui [--url URL] [--interval-ms N] [--timeout-ms N]
       aiops-tui --once [--screen home|incidents|lab|activity|system|help|detail:ID] [--width N] [--height N] [--details] [--theme NAME]

  --url URL         control-plane API (loopback http only)   [default http://127.0.0.1:8080]
  --interval-ms N   refresh interval, 250..10000             [default 750]
  --timeout-ms N    per-request timeout, 200..30000          [default 2000]
  --no-animation    no spinner animation (a static ⋯); AIOPS_NO_ANIMATION=1 does the same
  --details         start in the technical view (exact names, raw states); D toggles it
  --theme NAME      dark (tonal, truecolor: the default on a truecolor terminal), light, terminal (your
                    terminal's own colours: the default elsewhere) or mono;
                    the NO_COLOR environment variable selects mono
  --once            print one frame of the REAL current state as text and exit
                    (exit status 3 if the control plane is unreachable)

KEYS  Up/Down navigate   Enter inspect   A approve   R reject   Esc back   S refresh
      Tab or 1-4 switch areas   Ctrl+P command palette   ? help   Q or Ctrl+C quit
";

struct Opts {
    url: String,
    interval: Duration,
    timeout: Duration,
    once: bool,
    screen: String,
    details: bool,
    no_animation: bool,
    theme: Option<Theme>,
    width: u16,
    height: u16,
}

fn number<T: std::str::FromStr + PartialOrd>(name: &str, v: Option<String>, lo: T, hi: T) -> Result<T, String> {
    let v = v.ok_or_else(|| format!("{name} needs a value"))?;
    let n: T = v.parse().map_err(|_| format!("{name}: {v:?} is not a number"))?;
    if n < lo || n > hi {
        return Err(format!("{name}: {v} is out of range"));
    }
    Ok(n)
}

fn parse_args(args: Vec<String>) -> Result<Opts, String> {
    let mut o = Opts {
        url: DEFAULT_URL.into(),
        interval: Duration::from_millis(750),
        timeout: Duration::from_millis(2000),
        once: false,
        screen: "dashboard".into(),
        details: false,
        no_animation: false,
        theme: None,
        width: 120,
        height: 40,
    };
    let mut it = args.into_iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--url" => o.url = it.next().ok_or("--url needs a value")?,
            "--interval-ms" => o.interval = Duration::from_millis(number("--interval-ms", it.next(), 250, 10_000)?),
            "--timeout-ms" => o.timeout = Duration::from_millis(number("--timeout-ms", it.next(), 200, 30_000)?),
            "--once" => o.once = true,
            "--details" => o.details = true,
            "--no-animation" => o.no_animation = true,
            "--theme" => {
                let v = it.next().ok_or("--theme needs a value")?;
                o.theme = Some(Theme::parse(&v).ok_or_else(|| format!("--theme: {v:?} is not dark, light, terminal or mono"))?);
            }
            "--screen" => o.screen = it.next().ok_or("--screen needs a value")?,
            "--width" => o.width = number("--width", it.next(), 20, 500)?,
            "--height" => o.height = number("--height", it.next(), 3, 200)?,
            "-h" | "--help" => return Err(String::new()),
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    Ok(o)
}

fn wall_clock() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// One poll: status and incidents always; detail and audit only while the operator is looking.
fn fetch(client: &Client, want: &Interest) -> Result<Snapshot, ApiError> {
    let started = Instant::now();
    let practice = want.practice;
    // The practice (SIMULATION) namespace has the same shapes at a different, separate path.
    let client = &if practice { client.practice() } else { client.clone() };
    let status = client.status()?;
    let incidents = client.incidents()?;
    let detail = match &want.detail_id {
        Some(id) => match client.incident(id) {
            Ok(i) => Some(i),
            Err(ApiError::Server { status: 404, .. }) => None, // it vanished: the screen says so
            Err(e) => return Err(e),
        },
        None => None,
    };
    let audit = if want.want_audit { Some(client.audit(AUDIT_LIMIT)?) } else { None };
    let mut snapshot = Snapshot::new(status, incidents);
    snapshot.detail = detail;
    snapshot.audit = audit;
    snapshot.poll_ms = started.elapsed().as_millis();
    snapshot.practice = practice;          // so a snapshot for the other namespace is discarded
    Ok(snapshot)
}

/// A one-shot, read-only request on its own thread (the poller is never blocked by it).
fn spawn_call<F>(client: &Client, total: Duration, tx: Sender<Msg>, call: F)
where
    F: FnOnce(&Client) -> Loaded + Send + 'static,
{
    let client = client.clone().with_timeouts(Duration::from_secs(1), total);
    thread::spawn(move || {
        let _ = tx.send(Msg::Loaded(call(&client)));
    });
}

fn spawn_poller(
    client: Client,
    interval: Duration,
    interest: Arc<Mutex<Interest>>,
    tx: Sender<Msg>,
    wake: Receiver<()>,
) {
    thread::spawn(move || loop {
        let want = interest.lock().map(|g| g.clone()).unwrap_or_default();
        if tx.send(Msg::Poll(fetch(&client, &want))).is_err() {
            return;
        }
        match wake.recv_timeout(interval) {
            Ok(()) | Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return,
        }
    });
}

/// Approve/reject run off the UI thread: the server answers only after remediation completes.
fn spawn_action(client: &Client, kind: ActionKind, id: String, tx: Sender<Msg>) {
    let client = client.clone().with_timeouts(Duration::from_secs(1), ACTION_TIMEOUT);
    thread::spawn(move || {
        let result = match kind {
            ActionKind::Approve => client.approve(&id),
            ActionKind::Reject => client.reject(&id),
        };
        let _ = tx.send(Msg::Action { kind, id, result });
    });
}

fn once(o: &Opts) -> ExitCode {
    let client = match Client::new(&o.url) {
        Ok(c) => c.with_timeouts(Duration::from_secs(1), o.timeout),
        Err(e) => {
            eprintln!("aiops-tui: {e}");
            return ExitCode::from(1);
        }
    };
    let mut app = App::new(o.url.clone());
    app.details = o.details;
    app.reduce_motion = o.no_animation || std::env::var("AIOPS_NO_ANIMATION").is_ok_and(|v| !v.is_empty());
    app.theme = o.theme.unwrap_or_else(|| Theme::from_env(std::env::var("NO_COLOR").ok().as_deref(), std::env::var("COLORTERM").ok().as_deref()));
    app.wall = wall_clock();
    match o.screen.as_str() {
        "dashboard" | "home" => {}
        "incidents" => app.screen = Screen::Incidents,
        "lab" => app.screen = Screen::Lab,
        "audit" | "activity" => app.screen = Screen::Audit,
        // the old per-topic names still work: all of them are sections of System now
        "system" | "control" | "settings" | "diagnostics" | "about" => app.screen = Screen::System,
        "help" => app.screen = Screen::Help,
        s if s.starts_with("detail:") => app.screen = Screen::Detail(s["detail:".len()..].to_string()),
        other => {
            eprintln!("aiops-tui: unknown screen {other:?}");
            return ExitCode::from(1);
        }
    }
    let result = fetch(&client, &app.interest());
    let online = result.is_ok();
    app.apply(Msg::Poll(result));
    app.client = ClientInfo {
        interval_ms: Some(o.interval.as_millis() as u64),
        timeout_ms: Some(o.timeout.as_millis() as u64),
    };
    if online {
        // the same read-only requests the interactive screens make, answered synchronously
        match app.screen {
            Screen::System => {
                app.apply(Msg::Loaded(Loaded::Config(client.config())));
                app.apply(Msg::Loaded(Loaded::Version(client.version())));
                let slow = client.clone().with_timeouts(Duration::from_secs(1), DIAGNOSTICS_TIMEOUT);
                app.apply(Msg::Loaded(Loaded::Diagnostics(slow.diagnostics())));
            }
            _ => {}
        }
    }
    println!("{}", ui::render_to_string(&app, o.width, o.height));
    if online { ExitCode::SUCCESS } else { ExitCode::from(3) }
}

fn restore() {
    let _ = disable_raw_mode();
    let _ = execute!(io::stdout(), LeaveAlternateScreen);
}

fn interactive(o: &Opts) -> Result<(), String> {
    let client = Client::new(&o.url)
        .map_err(|e| e.to_string())?
        .with_timeouts(Duration::from_millis(1000), o.timeout);
    enable_raw_mode().map_err(|e| format!("this needs a terminal ({e}); use --once for plain output"))?;
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore();
        default_hook(info);
    }));
    execute!(io::stdout(), EnterAlternateScreen).map_err(|e| e.to_string())?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout())).map_err(|e| e.to_string())?;

    let (tx, rx) = mpsc::channel::<Msg>();
    let (wake_tx, wake_rx) = mpsc::channel::<()>();
    let interest = Arc::new(Mutex::new(Interest::default()));
    spawn_poller(client.clone(), o.interval, interest.clone(), tx.clone(), wake_rx);

    let mut app = App::new(o.url.clone());
    app.details = o.details;
    app.theme = o.theme.unwrap_or_else(|| Theme::from_env(std::env::var("NO_COLOR").ok().as_deref(), std::env::var("COLORTERM").ok().as_deref()));
    app.client = ClientInfo {
        interval_ms: Some(o.interval.as_millis() as u64),
        timeout_ms: Some(o.timeout.as_millis() as u64),
    };
    let outcome = (|| -> io::Result<()> {
        'ui: loop {
            app.now = Instant::now();
            app.wall = wall_clock();
            if let Ok(mut g) = interest.lock() {
                *g = app.interest();
            }
            let mut effects: Vec<Effect> = Vec::new();
            while let Ok(msg) = rx.try_recv() {
                app.apply(msg);
                effects.extend(app.take_effects());
            }
            if let Ok(size) = terminal.size() {
                app.width = size.width;
            }
            terminal.draw(|f| ui::render(f, &app))?;
            if event::poll(Duration::from_millis(100))? {
                match event::read()? {
                    Event::Key(key) if key.kind == KeyEventKind::Press => {
                        effects.extend(app.handle_key(key));
                    }
                    _ => {} // resize is handled by the next draw
                }
            }
            for effect in effects {
                    match effect {
                        Effect::Quit => break 'ui,
                        Effect::RefreshNow => {
                            let _ = wake_tx.send(());
                        }
                        Effect::LoadConfig => {
                            spawn_call(&client, Duration::from_secs(5), tx.clone(), |c| Loaded::Config(c.config()))
                        }
                        Effect::LoadVersion => {
                            spawn_call(&client, Duration::from_secs(5), tx.clone(), |c| Loaded::Version(c.version()))
                        }
                        // the doctor reads real infrastructure: it can take a while
                        Effect::RunDiagnostics => spawn_call(&client, DIAGNOSTICS_TIMEOUT, tx.clone(), |c| {
                            Loaded::Diagnostics(c.diagnostics())
                        }),
                        // sent exactly once per confirmation; there is no retry anywhere
                        Effect::StopControlPlane => {
                            spawn_call(&client, Duration::from_secs(10), tx.clone(), |c| Loaded::Stop(c.stop_control_plane()))
                        }
                        // A REAL fault: sent once, after the operator confirmed the dialog.
                        Effect::PauseWorkload { target, seconds } => {
                            spawn_call(&client, Duration::from_secs(20), tx.clone(), move |c| {
                                Loaded::FaultPaused(c.fault_pause(&target, seconds))
                            })
                        }
                        Effect::ResumeWorkload => {
                            spawn_call(&client, Duration::from_secs(20), tx.clone(), |c| {
                                Loaded::FaultResumed(c.fault_resume())
                            })
                        }
                        Effect::StartPractice => spawn_call(&client, Duration::from_secs(10), tx.clone(), |c| {
                            Loaded::PracticeStarted(c.practice_start())
                        }),
                        Effect::InjectFault => spawn_call(&client, Duration::from_secs(10), tx.clone(), |c| {
                            Loaded::PracticeFault(c.practice_fault())
                        }),
                        Effect::EndPractice => spawn_call(&client, Duration::from_secs(10), tx.clone(), |c| {
                            Loaded::PracticeStopped(c.practice_stop())
                        }),
                        // a decision goes to the namespace the operator is looking at
                        Effect::Approve(id) => {
                            let c = if app.practice { client.practice() } else { client.clone() };
                            spawn_action(&c, ActionKind::Approve, id, tx.clone())
                        }
                        Effect::Reject(id) => {
                            let c = if app.practice { client.practice() } else { client.clone() };
                            spawn_action(&c, ActionKind::Reject, id, tx.clone())
                        }
                    }
                }
        }
        Ok(())
    })();
    restore();
    outcome.map_err(|e| e.to_string())
}

fn main() -> ExitCode {
    let opts = match parse_args(std::env::args().skip(1).collect()) {
        Ok(o) => o,
        Err(msg) if msg.is_empty() => {
            print!("{HELP}");
            return ExitCode::SUCCESS;
        }
        Err(msg) => {
            eprintln!("aiops-tui: {msg}\n\n{HELP}");
            return ExitCode::from(1);
        }
    };
    if opts.once {
        return once(&opts);
    }
    match interactive(&opts) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("aiops-tui: {e}");
            ExitCode::from(1)
        }
    }
}
