//! Bounded, secret-redacting decoder for one-time Eggpass enrollment tickets.

use std::{
    net::IpAddr,
    str::FromStr,
    time::{SystemTime, UNIX_EPOCH},
};

use base64::{
    Engine as _,
    engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD},
};
use http::uri::Authority;
use serde::{Deserialize, Deserializer};
use sha2::{Digest, Sha256};
use thiserror::Error;
use zeroize::Zeroizing;

use crate::config::AgentsConfig;

const PREFIX: &str = "epagent1.";
pub const MAX_TICKET_BYTES: usize = 2 * 1024;
pub const MAX_TICKET_LIFETIME_SECONDS: i64 = 2 * 60;
const MAX_ENROLL_BYTES: usize = 128;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum TicketError {
    #[error("ticket is too large")]
    TooLarge,
    #[error("ticket prefix is invalid")]
    Prefix,
    #[error("ticket encoding is invalid")]
    Base64,
    #[error("ticket payload is malformed")]
    Malformed,
    #[error("ticket version is not supported")]
    Version,
    #[error("ticket expiry is missing or malformed")]
    Expiry,
    #[error("ticket is expired or has an excessive lifetime")]
    Expired,
    #[error("ticket is missing a required field")]
    MissingField,
    #[error("ticket control address is invalid")]
    ControlAddress,
    #[error("ticket control endpoint does not match operator configuration")]
    ControlEndpointMismatch,
    #[error("ticket control port is outside the operator-assigned range")]
    ControlPort,
    #[error("loopback control endpoint requires a single-host deployment")]
    LoopbackControl,
    #[error("ticket server name is invalid")]
    ServerName,
    #[error("ticket CA pin is malformed")]
    CaPin,
    #[error("agent server CA authority is not configured")]
    MissingCaAuthority,
    #[error("ticket CA pin does not match the configured agent server CA")]
    CaMismatch,
    #[error("ticket enrollment secret is malformed or too short")]
    EnrollmentSecret,
    #[error("ticket display label or provider list is invalid")]
    OptionalField,
}

struct EnrollmentSecret(Zeroizing<String>);

