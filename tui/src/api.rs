//! The control-plane API as the TUI uses it. Every action goes through the SAME endpoints any
//! other client uses, so policy and approval are enforced by the server, never here.
use std::fmt;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::Deserialize;

use crate::http::{request_json, Endpoint, HttpError};
use crate::model::{
    Audit, ConfigInfo, Diagnostics, Incident, IncidentList, Status, StopAck, VersionInfo,
};

pub const AUDIT_MAX_LIMIT: u32 = 1000;

#[derive(Debug, Clone, PartialEq)]
pub enum ApiError {
    /// Nothing is listening, or the connection broke: the control plane is offline.
    Offline(String),
    Timeout,
    /// The server answered with its error contract (or a bare HTTP error).
    Server { status: u16, code: String, message: String },
    /// A response the TUI cannot trust or understand.
    Malformed(String),
    /// A request the TUI refused to send.
    Invalid(String),
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ApiError::Offline(m) => write!(f, "control plane offline ({m})"),
            ApiError::Timeout => write!(f, "control plane did not answer in time"),
            ApiError::Server { status, code, message } => write!(f, "{code} (HTTP {status}): {message}"),
            ApiError::Malformed(m) => write!(f, "unexpected response: {m}"),
            ApiError::Invalid(m) => write!(f, "refused request: {m}"),
        }
    }
}

impl From<HttpError> for ApiError {
    fn from(e: HttpError) -> Self {
        match e {
            HttpError::BadUrl(m) => ApiError::Invalid(m),
            HttpError::Refused(m) | HttpError::Io(m) => ApiError::Offline(m),
            HttpError::Timeout => ApiError::Timeout,
            HttpError::Protocol(m) => ApiError::Malformed(m),
        }
    }
}

#[derive(Deserialize)]
struct ErrorBody {
    error: ErrorDetail,
}

#[derive(Deserialize)]
struct ErrorDetail {
    #[serde(default)]
    code: String,
    #[serde(default)]
    message: String,
}

#[derive(Debug, Clone)]
pub struct Client {
    endpoint: Endpoint,
    connect: Duration,
    total: Duration,
    /// `/api/v1` for the real control plane, `/api/v1/practice` for a practice session.
    prefix: &'static str,
}

pub const REAL_PREFIX: &str = "/api/v1";
pub const PRACTICE_PREFIX: &str = "/api/v1/practice";

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

impl Client {
    pub fn new(base: &str) -> Result<Client, ApiError> {
        Ok(Client {
            endpoint: Endpoint::parse(base)?,
            connect: Duration::from_millis(1000),
            total: Duration::from_millis(2000),
            prefix: REAL_PREFIX,
        })
    }

    /// The same client, addressed to the practice (SIMULATION) namespace. The shapes are identical.
    pub fn practice(&self) -> Client {
        Client { prefix: PRACTICE_PREFIX, ..self.clone() }
    }

    pub fn with_timeouts(mut self, connect: Duration, total: Duration) -> Client {
        self.connect = connect;
        self.total = total;
        self
    }

    fn call<T: DeserializeOwned>(&self, method: &str, path: &str) -> Result<T, ApiError> {
        self.call_with(method, path, &[])
    }

    fn call_with<T: DeserializeOwned>(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
    ) -> Result<T, ApiError> {
        self.call_body(method, path, headers, "")
    }

    fn call_body<T: DeserializeOwned>(
        &self,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: &str,
    ) -> Result<T, ApiError> {
        let response = request_json(&self.endpoint, method, path, headers, body, self.connect, self.total)?;
        if !(200..300).contains(&response.status) {
            return Err(match serde_json::from_str::<ErrorBody>(&response.body) {
                Ok(e) => ApiError::Server {
                    status: response.status,
                    code: e.error.code,
                    message: e.error.message,
                },
                Err(_) => ApiError::Server {
                    status: response.status,
                    code: format!("HTTP_{}", response.status),
                    message: response.body.chars().take(200).collect(),
                },
            });
        }
        serde_json::from_str(&response.body).map_err(|e| ApiError::Malformed(e.to_string()))
    }

