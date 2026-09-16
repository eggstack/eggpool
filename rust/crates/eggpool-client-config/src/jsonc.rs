//! Trivia-preserving JSONC scanning and structural editing primitives.
//!
//! OpenCode configs are JSONC: JSON plus `//` line comments, `/* */` block
//! comments, and trailing commas. A normal `serde_json` round trip would
//! destroy comments and renormalize the whole document, so EggPool mutates
//! OpenCode files by splicing only the owned provider entry at exact byte
//! offsets. All other bytes (comments, indentation, ordering, unrelated
//! providers/settings) are preserved byte-for-byte.
//!
//! Implementation-time baseline (2026-09-16, Plan 213 workstream 1):
//!
//! - OpenCode V1 (qualified against 1.18.30): `provider` / `npm` / `options`
//!   shape per `https://opencode.ai/docs/providers/` and
//!   `https://opencode.ai/docs/config/` (global
//!   `~/.config/opencode/opencode.json`, `OPENCODE_CONFIG` override,
//!   `opencode models` lists configured providers/models).
//! - OpenCode V2 (current docs at `https://opencode.ai/v2/docs/providers/`
//!   and `https://opencode.ai/v2/docs/models/`): `providers` (plural) /
//!   `package` / `settings` shape with `env: [...]` credential lists,
//!   per-model `limit` / `capabilities` / `variants`, and Responses-capable
//!   packages including `@opencode/ai/providers/openai-compatible/responses`.
//!
//! Dependency gate (Plan 213 workstream 6): `jsonc-parser` was evaluated at
//! implementation time and deliberately NOT adopted. The narrow scanner below
//! covers the exact operations EggPool needs (comment-aware key lookup,
//! value-span location, trailing-comma tolerance) with zero new audit
//! surface, zero binary-size impact on `eggpool`/`eggpool-connect`, and no
//! MSRV pressure on this Rust 1.81-compatible crate. Revisit only if OpenCode
//! requires structural operations this scanner cannot express.
//!
//! Bounds: inputs are client config files (small). Token vectors grow
//! linearly with input length; callers enforce document-size bounds before
//! invoking this module.

use serde_json::Value;

use crate::error::ClientConfigError;

/// Maximum characters retained from a parser reason string.
const MAX_REASON_CHARS: usize = 160;

/// JSONC lexical token kinds. Whitespace is skipped; every other span is a
/// token so splices can address exact byte offsets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TokKind {
    OpenBrace,
    CloseBrace,
    OpenBracket,
    CloseBracket,
    Colon,
    Comma,
    Str,
    Lit,
    LineComment,
    BlockComment,
}

/// One lexical span with byte offsets into the source text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Tok {
    kind: TokKind,
    start: usize,
    end: usize,
}

impl Tok {
    fn is_comment(self) -> bool {
        matches!(self.kind, TokKind::LineComment | TokKind::BlockComment)
    }

    fn is_significant(self) -> bool {
        !self.is_comment()
    }
}

/// Secret-free JSONC syntax error with a 1-based source location.
///
/// Reasons are generic grammar descriptions; no input content is echoed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JsoncError {
    pub line: usize,
    pub column: usize,
    pub reason: String,
}

impl JsoncError {
    fn new(text: &str, offset: usize, reason: &str) -> Self {
        let (line, column) = line_col(text, offset.min(text.len()));
        let reason: String = reason.chars().take(MAX_REASON_CHARS).collect();
        Self {
            line,
            column,
            reason,
        }
    }

    /// Bounded human rendering without input content.
    pub fn detail(&self) -> String {
        format!(
            "invalid JSONC at line {}, column {}: {}",
            self.line, self.column, self.reason
        )
    }
}

impl std::fmt::Display for JsoncError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.detail())
    }
}

/// Convert a byte offset to a 1-based (line, column) pair (columns count
/// Unicode scalar values).
fn line_col(text: &str, offset: usize) -> (usize, usize) {
    let mut line = 1_usize;
    let mut line_start = 0_usize;
    for (index, byte) in text.char_indices() {
        if index >= offset {
            break;
        }
        if byte == '\n' {
            line += 1;
            line_start = index + 1;
        }
    }
    let column = text[line_start..offset.min(text.len())].chars().count() + 1;
    (line, column)
}