impl<'de> Deserialize<'de> for EnrollmentSecret {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        String::deserialize(deserializer).map(|value| Self(Zeroizing::new(value)))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawTicket {
    version: Option<serde_json::Value>,
    control: Option<String>,
    server_name: Option<String>,
    ca_pin: Option<String>,
    enroll: Option<EnrollmentSecret>,
    expires_at: Option<serde_json::Value>,
    #[serde(default)]
    agent: Option<String>,
    #[serde(default)]
    providers: Vec<String>,
}

/// Parsed public ticket metadata and its zeroizing, private transport secret.
pub struct EnrollmentTicket {
    pub control: String,
    pub server_name: String,
    pub ca_pin: String,
    pub agent_label: Option<String>,
    pub providers: Vec<String>,
    expires_at: i64,
    enroll: Zeroizing<String>,
}

impl std::fmt::Debug for EnrollmentTicket {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("EnrollmentTicket")
            .field("control", &"[REDACTED]")
            .field("server_name", &"[REDACTED]")
            .field("ca_pin", &"[REDACTED]")
            .field("agent_label", &"[REDACTED]")
            .field("providers", &"[REDACTED]")
            .field("expires_at", &"[REDACTED]")
            .field("enroll", &"[REDACTED]")
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlAddress {
    pub host: String,
    pub port: u16,
}

impl EnrollmentTicket {
    /// Decode and validate the versioned ticket. now_unix is injectable so
    /// expiry checks have deterministic tests and no wall-clock sleeps.
    pub fn decode(input: &str, now_unix: i64) -> Result<Self, TicketError> {
        if input.len() > MAX_TICKET_BYTES {
            return Err(TicketError::TooLarge);
        }
        let encoded = input.strip_prefix(PREFIX).ok_or(TicketError::Prefix)?;
        if encoded.is_empty() {
            return Err(TicketError::Base64);
        }
        let decoded = URL_SAFE_NO_PAD
            .decode(encoded)
            .or_else(|_| URL_SAFE.decode(encoded))
            .map_err(|_| TicketError::Base64)?;
        let payload_bytes = Zeroizing::new(decoded);
        let raw: RawTicket =
            serde_json::from_slice(&payload_bytes).map_err(|_| TicketError::Malformed)?;
        let version = raw
            .version
            .as_ref()
            .and_then(serde_json::Value::as_u64)
            .ok_or(TicketError::Version)?;
        if version != 1 {
            return Err(TicketError::Version);
        }
        let control = raw.control.ok_or(TicketError::MissingField)?;
        let server_name = raw.server_name.ok_or(TicketError::MissingField)?;
        let ca_pin = raw.ca_pin.ok_or(TicketError::MissingField)?;
        let enroll = raw.enroll.ok_or(TicketError::MissingField)?.0;
        let expires_at = raw
            .expires_at
            .as_ref()
            .and_then(serde_json::Value::as_i64)
            .ok_or(TicketError::Expiry)?;
        if expires_at <= now_unix
            || expires_at.saturating_sub(now_unix) > MAX_TICKET_LIFETIME_SECONDS
        {
            return Err(TicketError::Expired);
        }
        parse_control_address(&control)?;
        if !valid_server_name(&server_name) {
            return Err(TicketError::ServerName);
        }
        validate_pin(&ca_pin)?;
        validate_enroll_secret(&enroll)?;
        if raw.agent.as_ref().is_some_and(|label| {
            label.chars().count() > 64
                || label.chars().any(char::is_control)
                || label.contains('=')
                || label.contains(':')
                || label.contains("://")
                || label.to_ascii_lowercase().starts_with("bearer ")
                || label.starts_with("sk-")
        }) || raw.providers.len() > 32
            || raw
                .providers
                .iter()
                .any(|provider| !valid_identifier(provider))
        {
            return Err(TicketError::OptionalField);
        }
        let ca_pin = ca_pin
            .strip_prefix("sha256:")
            .unwrap_or(&ca_pin)
            .to_ascii_lowercase();
        Ok(Self {
            control,
            server_name,
            ca_pin,
            agent_label: raw.agent,
            providers: raw.providers,
            expires_at,
            enroll,
        })
    }

    pub fn expires_at(&self) -> i64 {
        self.expires_at
    }

    /// Borrow the credential only for tunnel startup and secret-file staging.
    pub fn enroll_secret(&self) -> &str {
        self.enroll.as_str()
    }

    pub fn control_address(&self) -> Result<ControlAddress, TicketError> {
        parse_control_address(&self.control)
    }

    /// Check operator-assigned address policy before any ticket is consumed.
    pub fn validate_operator_address(
        &self,
        agents: &AgentsConfig,
    ) -> Result<ControlAddress, TicketError> {
        let address = self.control_address()?;
        let configured_host = agents.advertised_control_host.trim().trim_end_matches('.');
        if configured_host.is_empty()
            || !address
                .host
                .trim_end_matches('.')
                .eq_ignore_ascii_case(configured_host)
        {
            return Err(TicketError::ControlEndpointMismatch);
        }
        if address.port < agents.control_port_min || address.port > agents.control_port_max {
            return Err(TicketError::ControlPort);
        }
        if IpAddr::from_str(address.host.as_str()).is_ok_and(|ip| ip.is_loopback())
            && !agents.single_host
        {
            return Err(TicketError::LoopbackControl);
        }
        Ok(address)
    }