    fn checked(id: &str) -> Result<&str, ApiError> {
        if valid_id(id) {
            Ok(id)
        } else {
            Err(ApiError::Invalid(format!("not a valid incident id: {id:?}")))
        }
    }

    pub fn status(&self) -> Result<Status, ApiError> {
        self.call("GET", &format!("{}/status", self.prefix))
    }

    pub fn incidents(&self) -> Result<Vec<Incident>, ApiError> {
        Ok(self.call::<IncidentList>("GET", &format!("{}/incidents", self.prefix))?.incidents)
    }

    pub fn incident(&self, id: &str) -> Result<Incident, ApiError> {
        self.call("GET", &format!("{}/incidents/{}", self.prefix, Self::checked(id)?))
    }

    pub fn audit(&self, limit: u32) -> Result<Audit, ApiError> {
        if limit == 0 || limit > AUDIT_MAX_LIMIT {
            return Err(ApiError::Invalid(format!("audit limit must be 1..={AUDIT_MAX_LIMIT}")));
        }
        self.call("GET", &format!("{}/audit?limit={limit}", self.prefix))
    }

    /// Asks the SERVER to approve. The server's policy decides; the TUI constructs no command.
    pub fn approve(&self, id: &str) -> Result<Incident, ApiError> {
        self.call("POST", &format!("{}/incidents/{}/remediation/approve", self.prefix, Self::checked(id)?))
    }

    pub fn reject(&self, id: &str) -> Result<Incident, ApiError> {
        self.call("POST", &format!("{}/incidents/{}/remediation/reject", self.prefix, Self::checked(id)?))
    }

    // ---- read-only system endpoints (the server runs the doctor; the TUI only asks) ------------

    pub fn version(&self) -> Result<VersionInfo, ApiError> {
        self.call("GET", "/api/v1/version")
    }

    pub fn config(&self) -> Result<ConfigInfo, ApiError> {
        self.call("GET", "/api/v1/config")
    }

    pub fn diagnostics(&self) -> Result<Diagnostics, ApiError> {
        self.call("GET", "/api/v1/diagnostics")
    }

    /// Asks the SERVER to shut itself down gracefully, exactly as `aiops stop` would. It is the
    /// control plane's lifecycle only: it names no workload and has no path to remediation. The
    /// header is the server's explicit-confirmation marker (a web page cannot send it).
    pub fn stop_control_plane(&self) -> Result<StopAck, ApiError> {
        self.call_with("POST", "/api/v1/control/stop", &[("X-Aiops-Confirm", "stop-control-plane")])
    }

    // ---- practice (SIMULATION): control calls. The simulation lives in the control plane. -------

    fn practice_call(&self, action: &str) -> Result<(), ApiError> {
        self.call::<serde_json::Value>("POST", &format!("{PRACTICE_PREFIX}/{action}")).map(|_| ())
    }

    pub fn practice_start(&self) -> Result<(), ApiError> {
        self.practice_call("start")
    }

    /// Asks the SIMULATION to break its model. It cannot name a real workload or touch anything real.
    pub fn practice_fault(&self) -> Result<(), ApiError> {
        self.practice_call("fault")
    }

    pub fn practice_stop(&self) -> Result<(), ApiError> {
        self.practice_call("stop")
    }

    // ---- the guarded REAL fault (testing only). The server enforces every safety rule; this only
    // ---- asks, with the explicit confirmation header, and never names anything but the workload.

    /// Asks the server to pause the managed workload for `seconds`. The server validates the
    /// target, the duration and the workload's identity, and always ends the fault by itself.
    pub fn fault_pause(&self, target: &str, seconds: u32) -> Result<(), ApiError> {
        let body = serde_json::json!({
            "type": "pause_workload", "target": target, "duration_seconds": seconds,
        })
        .to_string();
        self.call_body::<serde_json::Value>(
            "POST",
            "/api/v1/faults",
            &[("X-Aiops-Confirm", "inject-fault")],
            &body,
        )
        .map(|_| ())
    }

    /// Resume the workload now (resuming is always safe, so it needs no confirmation).
    pub fn fault_resume(&self) -> Result<(), ApiError> {
        self.call::<serde_json::Value>("POST", "/api/v1/faults/cancel").map(|_| ())
    }
}
