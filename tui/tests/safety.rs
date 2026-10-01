//! Static guarantees: the TUI is only an HTTP client of the control plane.
use std::fs;
use std::path::Path;

fn sources() -> Vec<(String, String)> {
    let mut out = Vec::new();
    for entry in fs::read_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("src")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().map_or(false, |e| e == "rs") {
            out.push((path.display().to_string(), fs::read_to_string(&path).unwrap()));
        }
    }
    out
}

#[test]
fn the_tui_cannot_start_processes_or_reach_infrastructure() {
    for (file, text) in sources() {
        for forbidden in [
            "process::Command", "Command::new", ".spawn_process", "kubectl", "nvidia", "sqlite", "docker", "Docker",
            "/var/run", "unsafe ", "std::fs::write", "File::create", "OpenOptions",
        ] {
            assert!(!text.contains(forbidden), "{file} mentions {forbidden:?}");
        }
    }
}

#[test]
fn the_only_network_code_is_the_loopback_http_client() {
    for (file, text) in sources() {
        if !file.ends_with("http.rs") {
            assert!(!text.contains("TcpStream"), "{file} opens sockets itself");
            assert!(!text.contains("UdpSocket"), "{file} opens sockets itself");
        }
    }
}

#[test]
fn dependencies_stay_minimal() {
    let manifest = fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml")).unwrap();
    let deps = manifest.split("[dependencies]").nth(1).unwrap();
    let names: Vec<_> = deps
        .lines()
        .filter(|l| l.contains('=') && !l.trim_start().starts_with('#'))
        .map(|l| l.split('=').next().unwrap().trim().to_string())
        .collect();
    assert_eq!(names, vec!["ratatui", "crossterm", "serde", "serde_json"]);
}

#[test]
fn there_is_no_fake_or_demo_mode_in_the_binary() {
    for (file, text) in sources() {
        let lower = text.to_lowercase();
        for forbidden in ["--demo", "--fake", "--mock", "demo_mode", "fake_data", "sample_data"] {
            assert!(!lower.contains(forbidden), "{file} mentions {forbidden:?}");
        }
    }
}

#[test]
fn the_tui_has_no_watchdog_controls() {
    for (file, text) in sources() {
        let lower = text.to_lowercase();
        for forbidden in ["disarm", "/watchdog", "api/v1/watchdog", "reconfigure"] {
            assert!(!lower.contains(forbidden), "{file} mentions {forbidden:?}");
        }
    }
}
