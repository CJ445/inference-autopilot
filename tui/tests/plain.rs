//! Plain-language phrases: each claim must be backed by the data it is built from.
use aiops_tui::model::{AuditEvent, Evidence, Incident};
use aiops_tui::plain::*;
use serde_json::json;

fn evidence(metric: &str, value: serde_json::Value) -> Evidence {
    serde_json::from_value(json!({"evidence_id": "e1", "source": "x", "metric": metric,
                                  "value": value, "relation": "supports"})).unwrap()
}
fn event(name: &str, data: serde_json::Value) -> AuditEvent {
    serde_json::from_value(json!({"seq": 1, "event": name, "data": data, "hash": "h"})).unwrap()
}

#[test]
fn titles_say_what_the_detector_observed_and_no_more() {
    assert_eq!(problem_title("INFERENCE_UNRESPONSIVE"), "Your model stopped answering");
    let gpu = problem_title("GPU_MEMORY_PRESSURE");
    assert_eq!(gpu, "GPU memory is above its limit");
    assert!(!gpu.to_lowercase().contains("running out"), "the detector fires on a threshold, not exhaustion");
    assert_eq!(problem_summary("INFERENCE_UNRESPONSIVE"), Some("The model stopped responding to test requests."));
    assert_eq!(problem_summary("GPU_MEMORY_PRESSURE"), Some("GPU memory use went above the configured limit."));
}

#[test]
fn an_unknown_category_state_or_action_keeps_its_own_name_never_an_invented_sentence() {
    assert_eq!(problem_title("DISK_FULL_AND_BURNING"), "Disk full and burning");
    assert_eq!(problem_summary("DISK_FULL_AND_BURNING"), None);
    assert_eq!(state_phrase("QUANTUM_STATE"), "Quantum state");
    assert_eq!(action_phrase("rotate_keys"), "Rotate keys");
    assert_eq!(humanise(""), "");
}

#[test]
fn every_state_has_a_plain_phrase() {
    for (raw, plain) in [
        ("DETECTED", "Investigating"), ("TRIAGING", "Investigating"), ("DIAGNOSED", "Investigating"),
        ("PROPOSED", "Needs your OK"), ("POLICY_CHECK", "Needs your OK"), ("APPROVED", "Approved"),
        ("EXECUTING", "Running the fix"), ("VERIFYING", "Checking recovery"), ("RESOLVED", "Resolved"),
        ("UNRESOLVED", "Not resolved"), ("EXECUTION_FAILED", "The fix could not run"),
        ("REJECTED", "Declined"), ("CLEARED", "Cleared"), ("INSUFFICIENT_EVIDENCE", "Not enough evidence"),
    ] {
        assert_eq!(state_phrase(raw), plain, "{raw}");
    }
    assert_eq!(action_phrase("restart_workload"), "Restart the model server");
}

#[test]
fn a_probe_is_called_a_timeout_only_when_the_error_says_so() {
    let probe = |ok: bool, error: Option<&str>| {
        evidence_sentence(&evidence("inference_probe", json!({"ok": ok, "latency_ms": 18.4, "error": error})))
    };
    assert_eq!(probe(false, Some("timeout")).unwrap(), "A test request to the model did not complete in time.");
    assert_eq!(probe(false, Some("Request TIMEOUT after 3s")).unwrap(), "A test request to the model did not complete in time.");
    assert_eq!(probe(false, Some("connection refused")).unwrap(), "A test request to the model failed (connection refused).");
    assert_eq!(probe(false, None).unwrap(), "A test request to the model failed.");
    assert_eq!(probe(true, None).unwrap(), "A test request to the model succeeded (18 ms).");
    assert_eq!(evidence_sentence(&evidence("inference_probe", json!({"latency_ms": 3}))), None, "no verdict, no sentence");
}

#[test]
fn metrics_and_memory_evidence_state_only_what_was_measured() {
    assert_eq!(evidence_sentence(&evidence("vllm_metrics", json!({"available": false}))).unwrap(), "The model's metrics are not available.");
    assert_eq!(evidence_sentence(&evidence("vllm_metrics", json!({"available": true}))).unwrap(), "The model's metrics are available.");
    assert_eq!(evidence_sentence(&evidence("vllm_metrics", json!({}))), None);
    assert_eq!(evidence_sentence(&evidence("gpu_memory_used_bytes", json!(3_035_000_000.0))).unwrap(), "GPU memory in use: 2.8 GiB.");
    assert_eq!(evidence_sentence(&evidence("vllm_allocation_failure", json!(5))).unwrap(), "The model server reported 5 memory allocation failures.");
    assert_eq!(evidence_sentence(&evidence("something_new", json!(1))), None);
}

#[test]
fn the_completion_count_comes_from_the_server_or_it_is_not_stated() {
    assert_eq!(recovery_check(Some(3)), "After the restart the model must answer 3 test requests in a row.");
    assert_eq!(recovery_check(Some(5)), "After the restart the model must answer 5 test requests in a row.");
    let unknown = recovery_check(None);
    assert!(!unknown.chars().any(|c| c.is_ascii_digit()), "{unknown}");
    assert_eq!(check_phrase("inference_probe_stable", Some(3)), "The model answered 3 test requests in a row");
    assert_eq!(check_phrase("inference_probe_stable", None), "The model answers test requests again");
}

