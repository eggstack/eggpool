//! Self-identifying `epc1` transport encoding.
//!
//! The copy/share representation is shell-friendly and self-identifying:
//!
//! ```text
//! epc1.<base64url-no-padding payload>
//! ```
//!
//! The payload is canonical UTF-8 JSON without compression. Compression was
//! measured as unjustified for the intentionally small default profile
//! (representative profiles are a few hundred bytes; base64url of canonical
//! JSON is adequate and has a smaller dependency/security surface). The codec
//! version unambiguously fixes the algorithm: `epc1` always means
//! base64url(canonical JSON). A future compressed encoding must use a distinct
//! prefix (for example `epc1z`) rather than magic-byte probing.
//!
//! Decoding allocates against bounded lengths and rejects malformed base64,
//! invalid UTF-8, duplicate/invalid required fields, unsupported schemes,
//! whitespace/control-character injection, and unsupported schema versions
//! before unbounded allocation.

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};

use crate::error::ClientConfigError;
use crate::profile::ConnectionProfileV1;

/// Token prefix identifying canonical-JSON encoding.
pub const TOKEN_PREFIX: &str = "epc1.";
/// Maximum encoded token characters (prefix included).
pub const MAX_TOKEN_CHARS: usize = 8192;
/// Maximum base64-decoded payload bytes.
pub const MAX_DECODED_BYTES: usize = 16 * 1024;

/// Encode a validated profile into a shareable token.
pub fn encode_profile(profile: &ConnectionProfileV1) -> Result<String, ClientConfigError> {
    profile.validate()?;
    let canonical = profile.canonical_json()?;
    if canonical.len() > MAX_DECODED_BYTES {
        return Err(ClientConfigError::TooLarge {
            detail: "profile payload exceeds bounded size".to_owned(),
        });
    }
    let encoded = URL_SAFE_NO_PAD.encode(canonical.as_bytes());
    let token = format!("{TOKEN_PREFIX}{encoded}");
    if token.len() > MAX_TOKEN_CHARS {
        return Err(ClientConfigError::TooLarge {
            detail: "encoded token exceeds bounded size".to_owned(),
        });
    }
    Ok(token)
}

/// Decode and validate a shareable token.
pub fn decode_profile(token: &str) -> Result<ConnectionProfileV1, ClientConfigError> {
    if token.len() > MAX_TOKEN_CHARS {
        return Err(ClientConfigError::TooLarge {
            detail: "encoded token exceeds bounded size".to_owned(),
        });
    }
    let payload =
        token
            .strip_prefix(TOKEN_PREFIX)
            .ok_or_else(|| ClientConfigError::InvalidToken {
                detail: format!("expected prefix {TOKEN_PREFIX:?}"),
            })?;
    if payload.is_empty() {
        return Err(ClientConfigError::InvalidToken {
            detail: "token payload is empty".to_owned(),
        });
    }
    if payload.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(ClientConfigError::InvalidToken {
            detail: "token contains whitespace or control characters".to_owned(),
        });
    }
    // Bound allocation: base64 expands 4 chars to at most 3 bytes.
    let max_decoded_estimate = payload
        .len()
        .saturating_mul(3)
        .saturating_div(4)
        .saturating_add(4);
    if max_decoded_estimate > MAX_DECODED_BYTES.saturating_add(1024) {
        return Err(ClientConfigError::TooLarge {
            detail: "token payload exceeds bounded size".to_owned(),
        });
    }
    let decoded = URL_SAFE_NO_PAD
        .decode(payload)
        .map_err(|_| ClientConfigError::InvalidToken {
            detail: "token payload is not valid base64url".to_owned(),
        })?;
    if decoded.len() > MAX_DECODED_BYTES {
        return Err(ClientConfigError::TooLarge {
            detail: "decoded payload exceeds bounded size".to_owned(),
        });
    }
    let text = std::str::from_utf8(&decoded).map_err(|_| ClientConfigError::InvalidToken {
        detail: "token payload is not valid UTF-8".to_owned(),
    })?;
    // Serde struct deserialization rejects duplicate fields and unknown
    // fields (via `deny_unknown_fields`), satisfying the strict boundary.
    let profile: ConnectionProfileV1 =
        serde_json::from_str(text).map_err(|error| ClientConfigError::InvalidToken {
            detail: format!("token payload is not a valid profile: {error}"),
        })?;
    profile.validate().map_err(|error| match error {
        ClientConfigError::UnsupportedSchema { detail } => {
            ClientConfigError::InvalidToken { detail }
        }
        ClientConfigError::InvalidField { field, detail } => ClientConfigError::InvalidToken {
            detail: format!("{field}: {detail}"),
        },
        ClientConfigError::InvalidBaseUrl => ClientConfigError::InvalidToken {
            detail: "base URL must be an absolute HTTP(S) URL".to_owned(),
        },
        other => other,
    })?;
    Ok(profile)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adapter::ClientTarget;
    use crate::profile::ConnectionProfileV1;

    fn profile() -> ConnectionProfileV1 {
        ConnectionProfileV1::new(
            vec![ClientTarget::Codex],
            "https://pool.example/v1",
            "EGGPOOL_API_KEY",
            "/api/integrations/v1/profile",
            1,
            Some("0.8.0"),
        )
        .expect("profile")
    }

    #[test]
    fn token_round_trip_is_deterministic_and_shell_friendly() {
        let first = encode_profile(&profile()).expect("encode");
        let second = encode_profile(&profile()).expect("encode");
        assert_eq!(first, second);
        assert!(first.starts_with("epc1."));
        assert!(!first.contains('+') && !first.contains('/') && !first.contains('='));
        assert!(!first.contains(' ') && !first.contains('\n'));
        // Representative small profile stays well under bounds.
        assert!(
            first.len() < 1024,
            "token is unexpectedly large: {}",
            first.len()
        );
        let decoded = decode_profile(&first).expect("decode");
        assert_eq!(decoded, profile());
    }

    #[test]
    fn malformed_and_oversize_tokens_fail_closed() {
        assert!(decode_profile("not-a-token").is_err());
        assert!(decode_profile("epc1.").is_err());
        assert!(decode_profile("epc1.!!!").is_err());
        assert!(decode_profile("epc1. aGVsbG8=").is_err());
        assert!(decode_profile(&"x".repeat(MAX_TOKEN_CHARS + 1)).is_err());
        // Unsupported schema major fails with an actionable error.
        let mut bad = profile();
        bad.schema = "eggpool.connection/v9".to_owned();
        let canonical = serde_json::to_string(&bad).expect("json");
        let token = format!("{TOKEN_PREFIX}{}", URL_SAFE_NO_PAD.encode(canonical));
        let error = decode_profile(&token).expect_err("unsupported major must fail");
        assert!(error.to_string().contains("major") || error.to_string().contains("unsupported"));
    }

    #[test]
    fn token_rejects_invalid_urls_and_control_injection() {
        let bad = URL_SAFE_NO_PAD.encode(r#"{"schema":"eggpool.connection/v1"}"#);
        assert!(decode_profile(&format!("{TOKEN_PREFIX}{bad}")).is_err());
    }

    #[test]
    fn secret_absence_in_token_debug() {
        let token = encode_profile(&profile()).expect("encode");
        assert!(!token.contains("ep_test"));
        let debug = format!("{token:?}");
        assert!(!debug.contains("ep_test"));
    }
}
