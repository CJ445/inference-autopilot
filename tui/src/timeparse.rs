//! RFC 3339 timestamps as the control plane writes them (`2026-10-01T15:02:11.25+00:00`),
//! without a date-time dependency.

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    // Howard Hinnant's algorithm: days since 1970-01-01 in the proleptic Gregorian calendar.
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        _ => 28,
    }
}

/// Seconds since the Unix epoch, or `None` if `s` is not a full RFC 3339 timestamp with an offset.
pub fn parse_rfc3339(s: &str) -> Option<f64> {
    if !s.is_ascii() || s.len() < 20 {
        return None;
    }
    let b = s.as_bytes();
    if b[4] != b'-' || b[7] != b'-' || !matches!(b[10], b'T' | b't') || b[13] != b':' || b[16] != b':' {
        return None;
    }
    let n = |range: std::ops::Range<usize>| s[range].parse::<i64>().ok();
    let (y, mo, d) = (n(0..4)?, n(5..7)?, n(8..10)?);
    let (h, mi, sec) = (n(11..13)?, n(14..16)?, n(17..19)?);
    if !(1..=12).contains(&mo) || d < 1 || d > days_in_month(y, mo) || h > 23 || mi > 59 || sec > 59 {
        return None;
    }
    let mut rest = &s[19..];
    let mut frac = 0.0;
    if let Some(after) = rest.strip_prefix('.') {
        let digits = after.bytes().take_while(u8::is_ascii_digit).count();
        if digits == 0 {
            return None;
        }
        frac = format!("0.{}", &after[..digits]).parse().ok()?;
        rest = &after[digits..];
    }
    let offset = match rest {
        "Z" | "z" => 0,
        _ if rest.len() == 6 && (rest.starts_with('+') || rest.starts_with('-')) && &rest[3..4] == ":" => {
            let oh: i64 = rest[1..3].parse().ok()?;
            let om: i64 = rest[4..6].parse().ok()?;
            if oh > 23 || om > 59 {
                return None;
            }
            let secs = oh * 3600 + om * 60;
            if rest.starts_with('-') { -secs } else { secs }
        }
        _ => return None,
    };
    let local = days_from_civil(y, mo, d) * 86_400 + h * 3600 + mi * 60 + sec;
    Some((local - offset) as f64 + frac)
}

/// `HH:MM:SS` of a control-plane timestamp, or a placeholder if it is not one.
pub fn clock_hms(s: &str) -> String {
    if parse_rfc3339(s).is_some() {
        s[11..19].to_string()
    } else {
        "--:--:--".to_string()
    }
}