/// Tokenize JSONC text. Returns every significant span plus comments.
fn tokenize(text: &str) -> Result<Vec<Tok>, JsoncError> {
    let bytes = text.as_bytes();
    let mut toks = Vec::new();
    let mut index = 0_usize;
    while index < bytes.len() {
        let byte = bytes[index];
        match byte {
            b'{' => {
                toks.push(Tok {
                    kind: TokKind::OpenBrace,
                    start: index,
                    end: index + 1,
                });
                index += 1;
            }
            b'}' => {
                toks.push(Tok {
                    kind: TokKind::CloseBrace,
                    start: index,
                    end: index + 1,
                });
                index += 1;
            }
            b'[' => {
                toks.push(Tok {
                    kind: TokKind::OpenBracket,
                    start: index,
                    end: index + 1,
                });
                index += 1;
            }
            b']' => {
                toks.push(Tok {
                    kind: TokKind::CloseBracket,
                    start: index,
                    end: index + 1,
                });
                index += 1;
            }
            b':' => {
                toks.push(Tok {
                    kind: TokKind::Colon,
                    start: index,
                    end: index + 1,
                });
                index += 1;
            }
            b',' => {
                toks.push(Tok {
                    kind: TokKind::Comma,
                    start: index,
                    end: index + 1,
                });
                index += 1;
            }
            b'"' => {
                let start = index;
                index += 1;
                let mut closed = false;
                while index < bytes.len() {
                    let current = bytes[index];
                    if current == b'\\' {
                        // Skip the escaped byte without validating it here;
                        // the serde parse of stripped JSON reports bad
                        // escapes with a location.
                        index += 2;
                        continue;
                    }
                    if current == b'"' {
                        index += 1;
                        closed = true;
                        break;
                    }
                    if current == b'\n' {
                        break;
                    }
                    index += 1;
                }
                if !closed {
                    return Err(JsoncError::new(text, start, "unterminated string"));
                }
                toks.push(Tok {
                    kind: TokKind::Str,
                    start,
                    end: index,
                });
            }
            b'/' if index + 1 < bytes.len() && bytes[index + 1] == b'/' => {
                let start = index;
                index += 2;
                while index < bytes.len() && bytes[index] != b'\n' {
                    index += 1;
                }
                toks.push(Tok {
                    kind: TokKind::LineComment,
                    start,
                    end: index,
                });
            }
            b'/' if index + 1 < bytes.len() && bytes[index + 1] == b'*' => {
                let start = index;
                index += 2;
                let mut closed = false;
                while index + 1 < bytes.len() {
                    if bytes[index] == b'*' && bytes[index + 1] == b'/' {
                        index += 2;
                        closed = true;
                        break;
                    }
                    index += 1;
                }
                if !closed {
                    return Err(JsoncError::new(text, start, "unterminated block comment"));
                }
                toks.push(Tok {
                    kind: TokKind::BlockComment,
                    start,
                    end: index,
                });
            }
            b' ' | b'\t' | b'\n' | b'\r' => {
                index += 1;
            }
            _ => {
                // Numbers, true/false/null, or invalid input. Scan a literal
                // run; validity is decided by the serde parse of stripped
                // JSON so errors carry a location.
                let start = index;
                while index < bytes.len() {
                    let current = bytes[index];
                    if current.is_ascii_alphanumeric()
                        || matches!(current, b'+' | b'-' | b'.' | b'_' | b'e' | b'E')
                    {
                        index += 1;
                    } else {
                        break;
                    }
                }
                if start == index {
                    return Err(JsoncError::new(
                        text,
                        start,
                        "unexpected character outside string or comment",
                    ));
                }
                toks.push(Tok {
                    kind: TokKind::Lit,
                    start,
                    end: index,
                });
            }
        }
    }
    Ok(toks)
}

/// Strip comments (replaced by whitespace that preserves newlines and byte
/// offsets) and trailing commas (replaced by a space) so `serde_json` can
/// parse JSONC while keeping error locations aligned with the original text.
pub fn strip_to_json(text: &str) -> Result<String, JsoncError> {
    let toks = tokenize(text)?;
    let mut drop_comma = vec![false; toks.len()];
    for (position, tok) in toks.iter().enumerate() {
        if tok.kind != TokKind::Comma {
            continue;
        }
        let mut next = position + 1;
        while next < toks.len() && toks[next].is_comment() {
            next += 1;
        }
        if next < toks.len()
            && matches!(toks[next].kind, TokKind::CloseBrace | TokKind::CloseBracket)
        {
            drop_comma[position] = true;
        }
    }
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0_usize;
    for (position, tok) in toks.iter().enumerate() {
        if tok.is_comment() {
            out.push_str(&text[cursor..tok.start]);
            for byte in text[tok.start..tok.end].bytes() {
                if byte == b'\n' {
                    out.push('\n');
                } else {
                    out.push(' ');
                }
            }
            cursor = tok.end;
        } else if drop_comma[position] {
            out.push_str(&text[cursor..tok.start]);
            out.push(' ');
            cursor = tok.end;
        }
    }
    out.push_str(&text[cursor..]);
    Ok(out)
}

