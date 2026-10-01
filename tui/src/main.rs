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
use aiops_tui::app::{ActionKind, App, Effect, Interest, Msg, Screen, Snapshot};
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

const HELP: &str = "aiops-tui: operator interface for the Inference Autopilot control plane

USAGE: aiops-tui [--url URL] [--interval-ms N] [--timeout-ms N]
       aiops-tui --once [--screen dashboard|incidents|audit|detail:ID] [--width N] [--height N]

  --url URL         control-plane API (loopback http only)   [default http://127.0.0.1:8080]
  --interval-ms N   refresh interval, 250..10000             [default 750]
  --timeout-ms N    per-request timeout, 200..30000          [default 2000]
  --once            print one frame of the REAL current state as text and exit
                    (exit status 3 if the control plane is unreachable)

KEYS  Up/Down navigate   Enter inspect   A approve   R reject   Esc back
      S refresh   Tab or 1/2/3 switch screens   Q or Ctrl+C quit
";

struct Opts {
    url: String,
    interval: Duration,
    timeout: Duration,
    once: bool,
    screen: String,
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
    Ok(snapshot)
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
    app.wall = wall_clock();
    match o.screen.as_str() {
        "dashboard" => {}
        "incidents" => app.screen = Screen::Incidents,
        "audit" => app.screen = Screen::Audit,
        s if s.starts_with("detail:") => app.screen = Screen::Detail(s["detail:".len()..].to_string()),
        other => {
            eprintln!("aiops-tui: unknown screen {other:?}");
            return ExitCode::from(1);
        }
    }
    let result = fetch(&client, &app.interest());
    let online = result.is_ok();
    app.apply(Msg::Poll(result));
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
    let outcome = (|| -> io::Result<()> {
        'ui: loop {
            app.now = Instant::now();
            app.wall = wall_clock();
            if let Ok(mut g) = interest.lock() {
                *g = app.interest();
            }
            while let Ok(msg) = rx.try_recv() {
                app.apply(msg);
            }
            terminal.draw(|f| ui::render(f, &app))?;
            if event::poll(Duration::from_millis(100))? {
                match event::read()? {
                    Event::Key(key) if key.kind == KeyEventKind::Press => {
                        for effect in app.handle_key(key) {
                            match effect {
                                Effect::Quit => break 'ui,
                                Effect::RefreshNow => {
                                    let _ = wake_tx.send(());
                                }
                                Effect::Approve(id) => spawn_action(&client, ActionKind::Approve, id, tx.clone()),
                                Effect::Reject(id) => spawn_action(&client, ActionKind::Reject, id, tx.clone()),
                            }
                        }
                    }
                    _ => {} // resize is handled by the next draw
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
