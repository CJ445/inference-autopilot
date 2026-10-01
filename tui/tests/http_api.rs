//! The API client against a real in-process HTTP server that scripts exact responses.
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use aiops_tui::api::{ApiError, Client};

const STATUS: &str = include_str!("fixtures/status_healthy.json");
const PENDING: &str = include_str!("fixtures/incident_pending.json");
const RESOLVED: &str = include_str!("fixtures/incident_resolved.json");
const AUDIT: &str = include_str!("fixtures/audit.json");
const ERROR_409: &str = include_str!("fixtures/error_409.json");

enum Reply {
    Raw(Vec<u8>),
    After(Duration, Vec<u8>),
    Close,
}

fn http(status: u16, body: &str) -> Vec<u8> {
    format!(
        "HTTP/1.0 {} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
        status,
        body.len(),
        body
    )
    .into_bytes()
}

struct Fake {
    port: u16,
    requests: Arc<Mutex<Vec<String>>>,
    heads: Arc<Mutex<Vec<String>>>,
}

impl Fake {
    fn client(&self) -> Client {
        Client::new(&format!("http://127.0.0.1:{}", self.port))
            .unwrap()
            .with_timeouts(Duration::from_millis(500), Duration::from_millis(600))
    }
    fn requests(&self) -> Vec<String> {
        self.requests.lock().unwrap().clone()
    }
    /// The full request heads (request line and headers), for header assertions.
    fn heads(&self) -> Vec<String> {
        self.heads.lock().unwrap().clone()
    }
}

/// One connection per scripted reply; records each request line.
fn fake(replies: Vec<Reply>) -> Fake {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let seen = requests.clone();
    let heads = Arc::new(Mutex::new(Vec::new()));
    let seen_heads = heads.clone();
    thread::spawn(move || {
        for reply in replies {
            let Ok((mut conn, _)) = listener.accept() else { return };
            let mut head = Vec::new();
            let mut buf = [0u8; 1024];
            while !head.windows(4).any(|w| w == b"\r\n\r\n") {
                match conn.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => head.extend_from_slice(&buf[..n]),
                }
            }
            let line = String::from_utf8_lossy(&head).lines().next().unwrap_or("").to_string();
            seen.lock().unwrap().push(line);
            seen_heads.lock().unwrap().push(String::from_utf8_lossy(&head).into_owned());
            match reply {
                Reply::Raw(bytes) => {
                    let _ = conn.write_all(&bytes);
                }
                Reply::After(delay, bytes) => {
                    thread::sleep(delay);
                    let _ = conn.write_all(&bytes);
                }
                Reply::Close => {}
            }
        }
    });
    Fake { port, requests, heads }
}

#[test]
fn status_is_fetched_with_a_plain_get_and_parsed() {
    let f = fake(vec![Reply::Raw(http(200, STATUS))]);
    let s = f.client().status().unwrap();
    assert_eq!(s.health, "HEALTHY");
    assert_eq!(s.info.workload.as_deref(), Some("vllm"));
    assert_eq!(s.audit.as_ref().map(|a| a.valid), Some(true));
    assert_eq!(s.watchdog.as_ref().map(|w| w.state.as_str()), Some("armed"));
    assert_eq!(f.requests(), vec!["GET /api/v1/status HTTP/1.1"]);
}

#[test]
fn incidents_and_details_and_audit_parse() {
    let list = format!("{{\"incidents\": [{}, {}]}}", PENDING, RESOLVED);
    let f = fake(vec![
        Reply::Raw(http(200, &list)),
        Reply::Raw(http(200, RESOLVED)),
        Reply::Raw(http(200, AUDIT)),
    ]);
    let c = f.client();
    let incidents = c.incidents().unwrap();
    assert_eq!(incidents.len(), 2);
    assert_eq!(incidents[0].category, "INFERENCE_UNRESPONSIVE");
    assert!(incidents[0].proposal.is_some() && incidents[1].proposal.is_none());
    let detail = c.incident("inc_001").unwrap();
    assert_eq!(detail.status, "RESOLVED");
    let checks = detail.verification.unwrap().checks.unwrap();
    assert_eq!(checks.get("inference_probe_stable"), Some(&true));
    let audit = c.audit(50).unwrap();
    assert!(audit.valid && audit.events.len() == 4);
    assert_eq!(
        f.requests(),
        vec![
            "GET /api/v1/incidents HTTP/1.1",
            "GET /api/v1/incidents/inc_001 HTTP/1.1",
            "GET /api/v1/audit?limit=50 HTTP/1.1"
        ]
    );
}