/// Parse JSONC text into a `serde_json::Value` with location-bounded errors.
pub fn parse_value(text: &str) -> Result<Value, JsoncError> {
    let stripped = strip_to_json(text)?;
    serde_json::from_str(&stripped).map_err(|error| {
        JsoncError::new(&stripped, 0, "JSON structure is invalid")
            .with_serde_location(error.line(), error.column())
    })
}

impl JsoncError {
    fn with_serde_location(mut self, line: usize, column: usize) -> Self {
        self.line = line.max(1);
        self.column = column.max(1);
        self
    }
}

/// One object entry with exact value offsets for splicing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub key: String,
    pub key_start: usize,
    pub value_start: usize,
    pub value_end: usize,
}

/// Token-backed document view for structural lookup without re-parsing.
#[derive(Debug)]
pub struct Doc<'a> {
    text: &'a str,
    toks: Vec<Tok>,
}

impl<'a> Doc<'a> {
    /// Tokenize `text` for structural lookup.
    pub fn parse(text: &'a str) -> Result<Self, JsoncError> {
        Ok(Self {
            text,
            toks: tokenize(text)?,
        })
    }

    /// Source text under inspection.
    pub fn text(&self) -> &'a str {
        self.text
    }

    fn skip_comments(&self, mut index: usize) -> usize {
        while index < self.toks.len() && self.toks[index].is_comment() {
            index += 1;
        }
        index
    }

    fn expect_kind(&self, index: usize, kind: TokKind, what: &str) -> Result<usize, JsoncError> {
        let position = self.skip_comments(index);
        match self.toks.get(position) {
            Some(tok) if tok.kind == kind => Ok(position),
            Some(tok) => Err(JsoncError::new(self.text, tok.start, what)),
            None => Err(JsoncError::new(self.text, self.text.len(), what)),
        }
    }

    /// Parse one value starting at token `index`; returns the value's end
    /// byte offset and the next token index.
    fn parse_value_at(&self, index: usize) -> Result<(usize, usize), JsoncError> {
        let position = self.skip_comments(index);
        let tok = match self.toks.get(position) {
            Some(tok) => *tok,
            None => {
                return Err(JsoncError::new(
                    self.text,
                    self.text.len(),
                    "unexpected end of config while reading a value",
                ));
            }
        };
        match tok.kind {
            TokKind::OpenBrace | TokKind::OpenBracket => {
                let close = if tok.kind == TokKind::OpenBrace {
                    TokKind::CloseBrace
                } else {
                    TokKind::CloseBracket
                };
                let mut cursor = position + 1;
                loop {
                    cursor = self.skip_comments(cursor);
                    let current = match self.toks.get(cursor) {
                        Some(tok) => *tok,
                        None => {
                            return Err(JsoncError::new(
                                self.text,
                                self.text.len(),
                                "unexpected end of config inside an object or array",
                            ));
                        }
                    };
                    if current.kind == close {
                        return Ok((current.end, cursor + 1));
                    }
                    if tok.kind == TokKind::OpenBrace {
                        if current.kind != TokKind::Str {
                            return Err(JsoncError::new(
                                self.text,
                                current.start,
                                "expected an object key",
                            ));
                        }
                        let colon = self.expect_kind(cursor + 1, TokKind::Colon, "expected ':'")?;
                        let (_, next) = self.parse_value_at(colon + 1)?;
                        cursor = next;
                    } else {
                        let (_, next) = self.parse_value_at(cursor)?;
                        cursor = next;
                    }
                    cursor = self.skip_comments(cursor);
                    let separator = match self.toks.get(cursor) {
                        Some(tok) => *tok,
                        None => {
                            return Err(JsoncError::new(
                                self.text,
                                self.text.len(),
                                "unexpected end of config inside an object or array",
                            ));
                        }
                    };
                    if separator.kind == TokKind::Comma {
                        cursor += 1;
                        continue;
                    }
                    if separator.kind == close {
                        return Ok((separator.end, cursor + 1));
                    }
                    return Err(JsoncError::new(
                        self.text,
                        separator.start,
                        "expected ',' or a closing bracket",
                    ));
                }
            }
            TokKind::Str | TokKind::Lit => Ok((tok.end, position + 1)),
            _ => Err(JsoncError::new(
                self.text,
                tok.start,
                "expected a JSON value",
            )),
        }
    }

    /// Entries of the object value spanning `[value_start, value_end)`.
    /// Returns `None` when that value is not an object.
    pub fn object_entries(
        &self,
        value_start: usize,
        value_end: usize,
    ) -> Result<Option<Vec<Entry>>, JsoncError> {
        let open = match self.toks.iter().find(|tok| tok.start == value_start) {
            Some(tok) => *tok,
            None => return Ok(None),
        };
        if open.kind != TokKind::OpenBrace {
            return Ok(None);
        }
        let open_index = match self.toks.iter().position(|tok| *tok == open) {
            Some(index) => index,
            None => return Ok(None),
        };
        let mut entries = Vec::new();
        let mut cursor = open_index + 1;
        loop {
            cursor = self.skip_comments(cursor);
            let current = match self.toks.get(cursor) {
                Some(tok) => *tok,
                None => {
                    return Err(JsoncError::new(
                        self.text,
                        self.text.len(),
                        "unexpected end of config inside an object",
                    ));
                }
            };
            if current.kind == TokKind::CloseBrace {
                if current.end != value_end {
                    return Err(JsoncError::new(
                        self.text,
                        current.start,
                        "object boundary does not match the enclosing value",
                    ));
                }
                return Ok(Some(entries));
            }
            if current.kind != TokKind::Str {
                return Err(JsoncError::new(
                    self.text,
                    current.start,
                    "expected an object key",
                ));
            }
            let key: String = serde_json::from_str(&self.text[current.start..current.end])
                .map_err(|_| {
                    JsoncError::new(self.text, current.start, "object key is not valid JSON")
                })?;
            let colon = self.expect_kind(cursor + 1, TokKind::Colon, "expected ':'")?;
            let value_at = self.skip_comments(colon + 1);
            let value_tok = match self.toks.get(value_at) {
                Some(tok) => *tok,
                None => {
                    return Err(JsoncError::new(
                        self.text,
                        self.text.len(),
                        "unexpected end of config while reading a value",
                    ));
                }
            };
            let (end, next) = self.parse_value_at(value_at)?;
            entries.push(Entry {
                key,
                key_start: current.start,
                value_start: value_tok.start,
                value_end: end,
            });
            cursor = self.skip_comments(next);
            let separator = match self.toks.get(cursor) {
                Some(tok) => *tok,
                None => {
                    return Err(JsoncError::new(
                        self.text,
                        self.text.len(),
                        "unexpected end of config inside an object",
                    ));
                }
            };
            if separator.kind == TokKind::Comma {
                cursor += 1;
                continue;
            }
            if separator.kind == TokKind::CloseBrace {
                if separator.end != value_end {
                    // A nested close cannot terminate the outer object; this
                    // only triggers on unbalanced input the value walk would
                    // otherwise accept.
                    return Err(JsoncError::new(
                        self.text,
                        separator.start,
                        "object boundary does not match the enclosing value",
                    ));
                }
                return Ok(Some(entries));
            }
            return Err(JsoncError::new(
                self.text,
                separator.start,
                "expected ',' or '}'",
            ));
        }
    }

    /// Entries of the root object. Fails when the document is not exactly one
    /// JSONC object value (arrays, scalars, and trailing garbage refuse with
    /// a location).
    pub fn root_entries(&self) -> Result<Vec<Entry>, JsoncError> {
        let first = self.skip_comments(0);
        let open = match self.toks.get(first) {
            Some(tok) => *tok,
            None => {
                return Err(JsoncError::new(
                    self.text,
                    self.text.len(),
                    "config is empty",
                ));
            }
        };
        if open.kind != TokKind::OpenBrace {
            return Err(JsoncError::new(
                self.text,
                open.start,
                "config root must be an object",
            ));
        }
        let (_, next) = self.parse_value_at(first)?;
        let trailing = self.skip_comments(next);
        if trailing < self.toks.len() {
            let tok = self.toks[trailing];
            return Err(JsoncError::new(
                self.text,
                tok.start,
                "unexpected content after the config object",
            ));
        }
        // Re-derive the root close offset from the validated walk: the value
        // ends where `parse_value_at` stopped.
        let (root_end, _) = self.parse_value_at(first)?;
        match self.object_entries(open.start, root_end)? {
            Some(entries) => Ok(entries),
            None => Err(JsoncError::new(
                self.text,
                open.start,
                "config root must be an object",
            )),
        }
    }

    /// True when any comment token intersects `[start, end)`.
    pub fn has_comments_in(&self, start: usize, end: usize) -> bool {
        self.toks
            .iter()
            .any(|tok| tok.is_comment() && tok.start < end && tok.end > start && start < end)
    }

    /// Significant (non-comment) spans fully inside `[start, end)`.
    ///
    /// Only comma-ness is exposed: callers splice text and must never
    /// reinterpret token internals.
    pub fn significant_spans(&self, start: usize, end: usize) -> Vec<SpanInfo> {
        self.toks
            .iter()
            .copied()
            .filter(|tok| tok.is_significant() && tok.start >= start && tok.end <= end)
            .map(|tok| SpanInfo {
                is_comma: tok.kind == TokKind::Comma,
                start: tok.start,
                end: tok.end,
            })
            .collect()
    }

    /// Byte offsets of the root object's braces: `(open_end, close_start,
    /// close_end)`. Fails when the document is not exactly one object.
    pub fn root_span(&self) -> Result<(usize, usize, usize), JsoncError> {
        let first = self.skip_comments(0);
        let open = match self.toks.get(first) {
            Some(tok) => *tok,
            None => {
                return Err(JsoncError::new(
                    self.text,
                    self.text.len(),
                    "config is empty",
                ));
            }
        };
        if open.kind != TokKind::OpenBrace {
            return Err(JsoncError::new(
                self.text,
                open.start,
                "config root must be an object",
            ));
        }
        let (root_end, next) = self.parse_value_at(first)?;
        let trailing = self.skip_comments(next);
        if trailing < self.toks.len() {
            let tok = self.toks[trailing];
            return Err(JsoncError::new(
                self.text,
                tok.start,
                "unexpected content after the config object",
            ));
        }
        Ok((open.end, root_end - 1, root_end))
    }
}

