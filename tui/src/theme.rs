//! The TUI's small semantic palette. Colour only ever means state or focus; widgets never pick
//! their own. The terminal's own background and foreground are used for `BACKGROUND` / `TEXT`
//! so the UI stays terminal-native in any dark theme.
use ratatui::style::Color;

pub const BACKGROUND: Color = Color::Reset;
pub const BACKGROUND_SECONDARY: Color = Color::Rgb(32, 35, 43);
pub const TEXT: Color = Color::Reset;
pub const TEXT_MUTED: Color = Color::Rgb(128, 134, 150);
pub const BORDER: Color = Color::Rgb(62, 66, 80);
pub const BORDER_FOCUSED: Color = Color::Rgb(122, 162, 247);
pub const ACCENT: Color = Color::Rgb(122, 162, 247);

pub const HEALTHY: Color = Color::Rgb(115, 201, 145);
pub const WARNING: Color = Color::Rgb(229, 181, 90);
pub const CRITICAL: Color = Color::Rgb(240, 98, 98);

pub const PENDING: Color = Color::Rgb(229, 181, 90);
pub const INVESTIGATING: Color = Color::Rgb(122, 162, 247);
pub const APPROVED: Color = Color::Rgb(110, 190, 175);
pub const EXECUTING: Color = Color::Rgb(176, 143, 240);
pub const VERIFYING: Color = Color::Rgb(100, 200, 222);
pub const RESOLVED: Color = Color::Rgb(115, 201, 145);
pub const REJECTED: Color = Color::Rgb(128, 134, 150);
pub const CLEARED: Color = Color::Rgb(120, 165, 145);
pub const UNRESOLVED: Color = Color::Rgb(240, 98, 98);
pub const EXECUTION_FAILED: Color = Color::Rgb(240, 98, 98);

/// The label and colour of an incident status, from the server's status string only.
pub fn state(status: &str) -> (&'static str, Color) {
    match status {
        "DETECTED" | "TRIAGING" | "DIAGNOSED" => ("INVESTIGATING", INVESTIGATING),
        "PROPOSED" | "POLICY_CHECK" => ("AWAITING APPROVAL", PENDING),
        "APPROVED" => ("APPROVED", APPROVED),
        "EXECUTING" => ("EXECUTING", EXECUTING),
        "VERIFYING" => ("VERIFYING", VERIFYING),
        "RESOLVED" => ("RESOLVED", RESOLVED),
        "UNRESOLVED" => ("UNRESOLVED", UNRESOLVED),
        "EXECUTION_FAILED" => ("EXECUTION FAILED", EXECUTION_FAILED),
        "REJECTED" => ("REJECTED", REJECTED),
        "CLEARED" => ("CLEARED", CLEARED),
        "INSUFFICIENT_EVIDENCE" => ("INSUFFICIENT EVIDENCE", WARNING),
        _ => ("UNKNOWN", TEXT_MUTED),
    }
}
