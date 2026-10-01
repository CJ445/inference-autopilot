//! The control loop as seven stages, derived from one incident's status and nothing else:
//! Observe, Detect, Diagnose, Propose, Approve, Recover, Verify. Drawn the same way on Home, the
//! incident page and the Lab. A stage is never marked done on a guess: it is `NotRun` until the
//! server's own status says it happened.
use crate::model::Incident;

pub const NAMES: [&str; 7] = ["Observe", "Detect", "Diagnose", "Propose", "Approve", "Recover", "Verify"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    /// Happened: `✓`.
    Done,
    /// The loop is here now (for Approve: waiting for the operator): `→`.
    Active,
    /// Went wrong here: `✗`.
    Failed,
    /// Has not happened yet: `○`.
    NotRun,
    /// Will not happen on this path (declined, cleared, not enough evidence): `–`.
    NotApplicable,
}

impl Stage {
    pub fn glyph(self) -> &'static str {
        match self {
            Stage::Done => "✓",
            Stage::Active => "→",
            Stage::Failed => "✗",
            Stage::NotRun => "○",
            Stage::NotApplicable => "–",
        }
    }
}

/// The stages for the loop as it stands. `None` is "no incident": the system is observing (when it
/// is) and waiting for something to detect.
pub fn stages(incident: Option<&Incident>, observing: bool) -> [Stage; 7] {
    use Stage::*;
    let Some(i) = incident else {
        return [if observing { Done } else { NotApplicable }, NotRun, NotRun, NotRun, NotRun, NotRun, NotRun];
    };
    let proposed = i.proposal.is_some();
    match i.status.as_str() {
        "DETECTED" => [Done, Active, NotRun, NotRun, NotRun, NotRun, NotRun],
        "TRIAGING" => [Done, Done, Active, NotRun, NotRun, NotRun, NotRun],
        "DIAGNOSED" => [Done, Done, Done, Active, NotRun, NotRun, NotRun],
        "INSUFFICIENT_EVIDENCE" => [Done, Done, Failed, NotApplicable, NotApplicable, NotApplicable, NotApplicable],
        "PROPOSED" | "POLICY_CHECK" => [Done, Done, Done, Done, Active, NotRun, NotRun],
        "APPROVED" | "EXECUTING" => [Done, Done, Done, Done, Done, Active, NotRun],
        "VERIFYING" => [Done, Done, Done, Done, Done, Done, Active],
        "RESOLVED" => [Done; 7],
        "UNRESOLVED" => [Done, Done, Done, Done, Done, Done, Failed],
        "EXECUTION_FAILED" => [Done, Done, Done, Done, Done, Failed, NotRun],
        "REJECTED" => [Done, Done, Done, Done, Failed, NotApplicable, NotApplicable],
        // the problem went away before it was acted on: nothing was approved, run or verified
        "CLEARED" if proposed => [Done, Done, Done, Done, NotApplicable, NotApplicable, NotApplicable],
        "CLEARED" => [Done, Done, Done, NotApplicable, NotApplicable, NotApplicable, NotApplicable],
        _ => [Done, NotRun, NotRun, NotRun, NotRun, NotRun, NotRun],
    }
}

/// The first stage that is not finished and not skipped: what the loop is on (or stuck at).
pub fn current(stages: &[Stage; 7]) -> Option<usize> {
    stages.iter().position(|s| matches!(s, Stage::Active | Stage::Failed))
}
