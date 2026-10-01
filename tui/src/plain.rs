//! Plain-language phrases for the default views. Wording lives here, in one place, and is tested.
//!
//! The rule is the project's own: never a claim stronger than the data supports. Every phrase is
//! derived from a field the server sent; anything this module does not know falls back to the raw
//! name (humanised), never to an invented sentence.
use serde_json::Value;

use crate::model::{AuditEvent, Evidence, Incident};
use crate::timeparse::clock_hms;

/// `SOME_RAW_NAME` -> `Some raw name`.
pub fn humanise(raw: &str) -> String {
    let s = raw.replace('_', " ").to_lowercase();
    let mut c = s.chars();
    match c.next() {
        Some(first) => first.to_uppercase().collect::<String>() + c.as_str(),
        None => String::new(),
    }
}

/// The incident's headline. Describes what the detector observed, no more.
pub fn problem_title(category: &str) -> String {
    match category {
        "INFERENCE_UNRESPONSIVE" => "Your model stopped answering".into(),
        // the detector fires on memory above a configured limit, which is not "out of memory"
        "GPU_MEMORY_PRESSURE" => "GPU memory is above its limit".into(),
        other => humanise(other),
    }
}

/// What happened, in a sentence, or `None` for a category this module has no honest sentence for.
pub fn problem_summary(category: &str) -> Option<&'static str> {
    match category {
        "INFERENCE_UNRESPONSIVE" => Some("The model stopped responding to test requests."),
        "GPU_MEMORY_PRESSURE" => Some("GPU memory use went above the configured limit."),
        _ => None,
    }
}

/// The incident's state for a person.
pub fn state_phrase(status: &str) -> String {
    match status {
        "DETECTED" | "TRIAGING" | "DIAGNOSED" => "Investigating",
        "PROPOSED" | "POLICY_CHECK" => "Needs your OK",
        "APPROVED" => "Approved",
        "EXECUTING" => "Running the fix",
        "VERIFYING" => "Checking recovery",
        "RESOLVED" => "Resolved",
        "UNRESOLVED" => "Not resolved",
        "EXECUTION_FAILED" => "The fix could not run",
        "REJECTED" => "Declined",
        "CLEARED" => "Cleared",
        "INSUFFICIENT_EVIDENCE" => "Not enough evidence",
        other => return humanise(other),
    }
    .to_string()
}

/// The proposed action as an instruction.
pub fn action_phrase(action: &str) -> String {
    match action {
        "restart_workload" => "Restart the model server".into(),
        other => humanise(other),
    }
}

/// One evidence row as a sentence, or `None` if this module cannot state it plainly.
pub fn evidence_sentence(e: &Evidence) -> Option<String> {
    match (e.metric.as_str(), &e.value) {
        ("inference_probe", Value::Object(o)) => {
            if o.get("ok").and_then(Value::as_bool) == Some(false) {
                let err = o.get("error").and_then(Value::as_str).unwrap_or("");
                Some(if err.to_lowercase().contains("timeout") {
                    "A test request to the model did not complete in time.".to_string()
                } else if err.is_empty() {
                    "A test request to the model failed.".to_string()
                } else {
                    format!("A test request to the model failed ({err}).")
                })
            } else if o.get("ok").and_then(Value::as_bool) == Some(true) {
                let ms = o.get("latency_ms").and_then(Value::as_f64).unwrap_or(0.0);
                Some(format!("A test request to the model succeeded ({ms:.0} ms)."))
            } else {
                None
            }
        }
        ("vllm_metrics", Value::Object(o)) => match o.get("available").and_then(Value::as_bool) {
            Some(true) => Some("The model's metrics are available.".into()),
            Some(false) => Some("The model's metrics are not available.".into()),
            None => None,
        },
        ("gpu_memory_used_bytes", Value::Number(n)) => {
            Some(format!("GPU memory in use: {:.1} GiB.", n.as_f64().unwrap_or(0.0) / 1_073_741_824.0))
        }
        ("vllm_allocation_failure", Value::Number(n)) => {
            let n = n.as_f64().unwrap_or(0.0);
            Some(format!("The model server reported {n:.0} memory allocation failures."))
        }
        _ => None,
    }
}

/// What the recovery check will establish. The count comes from the server, or it is not stated.
pub fn recovery_check(required: Option<u64>) -> String {
    match required {
        Some(n) => format!("After the restart the model must answer {n} test requests in a row."),
        None => "After the restart the server checks that the model answers again.".into(),
    }
}

