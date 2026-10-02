//! The TUI's semantic palette: one tonal ladder, one accent, three signals. Colour only ever means
//! state or focus; widgets never pick their own. Surfaces are separated by *tone* (page, panel,
//! element, raised), not by drawn boxes.
//!
//! The constants are the `Dark` look. The other themes are applied to the finished frame (`apply`),
//! so no widget ever picks a colour for a theme: `Light` swaps each token for its light twin,
//! `Terminal` uses the terminal's own colours (and no surfaces), `Mono` uses none.
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier};

// The tonal ladder (cool, slightly blue: infrastructure, calm).
pub const PAGE: Color = Color::Rgb(11, 14, 18);
pub const PANEL: Color = Color::Rgb(18, 23, 29);
pub const ELEMENT: Color = Color::Rgb(27, 34, 43);
pub const RAISED: Color = Color::Rgb(36, 45, 57);
pub const BACKGROUND: Color = PAGE;
pub const BACKGROUND_SECONDARY: Color = ELEMENT;

pub const TEXT: Color = Color::Rgb(230, 234, 239);
pub const TEXT_MUTED: Color = Color::Rgb(155, 167, 180);
/// Quiet lines and the bars of finished things (decoration, never information).
pub const BORDER: Color = Color::Rgb(58, 70, 84);
/// The signal: inference, autonomy, "the loop is here". The one accent.
pub const ACCENT: Color = Color::Rgb(90, 209, 196);
pub const BORDER_FOCUSED: Color = ACCENT;

pub const HEALTHY: Color = Color::Rgb(139, 216, 154);
/// Reserved for "this needs you".
pub const WARNING: Color = Color::Rgb(242, 184, 75);
pub const CRITICAL: Color = Color::Rgb(240, 113, 120);

pub const PENDING: Color = WARNING;
pub const INVESTIGATING: Color = ACCENT;
pub const APPROVED: Color = ACCENT;
pub const EXECUTING: Color = ACCENT;
pub const VERIFYING: Color = ACCENT;
pub const RESOLVED: Color = HEALTHY;
pub const REJECTED: Color = TEXT_MUTED;
pub const CLEARED: Color = TEXT_MUTED;
pub const UNRESOLVED: Color = CRITICAL;
pub const EXECUTION_FAILED: Color = CRITICAL;

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

/// How the semantic palette above is shown.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Theme {
    /// The terminal's own named colours: they follow a light or a dark terminal theme, secondary
    /// text is the terminal's normal text colour, and there are no tonal surfaces (the bars, the
    /// spacing and the glyphs carry the structure).
    #[default]
    Terminal,
    /// The tonal palette above, for a dark terminal (truecolor).
    Dark,
    /// The same palette for a light terminal.
    Light,
    /// No colour at all (`NO_COLOR`): state is always also a word and a glyph.
    Mono,
}

impl Theme {
    pub fn parse(name: &str) -> Option<Theme> {
        match name {
            "terminal" | "ansi" => Some(Theme::Terminal),
            "dark" | "studio" | "auto" => Some(Theme::Dark),
            "light" => Some(Theme::Light),
            "mono" | "none" => Some(Theme::Mono),
            _ => None,
        }
    }

    /// What the environment asks for: `NO_COLOR` (non-empty, no-color.org) means Mono; a truecolor
    /// terminal (`COLORTERM` = truecolor or 24bit) gets the tonal palette; anything else gets the
    /// terminal's own colours.
    pub fn from_env(no_color: Option<&str>, colorterm: Option<&str>) -> Theme {
        if no_color.is_some_and(|v| !v.is_empty()) {
            Theme::Mono
        } else if colorterm.is_some_and(|v| v.contains("truecolor") || v.contains("24bit")) {
            Theme::Dark
        } else {
            Theme::Terminal
        }
    }

    /// Whether this theme paints surfaces (page, panel, element, raised).
    pub fn has_surfaces(self) -> bool {
        matches!(self, Theme::Dark | Theme::Light)
    }

