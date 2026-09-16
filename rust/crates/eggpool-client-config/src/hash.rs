//! Secret-free hashing helpers for portable client configuration.
//!
//! Hashes are computed over sanitized canonical content only. Callers must
//! never hash resolved credentials; the portable types contain no secrets by
//! construction.

use sha2::{Digest, Sha256};

/// Compute the lowercase hex SHA-256 of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex_bytes(&hasher.finalize())
}

/// Lowercase hex encoding without external dependencies.
pub fn hex_bytes(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(char::from_digit(u32::from(byte >> 4), 16).expect("hex digit"));
        output.push(char::from_digit(u32::from(byte & 0x0f), 16).expect("hex digit"));
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_hex_is_deterministic_lowercase() {
        let first = sha256_hex(b"eggpool");
        let second = sha256_hex(b"eggpool");
        assert_eq!(first, second);
        assert!(first.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "empty input must hash to the well-known SHA-256 digest"
        );
    }
}
