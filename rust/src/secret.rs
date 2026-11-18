//! Small zeroizing wrapper for configuration-owned secret strings.
//!
//! This is intentionally not a process-wide secret-management facility:
//! environment values, CLI arguments, request bodies, and downstream header
//! copies remain owned by their existing boundaries.

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use zeroize::{ZeroizeOnDrop, Zeroizing};

/// A string that is wiped when its owning configuration copy is dropped.
///
/// The serialized representation remains an ordinary TOML/Serde string so
/// existing configuration files and reload projections keep their shape.
#[derive(Clone, Eq, PartialEq)]
pub struct SecretString(Zeroizing<String>);

impl SecretString {
    /// Borrow the plaintext only at a call site that must use the secret.
    pub fn expose_secret(&self) -> &str {
        self.0.as_str()
    }
}

impl From<String> for SecretString {
    fn from(value: String) -> Self {
        Self(Zeroizing::new(value))
    }
}

impl From<&str> for SecretString {
    fn from(value: &str) -> Self {
        Self::from(value.to_owned())
    }
}

impl std::fmt::Debug for SecretString {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("SecretString([REDACTED])")
    }
}

impl Serialize for SecretString {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(self.expose_secret())
    }
}

impl<'de> Deserialize<'de> for SecretString {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        String::deserialize(deserializer).map(Self::from)
    }
}

impl ZeroizeOnDrop for SecretString {}

#[cfg(test)]
mod tests {
    use super::SecretString;

    #[derive(Debug, serde::Deserialize, serde::Serialize)]
    struct SecretHolder {
        value: SecretString,
    }

    #[test]
    fn secret_round_trips_as_a_plain_string_and_redacts_debug() {
        let secret = SecretString::from("sentinel-config-secret");
        assert_eq!(secret.expose_secret(), "sentinel-config-secret");
        assert_eq!(format!("{secret:?}"), "SecretString([REDACTED])");

        let toml = toml::to_string(&SecretHolder {
            value: secret.clone(),
        })
        .expect("serialize");
        assert_eq!(toml, "value = \"sentinel-config-secret\"\n");
        let parsed: SecretHolder = toml::from_str(&toml).expect("deserialize");
        assert_eq!(parsed.value.expose_secret(), "sentinel-config-secret");
        assert!(!format!("{parsed:?}").contains("sentinel-config-secret"));
    }
}