/// One significant span with comma-ness for splice decisions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SpanInfo {
    pub is_comma: bool,
    pub start: usize,
    pub end: usize,
}

impl From<JsoncError> for ClientConfigError {
    fn from(error: JsoncError) -> Self {
        Self::UnsafeRewrite {
            detail: error.detail(),
        }
    }
}

/// Whitespace prefix of the line containing `offset` (indentation probe for
/// spliced blocks).
pub fn line_indent(text: &str, offset: usize) -> String {
    let offset = offset.min(text.len());
    let line_start = text[..offset].rfind('\n').map_or(0, |index| index + 1);
    text[line_start..offset]
        .chars()
        .take_while(|character| *character == ' ' || *character == '\t')
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenizer_sees_comments_and_strings() {
        let text = "{\n// line\n\"a\": \"x//y\", /* block */ \"b\": 1,\n}\n";
        let toks = tokenize(text).expect("tokens");
        assert!(toks.iter().any(|tok| tok.kind == TokKind::LineComment));
        assert!(toks.iter().any(|tok| tok.kind == TokKind::BlockComment));
        // The `//` inside the string is not a comment.
        let comments = toks.iter().filter(|tok| tok.is_comment()).count();
        assert_eq!(comments, 2);
    }

    #[test]
    fn strip_keeps_offsets_and_drops_trailing_commas() {
        let text = "{\n\"a\": 1, // keep\n\"b\": [1, 2,],\n}\n";
        let stripped = strip_to_json(text).expect("strip");
        assert_eq!(stripped.len(), text.len());
        let value: Value = serde_json::from_str(&stripped).expect("json");
        assert_eq!(value.get("a").and_then(Value::as_u64), Some(1));
    }

    #[test]
    fn parse_reports_bounded_locations_without_content() {
        let text = "{\n\"a\": 1,\n\"b\": truely\n}\n";
        let error = parse_value(text).expect_err("must fail");
        assert!(error.line >= 1);
        assert!(!error.detail().contains("truely"));
    }

    #[test]
    fn root_entries_lists_keys_with_spans() {
        let text = "{\n\"provider\": {},\n\"theme\": \"x\"\n}\n";
        let doc = Doc::parse(text).expect("doc");
        let entries = doc.root_entries().expect("entries");
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].key, "provider");
        assert_eq!(&text[entries[1].value_start..entries[1].value_end], "\"x\"");
    }

    #[test]
    fn unbalanced_input_fails_closed_with_location() {
        let text = "{\"a\": {\"b\": 1}\n";
        let doc = Doc::parse(text).expect("tokens");
        assert!(doc.root_entries().is_err());
    }

    #[test]
    fn unterminated_comment_fails_closed() {
        let text = "{\"a\": 1} /* never ends";
        assert!(tokenize(text).is_err());
    }
}