    /// Verify the ticket SHA-256 pin against the configured server CA PEM.
    /// The client-certificate CA is intentionally never consulted here.
    pub fn verify_server_ca(&self, server_ca_path: Option<&str>) -> Result<(), TicketError> {
        let path = server_ca_path
            .filter(|path| !path.trim().is_empty())
            .ok_or(TicketError::MissingCaAuthority)?;
        let bytes = std::fs::read(crate::Config::runtime_path(path))
            .map_err(|_| TicketError::MissingCaAuthority)?;
        let fingerprint = format!("{:x}", Sha256::digest(bytes));
        if self.ca_pin != fingerprint {
            return Err(TicketError::CaMismatch);
        }
        Ok(())
    }

    /// Remaining lifetime used to bound the consumed-token replay record.
    pub fn remaining_lifetime(&self, now_unix: i64) -> std::time::Duration {
        std::time::Duration::from_secs(self.expires_at.saturating_sub(now_unix).max(0) as u64)
    }

    /// A one-way digest for the memory-only replay registry.
    pub fn enrollment_digest(&self) -> [u8; 32] {
        Sha256::digest(self.enroll.as_bytes()).into()
    }
}

pub fn current_unix_time() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs().min(i64::MAX as u64) as i64)
}

fn parse_control_address(value: &str) -> Result<ControlAddress, TicketError> {
    let authority = Authority::from_str(value).map_err(|_| TicketError::ControlAddress)?;
    if authority.as_str().contains('@') {
        return Err(TicketError::ControlAddress);
    }
    let host = authority
        .host()
        .trim_start_matches('[')
        .trim_end_matches(']');
    let port = authority
        .port_u16()
        .filter(|port| *port != 0)
        .ok_or(TicketError::ControlAddress)?;
    if !valid_control_host(host) {
        return Err(TicketError::ControlAddress);
    }
    Ok(ControlAddress {
        host: host.to_owned(),
        port,
    })
}

fn valid_control_host(host: &str) -> bool {
    if host.is_empty() || host.len() > 253 || host.chars().any(char::is_control) {
        return false;
    }
    if host.parse::<IpAddr>().is_ok() {
        return true;
    }
    valid_dns_name(host)
}

fn valid_server_name(value: &str) -> bool {
    !value.is_empty() && value.len() <= 253 && valid_dns_name(value)
}

fn valid_dns_name(value: &str) -> bool {
    let name = value.strip_suffix('.').unwrap_or(value);
    !name.is_empty()
        && name.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        })
}

fn validate_pin(value: &str) -> Result<(), TicketError> {
    let pin = value.strip_prefix("sha256:").unwrap_or(value);
    if pin.len() == 64 && pin.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        Ok(())
    } else {
        Err(TicketError::CaPin)
    }
}

