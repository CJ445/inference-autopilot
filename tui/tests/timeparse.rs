use aiops_tui::timeparse::{clock_hms, parse_rfc3339};

const T: f64 = 1_790_866_931.0; // 2026-10-01T15:02:11Z, computed independently in Python

#[test]
fn parses_the_control_planes_timestamps() {
    assert_eq!(parse_rfc3339("2026-10-01T15:02:11+00:00"), Some(T));
    assert_eq!(parse_rfc3339("2026-10-01T15:02:11Z"), Some(T));
    let frac = parse_rfc3339("2026-10-01T15:02:11.250000+00:00").unwrap();
    assert!((frac - (T + 0.25)).abs() < 1e-6);
}

#[test]
fn honours_utc_offsets() {
    assert_eq!(parse_rfc3339("2026-10-01T17:02:11+02:00"), Some(T));
    assert_eq!(parse_rfc3339("2026-10-01T10:02:11-05:00"), Some(T));
}

#[test]
fn handles_the_epoch_and_leap_days() {
    assert_eq!(parse_rfc3339("1970-01-01T00:00:00+00:00"), Some(0.0));
    assert_eq!(parse_rfc3339("2024-02-29T23:59:59Z"), Some(1_709_251_199.0));
}

#[test]
fn rejects_anything_that_is_not_a_timestamp() {
    for bad in ["", "yesterday", "2026-13-01T00:00:00Z", "2026-10-32T00:00:00Z",
                "2026-10-01T25:00:00Z", "2026-10-01 15:02:11", "2026-10-01T15:02:11", "15:02:11"] {
        assert_eq!(parse_rfc3339(bad), None, "{bad:?}");
    }
}

#[test]
fn clock_shows_the_wall_clock_part_or_a_placeholder() {
    assert_eq!(clock_hms("2026-10-01T15:02:11.250000+00:00"), "15:02:11");
    assert_eq!(clock_hms("garbage"), "--:--:--");
    assert_eq!(clock_hms(""), "--:--:--");
}