/// One recorded verification check as a sentence.
pub fn check_phrase(key: &str, required: Option<u64>) -> String {
    match key {
        "workload_restarted" => "The model server was restarted".into(),
        "workload_available" => "The workload is available".into(),
        "gpu_observable" => "The GPU can be read".into(),
        "gpu_memory_below_threshold" => "GPU memory is back under its limit".into(),
        "vllm_metrics_readable" => "The model's metrics can be read".into(),
        "inference_probe_stable" => match required {
            Some(n) => format!("The model answered {n} test requests in a row"),
            None => "The model answers test requests again".into(),
        },
        // the Kubernetes path's checks
        "workload_ready" => "The workload is ready".into(),
        "gpu_memory_ok" => "GPU memory is within its limit".into(),
        "error_rate_ok" => "The error rate is within its limit".into(),
        "verification_error" => "The recovery check could run".into(),
        other => humanise(other),
    }
}

/// The incident's raw timeline condensed to what a person follows, with the time of each step.
pub fn timeline_events(i: &Incident) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for t in &i.timeline {
        let phrase = match t.state.as_str() {
            "DETECTED" | "TRIAGING" | "DIAGNOSED" => "Problem detected and investigated".to_string(),
            "PROPOSED" | "POLICY_CHECK" => "Fix proposed".to_string(),
            "APPROVED" => "You approved the fix".to_string(),
            "REJECTED" => "You declined the fix".to_string(),
            "EXECUTING" => "Fix started".to_string(),
            "VERIFYING" => "Checking recovery".to_string(),
            other => state_phrase(other),
        };
        if out.last().map_or(true, |(_, p)| *p != phrase) {
            out.push((clock_hms(&t.at), phrase));
        }
    }
    out
}

/// One audit event as a sentence. Unknown events keep their recorded name.
pub fn event_phrase(e: &AuditEvent) -> String {
    let id = e.data.get("incident_id").and_then(Value::as_str);
    let with_id = |text: &str| match id {
        Some(id) => format!("{text} ({id})"),
        None => text.to_string(),
    };
    let simulated = e.data.get("mode").and_then(Value::as_str) == Some("SIMULATION");
    let text = match e.event.as_str() {
        "incident_created" => with_id("Problem detected"),
        "rca_generated" => {
            let insufficient = e.data.pointer("/rca/insufficient_evidence").and_then(Value::as_bool) == Some(true);
            with_id(if insufficient { "Not enough evidence to name a cause" } else { "Cause assessed" })
        }
        "remediation_proposed" => {
            let action = e.data.get("action").and_then(Value::as_str).unwrap_or("");
            format!("Fix proposed: {}", action_phrase(action))
        }
        "remediation_rejected" => "A proposed fix was refused as invalid".into(),
        "policy_evaluated" => match e.data.get("decision").and_then(Value::as_str) {
            Some("APPROVAL_REQUIRED") => "Needs your OK".into(),
            Some(other) => format!("Policy decision: {}", humanise(other)),
            None => "Policy checked".into(),
        },
        "approval_granted" => with_id("You approved the fix"),
        "approval_denied" => with_id("You declined the fix"),
        "remediation_started" => "Fix started".into(),
        "remediation_finished" => "Fix finished".into(),
        "remediation_failed" => "The fix failed".into(),
        "precondition_failed" => "The fix could not start".into(),
        "verification_finished" => {
            let ok = e.data.get("checks").and_then(Value::as_object).map_or(false, |c| {
                !c.is_empty() && c.values().all(|v| v.as_bool() == Some(true))
            });
            if ok { "Recovery verified".into() } else { "Recovery was not verified".into() }
        }
        "incident_cleared" => with_id("Problem cleared on its own"),
        "condition_suppressed" => "Another condition was noted while one is being handled".into(),
        "interrupted_by_restart" => with_id("The control plane restarted during a fix; it was closed as failed"),
        "fault_injected" if simulated => "A simulated fault was injected".into(),
        "fault_injected" => "A test fault started (the workload was paused)".into(),
        "fault_ended" => {
            let status = e.data.get("status").and_then(Value::as_str).unwrap_or("");
            format!("The test fault ended ({})", humanise(status).to_lowercase())
        }
        other => other.to_string(),
    };
    if simulated && !text.contains("simulated") { format!("{text} (simulated)") } else { text }
}