fn validate_enroll_secret(value: &str) -> Result<(), TicketError> {
    if value.is_empty() || value.len() > MAX_ENROLL_BYTES {
        return Err(TicketError::EnrollmentSecret);
    }
    let decoded = Zeroizing::new(
        URL_SAFE_NO_PAD
            .decode(value)
            .or_else(|_| URL_SAFE.decode(value))
            .map_err(|_| TicketError::EnrollmentSecret)?,
    );
    if decoded.len() < 32 {
        return Err(TicketError::EnrollmentSecret);
    }
    Ok(())
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

#[cfg(test)]
mod tests {
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use serde_json::{Value, json};
    use sha2::{Digest, Sha256};

    use super::{EnrollmentTicket, TicketError};

    const NOW: i64 = 1_800_000_000;
    const SECRET: &str = "ticket-secret-sentinel-that-must-never-be-echoed";

    fn payload() -> Value {
        json!({
            "version": 1,
            "control": "eggpool.example:7443",
            "server_name": "agent.example",
            "ca_pin": "a".repeat(64),
            "enroll": URL_SAFE_NO_PAD.encode([0x5a_u8; 32]),
            "expires_at": NOW + 60,
        })
    }

    fn encode(value: &Value) -> String {
        format!(
            "epagent1.{}",
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(value).expect("ticket serializes"))
        )
    }

    fn decode_error(ticket: &str) -> TicketError {
        let error = EnrollmentTicket::decode(ticket, NOW).expect_err("ticket is rejected");
        assert!(
            !error.to_string().contains(SECRET),
            "ticket error must not expose enrollment material"
        );
        error
    }

    #[test]
    fn decoder_accepts_a_valid_ticket_and_redacts_debug() {
        let mut value = payload();
        value["enroll"] = json!(SECRET);
        value["agent"] = json!("agent-label-sentinel");
        value["providers"] = json!(["provider-sentinel"]);
        let pin_sentinel = "b".repeat(64);
        value["ca_pin"] = json!(&pin_sentinel);
        let ticket = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid ticket");
        assert_eq!(
            ticket.remaining_lifetime(NOW),
            std::time::Duration::from_secs(60)
        );
        assert_eq!(ticket.control, "eggpool.example:7443");
        assert_eq!(ticket.enroll_secret(), SECRET);
        let debug = format!("{ticket:?}");
        for sentinel in [
            SECRET,
            "eggpool.example",
            "agent.example",
            "agent-label-sentinel",
            "provider-sentinel",
            pin_sentinel.as_str(),
        ] {
            assert!(!debug.contains(sentinel), "Debug leaked {sentinel}");
        }
        assert!(debug.contains("[REDACTED]"));
    }

    #[test]
    fn decoder_rejects_malformed_ticket_matrix_without_echoing_secret() {
        let mut value = payload();
        value["enroll"] = json!(SECRET);
        let valid = encode(&value);
        let mut bad = payload();
        bad["version"] = json!(9);
        assert_eq!(decode_error(&encode(&bad)), TicketError::Version);
        assert_eq!(decode_error("epagent1.not*base64"), TicketError::Base64);
        assert_eq!(
            decode_error(&format!("epagent1.{}", URL_SAFE_NO_PAD.encode(b"{"))),
            TicketError::Malformed
        );
        assert_eq!(
            decode_error(&format!("epagent1.{}", "a".repeat(2 * 1024))),
            TicketError::TooLarge
        );
        bad = payload();
        bad["expires_at"] = json!(NOW - 1);
        assert_eq!(decode_error(&encode(&bad)), TicketError::Expired);
        bad = payload();
        bad.as_object_mut().expect("object").remove("ca_pin");
        assert_eq!(decode_error(&encode(&bad)), TicketError::MissingField);
        bad = payload();
        bad["expires_at"] = json!("tomorrow");
        assert_eq!(decode_error(&encode(&bad)), TicketError::Expiry);
        bad = payload();
        bad["control"] = json!("https://agent.example:7443");
        assert_eq!(decode_error(&encode(&bad)), TicketError::ControlAddress);
        bad = payload();
        bad["enroll"] = json!(URL_SAFE_NO_PAD.encode(b"short"));
        assert_eq!(decode_error(&encode(&bad)), TicketError::EnrollmentSecret);
        assert!(!valid.is_empty());
    }

    #[test]
    fn ca_pin_is_checked_against_the_server_ca_file() {
        let directory = tempfile::tempdir().expect("temporary directory");
        let ca = b"fixture server CA authority";
        let ca_path = directory.path().join("server-ca.pem");
        std::fs::write(&ca_path, ca).expect("CA file writes");
        let mut value = payload();
        value["ca_pin"] = json!(format!("{:x}", Sha256::digest(ca)));
        let ticket = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid pin format");
        ticket
            .verify_server_ca(ca_path.to_str())
            .expect("matching server CA pin");
        value["ca_pin"] = json!("b".repeat(64));
        let mismatched = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid pin format");
        assert_eq!(
            mismatched.verify_server_ca(ca_path.to_str()),
            Err(TicketError::CaMismatch)
        );
        assert_eq!(
            mismatched.verify_server_ca(None),
            Err(TicketError::MissingCaAuthority)
        );
    }
}