#[test]
fn approve_and_reject_send_exactly_the_documented_posts() {
    let f = fake(vec![Reply::Raw(http(200, RESOLVED)), Reply::Raw(http(200, PENDING))]);
    let c = f.client();
    assert_eq!(c.approve("inc_001").unwrap().status, "RESOLVED");
    assert!(c.reject("inc_001").is_ok());
    assert_eq!(
        f.requests(),
        vec![
            "POST /api/v1/incidents/inc_001/remediation/approve HTTP/1.1",
            "POST /api/v1/incidents/inc_001/remediation/reject HTTP/1.1"
        ]
    );
}

#[test]
fn only_status_and_incident_routes_are_ever_requested_by_reads() {
    let f = fake(vec![Reply::Raw(http(200, STATUS)), Reply::Raw(http(200, "{\"incidents\": []}"))]);
    let c = f.client();
    c.status().unwrap();
    c.incidents().unwrap();
    assert!(f.requests().iter().all(|l| l.starts_with("GET ")));
}

#[test]
fn the_servers_error_contract_is_surfaced_with_status_code_and_message() {
    let f = fake(vec![Reply::Raw(http(409, ERROR_409))]);
    match f.client().approve("inc_001") {
        Err(ApiError::Server { status, code, message }) => {
            assert_eq!(status, 409);
            assert_eq!(code, "POLICY_DENIED");
            assert!(message.contains("Another remediation"));
        }
        other => panic!("expected a server error, got {:?}", other),
    }
}

#[test]
fn a_non_json_error_body_still_yields_a_server_error() {
    let f = fake(vec![Reply::Raw(http(502, "<html>bad gateway</html>"))]);
    match f.client().status() {
        Err(ApiError::Server { status, code, .. }) => {
            assert_eq!(status, 502);
            assert_eq!(code, "HTTP_502");
        }
        other => panic!("expected a server error, got {:?}", other),
    }
}

#[test]
fn a_500_internal_error_is_a_server_error_not_a_panic() {
    let body = "{\"error\": {\"code\": \"INTERNAL_ERROR\", \"message\": \"Internal error.\", \"request_id\": \"req_1\"}}";
    let f = fake(vec![Reply::Raw(http(500, body))]);
    assert!(matches!(f.client().incidents(), Err(ApiError::Server { status: 500, .. })));
}

#[test]
fn connection_refused_is_offline() {
    let port = {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    }; // listener dropped: nothing listens here
    let c = Client::new(&format!("http://127.0.0.1:{}", port)).unwrap();
    assert!(matches!(c.status(), Err(ApiError::Offline(_))));
}

#[test]
fn a_slow_server_times_out_instead_of_hanging() {
    let f = fake(vec![Reply::After(Duration::from_millis(1500), http(200, STATUS))]);
    let started = Instant::now();
    assert!(matches!(f.client().status(), Err(ApiError::Timeout)));
    assert!(started.elapsed() < Duration::from_millis(1200));
}

#[test]
fn a_server_that_closes_without_answering_is_an_error_not_a_hang() {
    let f = fake(vec![Reply::Close]);
    let err = f.client().status().unwrap_err();
    assert!(matches!(err, ApiError::Offline(_) | ApiError::Malformed(_)), "{:?}", err);
}

#[test]
fn malformed_json_is_reported_as_malformed() {
    let f = fake(vec![Reply::Raw(http(200, "{ this is not json"))]);
    assert!(matches!(f.client().status(), Err(ApiError::Malformed(_))));
}

#[test]
fn a_response_missing_required_fields_is_malformed() {
    let f = fake(vec![
        Reply::Raw(http(200, "{\"last_observed_at\": null}")),        // no "health"
        Reply::Raw(http(200, "{\"incident_id\": \"inc_9\"}")),          // no category/status
    ]);
    let c = f.client();
    assert!(matches!(c.status(), Err(ApiError::Malformed(m)) if m.contains("health")));
    assert!(matches!(c.incident("inc_9"), Err(ApiError::Malformed(_))));
}

#[test]
fn optional_fields_may_be_absent() {
    let f = fake(vec![Reply::Raw(http(200, "{\"health\": \"DEGRADED\"}"))]);
    let s = f.client().status().unwrap();
    assert_eq!(s.health, "DEGRADED");
    assert!(s.last_observation.is_none() && s.watchdog.is_none() && s.audit.is_none());
}