    /// The colour a token takes in this theme.
    pub fn resolve(self, c: Color) -> Color {
        match self {
            Theme::Dark => c,
            Theme::Mono => Color::Reset,
            Theme::Light => match c {
                Color::Rgb(11, 14, 18) => Color::Rgb(251, 252, 253),
                Color::Rgb(18, 23, 29) => Color::Rgb(241, 244, 247),
                Color::Rgb(27, 34, 43) => Color::Rgb(232, 237, 242),
                Color::Rgb(36, 45, 57) => Color::Rgb(221, 228, 235),
                Color::Rgb(58, 70, 84) => Color::Rgb(176, 188, 200),
                Color::Rgb(230, 234, 239) => Color::Rgb(20, 24, 29),
                Color::Rgb(155, 167, 180) => Color::Rgb(76, 90, 104),
                Color::Rgb(90, 209, 196) => Color::Rgb(11, 110, 105),
                Color::Rgb(139, 216, 154) => Color::Rgb(22, 104, 46),
                Color::Rgb(242, 184, 75) => Color::Rgb(138, 90, 0),
                Color::Rgb(240, 113, 120) => Color::Rgb(179, 38, 45),
                other => other,
            },
            Theme::Terminal => match c {
                Color::Rgb(230, 234, 239) | Color::Rgb(155, 167, 180) => Color::Reset, // text: full contrast
                Color::Rgb(58, 70, 84) => Color::DarkGray,                              // decoration only
                Color::Rgb(90, 209, 196) => Color::Cyan,
                Color::Rgb(139, 216, 154) => Color::Green,
                Color::Rgb(242, 184, 75) => Color::Yellow,
                Color::Rgb(240, 113, 120) => Color::Red,
                // every surface is the terminal's own background
                Color::Rgb(11, 14, 18) | Color::Rgb(18, 23, 29) | Color::Rgb(27, 34, 43) | Color::Rgb(36, 45, 57) => Color::Reset,
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
            let (fg, bg) = (theme.resolve(cell.fg), theme.resolve(cell.bg));
            cell.set_fg(fg);
            cell.set_bg(bg);
        }
    }
}

/// Dims everything outside `keep`, for the backdrop behind an overlay. Ratatui has no alpha
/// blending: with RGB colours each cell is blended toward black, otherwise it is marked DIM.
pub fn dim_outside(buf: &mut Buffer, keep: Rect) {
    let area = buf.area;
    let blend = |c: Color, retain: f32| match c {
        Color::Rgb(r, g, b) => Some(Color::Rgb((r as f32 * retain) as u8, (g as f32 * retain) as u8, (b as f32 * retain) as u8)),
        _ => None,
    };
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            if x >= keep.left() && x < keep.right() && y >= keep.top() && y < keep.bottom() {
                continue;
            }
            let cell = buf.get_mut(x, y);
            match (blend(cell.fg, 0.45), blend(cell.bg, 0.45)) {
                (None, None) => {
                    cell.modifier.insert(Modifier::DIM);
                }
                (fg, bg) => {
                    if let Some(c) = fg {
                        cell.set_fg(c);
                    } else {
                        cell.modifier.insert(Modifier::DIM);
                    }
                    if let Some(c) = bg {
                        cell.set_bg(c);
                    }
                }
            }
        }
    }
}

/// WCAG contrast ratio between two RGB colours (1.0 to 21.0); `None` if either is not RGB.
pub fn contrast(a: Color, b: Color) -> Option<f64> {
    fn lum(c: Color) -> Option<f64> {
        let Color::Rgb(r, g, bl) = c else { return None };
        let f = |v: u8| {
            let v = v as f64 / 255.0;
            if v <= 0.03928 { v / 12.92 } else { ((v + 0.055) / 1.055).powf(2.4) }
        };
        Some(0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl))
    }
    let (la, lb) = (lum(a)?, lum(b)?);
    let (hi, lo) = if la >= lb { (la, lb) } else { (lb, la) };
    Some((hi + 0.05) / (lo + 0.05))
}
