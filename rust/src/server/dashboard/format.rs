pub(super) fn format_microdollars(value: i64) -> String {
    format!("${:.2}", value as f64 / 1_000_000.0)
}

pub(super) fn format_tokens(value: i64) -> String {
    let digits = value.unsigned_abs().to_string();
    let grouped = digits
        .as_bytes()
        .rchunks(3)
        .rev()
        .map(|chunk| std::str::from_utf8(chunk).unwrap_or("0"))
        .collect::<Vec<_>>()
        .join(",");
    if value < 0 {
        format!("-{grouped}")
    } else {
        grouped
    }
}

pub(super) fn format_latency(value: f64) -> String {
    format!("{value:.1} ms")
}

pub(super) fn civil_date_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let shifted = days_since_epoch + 719_468;
    let era = if shifted >= 0 {
        shifted / 146_097
    } else {
        (shifted - 146_096) / 146_097
    };
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month, day)
}

pub(super) fn format_ratio_percent(value: Option<f64>) -> String {
    value
        .filter(|ratio| ratio.is_finite())
        .map(|ratio| format!("{:.1}%", ratio * 100.0))
        .unwrap_or_else(|| "—".to_owned())
}

pub(super) fn format_bytes(value: i64) -> String {
    if value < 1_000 {
        return format!("{value} B");
    }
    let units = ["KB", "MB", "GB", "TB"];
    let mut scaled = value as f64;
    let mut unit = "B";
    for candidate in units {
        scaled /= 1_000.0;
        unit = candidate;
        if scaled < 1_000.0 {
            break;
        }
    }
    format!("{scaled:.1} {unit}")
}

pub(super) fn period_options(current: &str) -> String {
    [
        ("1h", "Last hour"),
        ("24h", "Last 24 hours"),
        ("7d", "Last 7 days"),
        ("30d", "Last 30 days"),
    ]
    .iter()
    .map(|(value, label)| {
        let selected = if *value == current {
            " selected=\"selected\""
        } else {
            ""
        };
        format!("<option value=\"{}\"{}>{}</option>", value, selected, label)
    })
    .collect()
}

pub(super) fn query_component(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    encoded
}

pub(super) fn html_escape(value: impl std::fmt::Display) -> String {
    value
        .to_string()
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

pub(super) fn sanitize_class_name(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '_' | '-') {
                character
            } else {
                '_'
            }
        })
        .collect()
}