#[test]
fn unknown_fields_from_a_newer_server_are_ignored() {
    let body = "{\"health\": \"HEALTHY\", \"brand_new_field\": {\"x\": 1}}";
    let f = fake(vec![Reply::Raw(http(200, body))]);
    assert_eq!(f.client().status().unwrap().health, "HEALTHY");
}

#[test]
fn a_truncated_body_is_malformed() {
    let raw = b"HTTP/1.0 200 OK\r\nContent-Length: 500\r\n\r\n{\"health\": \"HEA".to_vec();
    let f = fake(vec![Reply::Raw(raw)]);
    assert!(matches!(f.client().status(), Err(ApiError::Malformed(_))));
}

#[test]
fn chunked_encoding_is_refused_rather_than_misparsed() {
    let raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n".to_vec();
    let f = fake(vec![Reply::Raw(raw)]);
    assert!(matches!(f.client().status(), Err(ApiError::Malformed(_))));
}

#[test]
fn garbage_that_is_not_http_is_malformed() {
    let f = fake(vec![Reply::Raw(b"not http at all\r\n\r\n".to_vec())]);
    assert!(matches!(f.client().status(), Err(ApiError::Malformed(_))));
}

#[test]
fn an_oversized_response_is_refused() {
    let big = "x".repeat(5 * 1024 * 1024);
    let f = fake(vec![Reply::Raw(http(200, &big))]);
    assert!(matches!(f.client().status(), Err(ApiError::Malformed(m)) if m.contains("large")));
}

#[test]
fn only_loopback_http_endpoints_are_accepted() {
    for url in [
        "http://10.0.0.5:8080", "http://example.com:8080", "https://127.0.0.1:8080",
        "ftp://127.0.0.1:8080", "127.0.0.1:8080", "http://127.0.0.1", "http://0.0.0.0:8080",
        "http://127.0.0.1:8080/api", "http://user@127.0.0.1:8080", "",
    ] {
        assert!(matches!(Client::new(url), Err(ApiError::Invalid(_))), "{}", url);
    }
    for url in ["http://127.0.0.1:8080", "http://localhost:9000", "http://[::1]:8080"] {
        assert!(Client::new(url).is_ok(), "{}", url);
    }
}

#[test]
fn an_incident_id_cannot_inject_a_path_and_no_request_is_made() {
    let f = fake(vec![Reply::Raw(http(200, PENDING))]);
    let c = f.client();
    for bad in ["../audit", "inc_001/remediation/approve", "a b", "", "inc_001?x=1", "inc\r\nX: y"] {
        assert!(matches!(c.incident(bad), Err(ApiError::Invalid(_))), "{:?}", bad);
        assert!(matches!(c.approve(bad), Err(ApiError::Invalid(_))), "{:?}", bad);
        assert!(matches!(c.reject(bad), Err(ApiError::Invalid(_))), "{:?}", bad);
    }
    assert!(f.requests().is_empty());
}

#[test]
fn the_audit_limit_is_bounded() {
    let f = fake(vec![Reply::Raw(http(200, AUDIT))]);
    let c = f.client();
    assert!(matches!(c.audit(0), Err(ApiError::Invalid(_))));
    assert!(matches!(c.audit(100_000), Err(ApiError::Invalid(_))));
    assert!(c.audit(1000).is_ok());
}

// --- the read-only system endpoints and the one confirmed stop ---------------------------------------

