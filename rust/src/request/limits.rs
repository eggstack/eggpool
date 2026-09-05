//! Overflow-safe request, media, document, and token estimates.

use serde_json::Value;
use thiserror::Error;

pub const DEFAULT_MAX_REQUEST_BODY_BYTES: usize = 10 * 1024 * 1024;
pub const MAX_SSE_FRAME_BYTES: usize = 64 * 1024;
pub const MAX_ESTIMATED_INPUT_TOKENS: u64 = 128_000;
pub const CONTEXT_ESTIMATE_MIN_TOKENS: u64 = 1_000;
pub const MAX_IMAGE_BYTES: u64 = 5 * 1024 * 1024;
pub const MAX_PDF_BYTES: u64 = 32 * 1024 * 1024;
pub const ESTIMATED_BYTES_PER_TOKEN: u64 = 3;
pub const ESTIMATED_TEXT_CHARS_PER_TOKEN: u64 = 4;
pub const ESTIMATED_NON_ASCII_BYTES_PER_TOKEN: u64 = 2;
pub const ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR: u64 = 8;
const MAX_ESTIMATE_DEPTH: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum LimitError {
    #[error("invalid {field}: expected a positive integer")]
    InvalidPositiveInteger { field: &'static str },
    #[error("invalid base64 payload")]
    InvalidBase64,
    #[error("base64 payload exceeds {kind} limit")]
    EncodedPayloadTooLarge { kind: &'static str },
}

pub fn estimate_input_tokens(body: &[u8]) -> u64 {
    if body.is_empty() {
        return CONTEXT_ESTIMATE_MIN_TOKENS;
    }
    let estimate = ceil_div(body.len() as u64, ESTIMATED_BYTES_PER_TOKEN);
    estimate.max(CONTEXT_ESTIMATE_MIN_TOKENS)
}

pub fn estimate_reservation_tokens(body: &[u8]) -> u64 {
    estimate_input_tokens(body).min(MAX_ESTIMATED_INPUT_TOKENS)
}

pub fn estimate_context_input_tokens(body: &[u8], value: &Value, extra_input_tokens: u64) -> u64 {
    let payload_estimate = estimate_json_value_tokens(value);
    let byte_floor = ceil_div(body.len() as u64, ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR);
    payload_estimate.max(
        byte_floor
            .saturating_add(extra_input_tokens)
            .max(CONTEXT_ESTIMATE_MIN_TOKENS),
    )
}

pub fn estimate_json_value_tokens(value: &Value) -> u64 {
    estimate_value(value, 0)
}

fn estimate_value(value: &Value, depth: usize) -> u64 {
    if depth >= MAX_ESTIMATE_DEPTH {
        return estimate_text_tokens(&value.to_string()).max(1);
    }
    match value {
        Value::String(text) => estimate_text_tokens(text),
        Value::Object(map) => map.iter().fold(1_u64, |total, (key, child)| {
            total
                .saturating_add(estimate_text_tokens(key))
                .saturating_add(estimate_value(child, depth + 1))
                .saturating_add(1)
        }),
        Value::Array(items) => items.iter().fold(0_u64, |total, item| {
            total.saturating_add(estimate_value(item, depth + 1))
        }),
        Value::Null | Value::Bool(_) => 1,
        Value::Number(number) => estimate_text_tokens(&number.to_string()).max(1),
    }
}

pub fn estimate_text_tokens(text: &str) -> u64 {
    if text.is_empty() {
        return 0;
    }
    if text.is_ascii() {
        return ceil_div(text.len() as u64, ESTIMATED_TEXT_CHARS_PER_TOKEN);
    }
    let ascii_chars = text
        .chars()
        .filter(|character| character.is_ascii())
        .count() as u64;
    let non_ascii_bytes = text.len() as u64 - ascii_chars;
    ceil_div(ascii_chars, ESTIMATED_TEXT_CHARS_PER_TOKEN).saturating_add(ceil_div(
        non_ascii_bytes,
        ESTIMATED_NON_ASCII_BYTES_PER_TOKEN,
    ))
}

pub fn requested_output_tokens(
    value: &Value,
    protocol: &str,
    request_surface: &str,
) -> Result<Option<u64>, LimitError> {
    let keys: &[&str] = if request_surface == "responses" {
        &["max_output_tokens", "max_completion_tokens", "max_tokens"]
    } else if protocol == "anthropic" {
        &["max_tokens"]
    } else {
        &["max_completion_tokens", "max_tokens"]
    };
    let Some(object) = value.as_object() else {
        return Ok(None);
    };
    for key in keys {
        if let Some(candidate) = object.get(*key) {
            if candidate.is_null() {
                continue;
            }
            let Some(number) = candidate.as_u64() else {
                return Err(LimitError::InvalidPositiveInteger { field: key });
            };
            return Ok((number > 0).then_some(number));
        }
    }
    Ok(None)
}

pub fn decoded_base64_len(encoded: &str) -> Option<u64> {
    if encoded.is_empty() || encoded.len() % 4 != 0 {
        return None;
    }
    let padding = encoded
        .as_bytes()
        .iter()
        .rev()
        .take_while(|byte| **byte == b'=')
        .count();
    if padding > 2 || encoded.as_bytes()[..encoded.len() - padding].contains(&b'=') {
        return None;
    }
    if !encoded
        .as_bytes()
        .iter()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'+' | b'/' | b'='))
    {
        return None;
    }
    let quartets = (encoded.len() / 4) as u64;
    quartets
        .checked_mul(3)
        .and_then(|length| length.checked_sub(padding as u64))
}

pub fn base64_definitely_exceeds(encoded: &str, limit_bytes: u64) -> bool {
    if encoded.len() % 4 != 0 {
        return false;
    }
    let minimum_decoded = (encoded.len() as u64 / 4)
        .checked_mul(3)
        .and_then(|value| value.checked_sub(2))
        .unwrap_or(u64::MAX);
    minimum_decoded > limit_bytes
}

pub fn validate_base64(
    encoded: &str,
    limit_bytes: u64,
    kind: &'static str,
) -> Result<u64, LimitError> {
    if base64_definitely_exceeds(encoded, limit_bytes) {
        return Err(LimitError::EncodedPayloadTooLarge { kind });
    }
    let Some(decoded_len) = decoded_base64_len(encoded) else {
        return Err(LimitError::InvalidBase64);
    };
    if decoded_len > limit_bytes {
        return Err(LimitError::EncodedPayloadTooLarge { kind });
    }
    Ok(decoded_len)
}

fn ceil_div(value: u64, divisor: u64) -> u64 {
    value.saturating_add(divisor - 1) / divisor
}
