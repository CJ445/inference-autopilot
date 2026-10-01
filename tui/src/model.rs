//! The control plane's JSON, as the TUI reads it. Required fields are the few the UI cannot work
//! without; everything else is optional so a newer or older server never crashes the operator.
use serde::Deserialize;
use serde_json::{Map, Value};
use std::collections::BTreeMap;

pub type Obj = Map<String, Value>;

#[derive(Debug, Clone, Deserialize)]
pub struct Status {
    pub health: String,
    #[serde(default)]
    pub last_observed_at: Option<String>,
    #[serde(default)]
    pub last_observation: Option<Obj>,
    #[serde(default)]
    pub incidents: IncidentIds,
    #[serde(default)]
    pub info: Info,
    #[serde(default)]
    pub audit: Option<AuditStatus>,
    #[serde(default)]
    pub watchdog: Option<Watchdog>,
    #[serde(default)]
    pub extra_error: Option<String>,
    /// Whether the managed workload exists (only reported where the control plane can tell).
    #[serde(default)]
    pub workload: Option<WorkloadState>,
    /// `SIMULATION` for a practice session; `REAL` (or absent on an older server) otherwise.
    #[serde(default)]
    pub mode: Option<String>,
    /// Present only in a practice session: where the simulated loop is.
    #[serde(default)]
    pub practice: Option<PracticeInfo>,
    /// Present only where the control plane offers the guarded real fault.
    #[serde(default)]
    pub faults: Option<FaultsInfo>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FaultsInfo {
    #[serde(default)]
    pub available: bool,
    #[serde(default)]
    pub active: Option<ActiveFault>,
}

/// A real fault in progress: the workload is paused until `expires_at` (epoch seconds) at the latest.
#[derive(Debug, Clone, Deserialize)]
pub struct ActiveFault {
    pub fault_id: String,
    #[serde(default)]
    pub status: String,
    pub expires_at: f64,
    #[serde(default)]
    pub workload: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PracticeInfo {
    pub stage: String,
}

/// `absent` | `stopped` | `running` | `unknown`, exactly as the server reports it.
#[derive(Debug, Clone, Deserialize)]
pub struct WorkloadState {
    #[serde(default)]
    pub name: Option<String>,
    pub state: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct IncidentIds {
    #[serde(default)]
    pub active: Vec<String>,
    #[serde(default)]
    pub pending: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Info {
    #[serde(default)]
    pub started_at: Option<String>,
    #[serde(default)]
    pub profile: Option<String>,
    #[serde(default)]
    pub provider: Option<String>,
    #[serde(default)]
    pub workload: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub pid: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AuditStatus {
    pub valid: bool,
    #[serde(default)]
    pub events: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Watchdog {
    pub state: String,
    #[serde(default)]
    pub pid: Option<u64>,
    #[serde(default)]
    pub protected_pid: Option<u64>,
    #[serde(default)]
    pub limits: Option<Obj>,
    #[serde(default)]
    pub last_sample: Option<Obj>,
    #[serde(default)]
    pub last_check_age_seconds: Option<f64>,
    #[serde(default)]
    pub consecutive_sensor_failures: Option<u64>,
    #[serde(default)]
    pub reason: Option<Vec<String>>,
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub note: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Incident {
    pub incident_id: String,
    #[serde(default)]
    pub service: Option<String>,
    pub category: String,
    pub status: String,
    #[serde(default)]
    pub evidence: Vec<Evidence>,
    #[serde(default)]
    pub timeline: Vec<TimelineEntry>,
    #[serde(default)]
    pub proposal: Option<Proposal>,
    #[serde(default)]
    pub rca: Option<Rca>,
    #[serde(default)]
    pub remediation: Option<Remediation>,
    #[serde(default)]
    pub verification: Option<Verification>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Evidence {
    #[serde(default)]
    pub evidence_id: String,
    pub source: String,
    pub metric: String,
    #[serde(default)]
    pub value: Value,
    pub relation: String,
    #[serde(default)]
    pub resource: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TimelineEntry {
    pub state: String,
    pub at: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Proposal {
    pub action: String,
    #[serde(default)]
    pub parameters: Obj,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Rca {
    #[serde(default)]
    pub root_cause: Option<RootCause>,
    #[serde(default)]
    pub evidence_ids: Vec<String>,
    #[serde(default)]
    pub insufficient_evidence: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RootCause {
    pub category: String,
    #[serde(default)]
    pub statement: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Remediation {
    pub state: String,
    #[serde(default)]
    pub error: Option<Value>,
    #[serde(default)]
    pub api_result: Option<Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Verification {
    #[serde(default)]
    pub checks: Option<BTreeMap<String, bool>>,
    /// How many completions in a row recovery must show, when the server knows (else absent).
    #[serde(default)]
    pub required_completions: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Audit {
    pub valid: bool,
    #[serde(default)]
    pub total: u64,
    #[serde(default)]
    pub events: Vec<AuditEvent>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AuditEvent {
    pub seq: u64,
    pub event: String,
    #[serde(default)]
    pub data: Value,
    #[serde(default)]
    pub hash: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct IncidentList {
    pub incidents: Vec<Incident>,
}

/// Typed reads from the free-form observation objects (never defaulting a missing value).
pub fn num(o: &Obj, key: &str) -> Option<f64> {
    o.get(key).and_then(Value::as_f64)
}

pub fn flag(o: &Obj, key: &str) -> Option<bool> {
    o.get(key).and_then(Value::as_bool)
}

pub fn text(o: &Obj, key: &str) -> Option<String> {
    o.get(key).and_then(Value::as_str).map(str::to_string)
}

/// `GET /api/v1/version`: what the control plane reports about itself. Nothing here is computed
/// by the TUI.
#[derive(Debug, Clone, Deserialize)]
pub struct VersionInfo {
    pub version: String,
    #[serde(default)]
    pub git_revision: Option<String>,
    #[serde(default)]
    pub python: Option<String>,
    #[serde(default)]
    pub platform: Option<String>,
}

/// `GET /api/v1/config`: the active, validated, non-secret configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct ConfigInfo {
    pub source: String,
    pub profile: String,
    pub provider: String,
    #[serde(default)]
    pub sections: BTreeMap<String, Obj>,
}

/// `GET /api/v1/diagnostics`: the doctor's results, statuses exactly as the server names them.
#[derive(Debug, Clone, Deserialize)]
pub struct Diagnostics {
    pub results: Vec<Check>,
    #[serde(default)]
    pub ran_at: Option<String>,
    #[serde(default)]
    pub duration_seconds: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Check {
    pub check: String,
    pub status: String,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub blocking: bool,
}

/// `POST /api/v1/control/stop` acknowledgement.
#[derive(Debug, Clone, Deserialize)]
pub struct StopAck {
    #[serde(default)]
    pub stopping: bool,
}