#[test]
fn version_config_and_diagnostics_are_plain_gets_and_parse() {
    let f = fake(vec![
        Reply::Raw(http(200, r#"{"version": "0.1.0", "git_revision": null, "python": "3.13.5", "platform": "Linux x86_64"}"#)),
        Reply::Raw(http(200, r#"{"source": "/x/aiops.toml", "profile": "docker-real-gpu", "provider": "docker",
                               "sections": {"workload": {"name": "vllm"}}}"#)),
        Reply::Raw(http(200, r#"{"ran_at": "t", "duration_seconds": 1.5, "results": [
                               {"check": "python", "status": "PASS", "detail": "ok", "blocking": true},
                               {"check": "kubectl", "status": "NOT_APPLICABLE", "detail": "n/a", "blocking": false}]}"#)),
    ]);
    let c = f.client();
    let v = c.version().unwrap();
    assert_eq!((v.version.as_str(), v.git_revision), ("0.1.0", None));
    let cfg = c.config().unwrap();
    assert_eq!(cfg.profile, "docker-real-gpu");
    assert_eq!(cfg.sections["workload"]["name"], "vllm");
    let d = c.diagnostics().unwrap();
    assert_eq!(d.results.len(), 2);
    assert_eq!(d.results[1].status, "NOT_APPLICABLE");   // the server's word, untouched
    assert_eq!(
        f.requests(),
        vec!["GET /api/v1/version HTTP/1.1", "GET /api/v1/config HTTP/1.1", "GET /api/v1/diagnostics HTTP/1.1"]
    );
}

#[test]
fn stop_is_one_post_to_the_control_endpoint_with_the_confirmation_header() {
    let f = fake(vec![Reply::Raw(http(202, r#"{"stopping": true, "pid": 4242}"#))]);
    assert!(f.client().stop_control_plane().unwrap().stopping);
    assert_eq!(f.requests(), vec!["POST /api/v1/control/stop HTTP/1.1"]);
    assert!(f.heads()[0].to_lowercase().contains("x-aiops-confirm: stop-control-plane"));
}

#[test]
fn a_stop_that_times_out_is_sent_once_and_never_retried() {
    let f = fake(vec![Reply::After(Duration::from_millis(1500), http(202, r#"{"stopping": true}"#))]);
    assert_eq!(f.client().stop_control_plane().unwrap_err(), ApiError::Timeout);
    thread::sleep(Duration::from_millis(1800));
    assert_eq!(f.requests().len(), 1, "no automatic retry of a mutation");
}

#[test]
fn a_refused_stop_surfaces_the_servers_error_contract() {
    let f = fake(vec![Reply::Raw(http(
        400,
        r#"{"error": {"code": "CONFIRMATION_REQUIRED", "message": "needs confirmation", "request_id": "r"}}"#,
    ))]);
    match f.client().stop_control_plane() {
        Err(ApiError::Server { status: 400, code, .. }) => assert_eq!(code, "CONFIRMATION_REQUIRED"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn headers_with_control_characters_are_refused_before_sending() {
    use aiops_tui::http::{request_with, Endpoint};
    let f = fake(vec![]);
    let ep = Endpoint::parse(&format!("http://127.0.0.1:{}", f.port)).unwrap();
    let r = request_with(&ep, "GET", "/x", &[("X-A", "b\r\nX-Injected: 1")], Duration::from_millis(200), Duration::from_millis(200));
    assert!(r.is_err());
    assert!(f.requests().is_empty());
}

// --- practice (SIMULATION): a separate namespace with the same shapes -------------------------------------

#[test]
fn the_practice_client_addresses_the_practice_namespace_and_the_real_one_does_not() {
    let f = fake(vec![
        Reply::Raw(http(200, STATUS)),
        Reply::Raw(http(200, "{\"incidents\": []}")),
        Reply::Raw(http(200, PENDING)),
        Reply::Raw(http(200, AUDIT)),
        Reply::Raw(http(200, RESOLVED)),
        Reply::Raw(http(200, PENDING)),
        Reply::Raw(http(200, STATUS)),
    ]);
    let real = f.client();
    let p = real.practice();
    p.status().unwrap();
    p.incidents().unwrap();
    p.incident("inc_001").unwrap();
    p.audit(50).unwrap();
    p.approve("inc_001").unwrap();
    p.reject("inc_001").unwrap();
    real.status().unwrap();                           // the original client is unchanged
    assert_eq!(
        f.requests(),
        vec![
            "GET /api/v1/practice/status HTTP/1.1",
            "GET /api/v1/practice/incidents HTTP/1.1",
            "GET /api/v1/practice/incidents/inc_001 HTTP/1.1",
            "GET /api/v1/practice/audit?limit=50 HTTP/1.1",
            "POST /api/v1/practice/incidents/inc_001/remediation/approve HTTP/1.1",
            "POST /api/v1/practice/incidents/inc_001/remediation/reject HTTP/1.1",
            "GET /api/v1/status HTTP/1.1",
        ]
    );
}

#[test]
fn practice_control_calls_are_single_posts_to_fixed_paths() {
    let ok = |s: &str| Reply::Raw(http(200, s));
    let f = fake(vec![
        ok(r#"{"active": true, "mode": "SIMULATION", "stage": "healthy"}"#),
        ok(r#"{"active": true, "mode": "SIMULATION", "stage": "detecting"}"#),
        ok(r#"{"active": false, "mode": "SIMULATION", "stage": null}"#),
    ]);
    let c = f.client();
    c.practice_start().unwrap();
    c.practice_fault().unwrap();
    c.practice_stop().unwrap();
    assert_eq!(
        f.requests(),
        vec![
            "POST /api/v1/practice/start HTTP/1.1",
            "POST /api/v1/practice/fault HTTP/1.1",
            "POST /api/v1/practice/stop HTTP/1.1",
        ]
    );
}

#[test]
fn a_missing_practice_session_is_a_server_error_with_its_code() {
    let f = fake(vec![Reply::Raw(http(
        409,
        r#"{"error": {"code": "NO_PRACTICE", "message": "no practice session is running", "request_id": "r"}}"#,
    ))]);
    match f.client().practice().status() {
        Err(ApiError::Server { status: 409, code, .. }) => assert_eq!(code, "NO_PRACTICE"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn the_practice_status_shape_parses_with_its_mode_and_stage() {
    let body = STATUS.replacen("{", "{\"mode\": \"SIMULATION\", \"practice\": {\"stage\": \"awaiting_approval\"}, ", 1);
    let f = fake(vec![Reply::Raw(http(200, &body))]);
    let s = f.client().practice().status().unwrap();
    assert_eq!(s.mode.as_deref(), Some("SIMULATION"));
    assert_eq!(s.practice.unwrap().stage, "awaiting_approval");
}

// --- the guarded REAL fault: explicit confirmation, a validated body, never anything else ------------------

#[test]
fn a_fault_request_is_one_post_with_the_confirmation_header_and_a_json_body() {
    let f = fake(vec![Reply::Raw(http(202, r#"{"injected": true, "active": {"status": "ACTIVE"}}"#))]);
    f.client().fault_pause("vllm", 120).unwrap();
    assert_eq!(f.requests(), vec!["POST /api/v1/faults HTTP/1.1"]);
    let head = f.heads()[0].to_lowercase();
    assert!(head.contains("x-aiops-confirm: inject-fault"));
    assert!(head.contains("content-type: application/json"));
    assert!(head.contains("content-length: 63") || head.contains("content-length:"), "{head}");
}

#[test]
fn the_fault_body_carries_only_the_type_the_target_and_the_duration() {
    use std::io::Read;
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = thread::spawn(move || {
        let (mut conn, _) = listener.accept().unwrap();
        let mut got = Vec::new();
        let mut buf = [0u8; 1024];
        loop {
            let n = conn.read(&mut buf).unwrap();
            got.extend_from_slice(&buf[..n]);
            let text = String::from_utf8_lossy(&got).to_string();
            if let Some(split) = text.find("\r\n\r\n") {
                let len: usize = text.to_lowercase().split("content-length: ").nth(1).unwrap()
                    .split("\r\n").next().unwrap().trim().parse().unwrap();
                if got.len() >= split + 4 + len {
                    break;
                }
            }
        }
        let _ = conn.write_all(&http(202, r#"{"injected": true}"#));
        String::from_utf8_lossy(&got).to_string()
    });
    Client::new(&format!("http://127.0.0.1:{port}")).unwrap().fault_pause("vllm", 120).unwrap();
    let request = server.join().unwrap();
    let body = request.split("\r\n\r\n").nth(1).unwrap();
    let v: serde_json::Value = serde_json::from_str(body).unwrap();
    assert_eq!(v, serde_json::json!({"type": "pause_workload", "target": "vllm", "duration_seconds": 120}));
}

#[test]
fn resuming_is_a_plain_post_and_a_refused_fault_surfaces_the_servers_error() {
    let f = fake(vec![
        Reply::Raw(http(200, r#"{"available": true, "active": null}"#)),
        Reply::Raw(http(
            409,
            r#"{"error": {"code": "FAULT_ACTIVE", "message": "a fault is already active", "request_id": "r"}}"#,
        )),
    ]);
    let c = f.client();
    c.fault_resume().unwrap();
    match c.fault_pause("vllm", 60) {
        Err(ApiError::Server { status: 409, code, message }) => {
            assert_eq!(code, "FAULT_ACTIVE");
            assert!(message.contains("already active"));
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(f.requests(), vec!["POST /api/v1/faults/cancel HTTP/1.1", "POST /api/v1/faults HTTP/1.1"]);
}

#[test]
fn a_timed_out_fault_request_is_never_resent() {
    let f = fake(vec![Reply::After(Duration::from_millis(1500), http(202, r#"{"injected": true}"#))]);
    assert_eq!(f.client().fault_pause("vllm", 60).unwrap_err(), ApiError::Timeout);
    thread::sleep(Duration::from_millis(1800));
    assert_eq!(f.requests().len(), 1, "no automatic retry of a mutation");
}
