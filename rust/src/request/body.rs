//! Deterministic, provider-independent JSON body encoding.

use bytes::Bytes;
use serde_json::Value;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum BodyEncodingError {
    #[error("canonical JSON body serialization failed: {0}")]
    Serialize(#[from] serde_json::Error),
    #[error("canonical JSON body exceeds the configured encoded limit")]
    TooLarge { length: usize, limit: usize },
}

/// The result of compact JSON encoding before any provider transport policy.
#[derive(Clone, PartialEq, Eq)]
pub struct EncodedJsonBody {
    pub bytes: Bytes,
    pub uncompressed_len: usize,
}

impl std::fmt::Debug for EncodedJsonBody {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("EncodedJsonBody")
            .field("uncompressed_len", &self.uncompressed_len)
            .finish_non_exhaustive()
    }
}

/// Encode a JSON value with no whitespace and without provider headers.
pub fn encode_compact_json(value: &Value) -> Result<EncodedJsonBody, BodyEncodingError> {
    let bytes = serde_json::to_vec(value)?;
    let uncompressed_len = bytes.len();
    Ok(EncodedJsonBody {
        bytes: Bytes::from(bytes),
        uncompressed_len,
    })
}

pub fn encode_compact_json_bounded(
    value: &Value,
    max_bytes: usize,
) -> Result<EncodedJsonBody, BodyEncodingError> {
    let encoded = encode_compact_json(value)?;
    if encoded.uncompressed_len > max_bytes {
        return Err(BodyEncodingError::TooLarge {
            length: encoded.uncompressed_len,
            limit: max_bytes,
        });
    }
    Ok(encoded)
}
