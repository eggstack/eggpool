//! Pure text/document mutation primitives.
//!
//! These helpers preserve comments, unrelated tables, and formatting while
//! converging only EggPool-owned fields. They operate on strings and never
//! touch the filesystem, subprocesses, or EggPool runtime state.

/// Parse a TOML table header (`[table]` or `[[table]]`) from one line.
pub fn toml_line_header(line: &str) -> Option<String> {
    let trimmed = line.trim();
    if let Some(value) = trimmed
        .strip_prefix("[[")
        .and_then(|value| value.strip_suffix("]]"))
    {
        return Some(value.trim().to_owned());
    }
    trimmed
        .strip_prefix('[')
        .and_then(|value| value.strip_suffix(']'))
        .map(|value| value.trim().to_owned())
}

/// Parse a TOML assignment key from one line, ignoring comments and headers.
pub fn toml_assignment_key(line: &str) -> Option<String> {
    let trimmed = line.trim_start();
    if trimmed.starts_with('#') || trimmed.starts_with('[') {
        return None;
    }
    let (key, _) = trimmed.split_once('=')?;
    let key = key.trim().trim_matches(['"', '\'']).trim().to_owned();
    (!key.is_empty()
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'))
    .then_some(key)
}

/// Best-effort TOML string value extraction for drift checks.
pub fn toml_string_value(line: &str) -> Option<String> {
    let (_, raw) = line.split_once('=')?;
    let raw = raw.trim();
    let mut in_string = false;
    let mut escaped = false;
    let mut end = raw.len();
    for (index, character) in raw.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if character == '\\' && in_string {
            escaped = true;
            continue;
        }
        if character == '"' {
            in_string = !in_string;
            continue;
        }
        if character == '#' && !in_string {
            end = index;
            break;
        }
    }
    let value = raw[..end].trim().trim_matches('"').trim().to_owned();
    Some(value)
}

/// Index of the first TOML table header, if any.
pub fn first_table_index(lines: &[String]) -> Option<usize> {
    lines
        .iter()
        .position(|line| toml_line_header(line).is_some())
}

/// Index of a root-level key (before the first table), if present.
pub fn find_root_key(lines: &[String], key: &str) -> Option<usize> {
    let end = first_table_index(lines).unwrap_or(lines.len());
    lines[..end]
        .iter()
        .position(|line| toml_assignment_key(line).as_deref() == Some(key))
}

/// Set a root-level key, inserting before the first table when absent.
pub fn set_root_key(lines: &mut Vec<String>, key: &str, rendered: &str) {
    let line = format!("{key} = {rendered}");
    if let Some(index) = find_root_key(lines, key) {
        lines[index] = line;
        return;
    }
    match first_table_index(lines) {
        Some(index) => lines.insert(index, line),
        None => {
            if !lines.is_empty() && !lines.last().is_some_and(String::is_empty) {
                lines.push(String::new());
            }
            lines.push(line);
        }
    }
}

/// Remove a root-level key, returning its previous string value if any.
pub fn remove_root_key(lines: &mut Vec<String>, key: &str) -> Option<String> {
    let index = find_root_key(lines, key)?;
    let previous = toml_string_value(&lines[index]);
    lines.remove(index);
    previous
}

/// Locate a `[table]` span as `(start, end)` line indexes.
pub fn find_table(lines: &[String], header: &str) -> Option<(usize, usize)> {
    let start = lines
        .iter()
        .position(|line| toml_line_header(line).as_deref() == Some(header))?;
    let mut end = start + 1;
    while end < lines.len() && toml_line_header(&lines[end]).is_none() {
        end += 1;
    }
    Some((start, end))
}

/// Read one value from a `[table]`, if present.
pub fn table_value(lines: &[String], header: &str, key: &str) -> Option<String> {
    let (start, end) = find_table(lines, header)?;
    lines[start + 1..end]
        .iter()
        .find(|line| toml_assignment_key(line).as_deref() == Some(key))
        .and_then(|line| toml_string_value(line))
}

/// Best-effort JSONC detection: look for `//` or `/*` outside strings.
pub fn has_jsonc_comments(raw: &str) -> bool {
    let mut in_string = false;
    let mut escaped = false;
    let bytes = raw.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if escaped {
            escaped = false;
            index += 1;
            continue;
        }
        if in_string {
            if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            index += 1;
            continue;
        }
        if byte == b'"' {
            in_string = true;
            index += 1;
            continue;
        }
        if byte == b'/'
            && index + 1 < bytes.len()
            && (bytes[index + 1] == b'/' || bytes[index + 1] == b'*')
        {
            return true;
        }
        index += 1;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toml_root_keys_stay_before_first_table() {
        let mut lines = vec![
            "# user comment".to_owned(),
            "model_provider = \"other\"".to_owned(),
            String::new(),
            "[other_table]".to_owned(),
            "key = 1".to_owned(),
        ];
        set_root_key(&mut lines, "model_provider", "\"eggpool\"");
        let text = lines.join("\n");
        assert!(text.contains("# user comment"));
        let provider_pos = text.find("model_provider").expect("provider");
        let table_pos = text.find("[other_table]").expect("table");
        assert!(provider_pos < table_pos);
    }

    #[test]
    fn jsonc_detection_ignores_slashes_inside_strings() {
        assert!(!has_jsonc_comments(
            "{\"url\": \"https://example.invalid/v1\"}"
        ));
        assert!(has_jsonc_comments(
            "{\n// user comment\n\"provider\": {}\n}\n"
        ));
        assert!(has_jsonc_comments("{\"a\": 1} /* trailing */"));
    }
}
