//! The TUI's small semantic palette. Colour only ever means state or focus; widgets never pick
//! their own. The terminal's own background and foreground are used for `BACKGROUND` / `TEXT`
//! so the UI stays terminal-native in any dark theme.
use ratatui::buffer::Buffer;
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

/// How the semantic palette above is shown. The constants are the `Dark` look; the others are
/// applied to the finished frame (`apply`), so no widget ever picks a colour for a theme.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Theme {
    /// The terminal's own named colours: they follow a light or a dark terminal theme, and the
    /// secondary text is the terminal's normal text colour (never a fixed grey).
    #[default]
    Terminal,
    /// The fixed RGB palette above, tuned for a dark background.
    Dark,
    /// No colour at all (`NO_COLOR`): state is always also a word and a glyph.
    Mono,
}

impl Theme {
    pub fn parse(name: &str) -> Option<Theme> {
        match name {
            "terminal" | "auto" => Some(Theme::Terminal),
            "dark" => Some(Theme::Dark),
            "mono" | "none" => Some(Theme::Mono),
            _ => None,
        }
    }

    /// The theme the environment asks for: `NO_COLOR` (any non-empty value, no-color.org) means
    /// Mono; otherwise the terminal's own colours.
    pub fn from_env(no_color: Option<&str>) -> Theme {
        if no_color.is_some_and(|v| !v.is_empty()) { Theme::Mono } else { Theme::Terminal }
    }

    fn map(self, c: Color) -> Color {
        match self {
            Theme::Dark => c,
            Theme::Mono => Color::Reset,
            Theme::Terminal => match c {
                Color::Rgb(128, 134, 150) => Color::Reset,     // secondary text: full contrast
                Color::Rgb(62, 66, 80) => Color::DarkGray,     // rules and borders: decoration only
                Color::Rgb(122, 162, 247) | Color::Rgb(100, 200, 222) | Color::Rgb(110, 190, 175) => Color::Cyan,
                Color::Rgb(115, 201, 145) | Color::Rgb(120, 165, 145) => Color::Green,
                Color::Rgb(229, 181, 90) => Color::Yellow,
                Color::Rgb(240, 98, 98) => Color::Red,
                Color::Rgb(176, 143, 240) => Color::Magenta,
                Color::Rgb(32, 35, 43) => Color::Reset,        // the selected row has a marker too
                other => other,
            },
        }
    }
}

/// Re-colours a finished frame for `theme`. `Dark` is the identity.
pub fn apply(buf: &mut Buffer, theme: Theme) {
    if theme == Theme::Dark {
        return;
    }
    let area = buf.area;
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            let cell = buf.get_mut(x, y);
            let (fg, bg) = (theme.map(cell.fg), theme.map(cell.bg));
            cell.set_fg(fg);
            cell.set_bg(bg);
        }
    }
}