#[test]
fn verification_checks_have_plain_sentences_and_unknown_ones_keep_their_name() {
    for (key, text) in [
        ("workload_restarted", "The model server was restarted"), ("gpu_observable", "The GPU can be read"),
        ("vllm_metrics_readable", "The model's metrics can be read"),
        ("gpu_memory_below_threshold", "GPU memory is back under its limit"),
        ("workload_available", "The workload is available"),
        ("verification_error", "The recovery check could run"), ("error_rate_ok", "The error rate is within its limit"),
        ("gpu_memory_ok", "GPU memory is within its limit"), ("workload_ready", "The workload is ready"),
        ("some_future_check", "Some future check"),
    ] {
        assert_eq!(check_phrase(key, Some(3)), text, "{key}");
    }
}

#[test]
fn the_timeline_is_condensed_to_the_steps_a_person_follows() {
    let i: Incident = serde_json::from_value(json!({
        "incident_id": "inc_001", "category": "X", "status": "RESOLVED",
        "timeline": [
            {"state": "DETECTED", "at": "2026-10-01T15:02:11+00:00"}, {"state": "TRIAGING", "at": "2026-10-01T15:02:11+00:00"},
            {"state": "DIAGNOSED", "at": "2026-10-01T15:02:12+00:00"}, {"state": "PROPOSED", "at": "2026-10-01T15:02:12+00:00"},
            {"state": "POLICY_CHECK", "at": "2026-10-01T15:02:12+00:00"}, {"state": "APPROVED", "at": "2026-10-01T15:02:18+00:00"},
            {"state": "EXECUTING", "at": "2026-10-01T15:02:18+00:00"}, {"state": "VERIFYING", "at": "2026-10-01T15:02:22+00:00"},
            {"state": "RESOLVED", "at": "2026-10-01T15:02:47+00:00"}]})).unwrap();
    let t: Vec<(String, String)> = timeline_events(&i);
    assert_eq!(t.iter().map(|(_, p)| p.as_str()).collect::<Vec<_>>(), vec![
        "Problem detected and investigated", "Fix proposed", "You approved the fix", "Fix started",
        "Checking recovery", "Resolved"]);
    assert_eq!(t[0].0, "15:02:11");                          // the time of the FIRST raw step
    assert_eq!(t[5].0, "15:02:47");
}

#[test]
fn audit_events_read_as_what_happened_and_unknown_events_keep_their_name() {
    let e = |name: &str, data| event_phrase(&event(name, data));
    assert_eq!(e("incident_created", json!({"incident_id": "inc_001"})), "Problem detected (inc_001)");
    assert_eq!(e("rca_generated", json!({"incident_id": "inc_001", "rca": {"insufficient_evidence": true}})), "Not enough evidence to name a cause (inc_001)");
    assert_eq!(e("rca_generated", json!({"incident_id": "inc_001", "rca": {"insufficient_evidence": false}})), "Cause assessed (inc_001)");
    assert_eq!(e("remediation_proposed", json!({"action": "restart_workload"})), "Fix proposed: Restart the model server");
    assert_eq!(e("policy_evaluated", json!({"decision": "APPROVAL_REQUIRED"})), "Needs your OK");
    assert_eq!(e("approval_granted", json!({"incident_id": "inc_001"})), "You approved the fix (inc_001)");
    assert_eq!(e("approval_refused", json!({"incident_id": "inc_001", "reason": "the model is answering again"})),
               "Nothing was restarted: the model is answering again (inc_001)");
    assert_eq!(e("approval_denied", json!({"incident_id": "inc_001"})), "You declined the fix (inc_001)");
    assert_eq!(e("verification_finished", json!({"checks": {"a": true, "b": true}})), "Recovery verified");
    assert_eq!(e("verification_finished", json!({"checks": {"a": true, "b": false}})), "Recovery was not verified");
    assert_eq!(e("verification_finished", json!({"checks": {}})), "Recovery was not verified", "an empty set proves nothing");
    assert_eq!(e("fault_injected", json!({})), "A test fault started (the workload was paused)");
    assert_eq!(e("fault_ended", json!({"status": "IDENTITY_CHANGED"})), "The test fault ended (identity changed)");
    assert_eq!(e("some_future_event", json!({})), "some_future_event");
}

#[test]
fn simulated_events_are_always_marked_simulated() {
    let sim = |name: &str| event_phrase(&event(name, json!({"mode": "SIMULATION", "incident_id": "inc_001"})));
    assert_eq!(sim("fault_injected"), "A simulated fault was injected");
    assert!(sim("incident_created").ends_with("(simulated)"), "{}", sim("incident_created"));
    assert!(sim("approval_granted").contains("simulated"));
    assert!(sim("brand_new_event").ends_with("(simulated)"));
}
