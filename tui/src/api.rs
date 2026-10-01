//! The control-plane API as the TUI uses it. Every action goes through the SAME endpoints any
//! other client uses, so policy and approval are enforced by the server, never here.
use std::fmt;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde::Deserialize;

use crate::http::{request, Endpoint, HttpError};
use crate::model::{Audit, Incident, IncidentList, Status};

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
}

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
        })
    }

    pub fn with_timeouts(mut self, connect: Duration, total: Duration) -> Client {
        self.connect = connect;
        self.total = total;
        self
    }

    fn call<T: DeserializeOwned>(&self, method: &str, path: &str) -> Result<T, ApiError> {
        let response = request(&self.endpoint, method, path, self.connect, self.total)?;
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
        self.call("GET", "/api/v1/status")
    }

    pub fn incidents(&self) -> Result<Vec<Incident>, ApiError> {
        Ok(self.call::<IncidentList>("GET", "/api/v1/incidents")?.incidents)
    }

    pub fn incident(&self, id: &str) -> Result<Incident, ApiError> {
        self.call("GET", &format!("/api/v1/incidents/{}", Self::checked(id)?))
    }

    pub fn audit(&self, limit: u32) -> Result<Audit, ApiError> {
        if limit == 0 || limit > AUDIT_MAX_LIMIT {
            return Err(ApiError::Invalid(format!("audit limit must be 1..={AUDIT_MAX_LIMIT}")));
        }
        self.call("GET", &format!("/api/v1/audit?limit={limit}"))
    }

    /// Asks the SERVER to approve. The server's policy decides; the TUI constructs no command.
    pub fn approve(&self, id: &str) -> Result<Incident, ApiError> {
        self.call("POST", &format!("/api/v1/incidents/{}/remediation/approve", Self::checked(id)?))
    }

    pub fn reject(&self, id: &str) -> Result<Incident, ApiError> {
        self.call("POST", &format!("/api/v1/incidents/{}/remediation/reject", Self::checked(id)?))
    }
}
