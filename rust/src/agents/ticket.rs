//! Bounded, secret-redacting decoder for one-time Eggpass enrollment tickets.

use std::{
    io::Read,
    net::IpAddr,
    str::FromStr,
    time::{SystemTime, UNIX_EPOCH},
};

use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use http::uri::Authority;
use serde::{Deserialize, Deserializer};
use sha2::{Digest, Sha256};
use thiserror::Error;
use zeroize::Zeroizing;

use crate::config::AgentsConfig;

const PREFIX: &str = "epagent1.";
pub const MAX_TICKET_BYTES: usize = 2 * 1024;
pub const MAX_TICKET_LIFETIME_SECONDS: i64 = 10 * 60;
const MAX_ENROLL_BYTES: usize = 128;
const MAX_API_KEY_BYTES: usize = 512;
const MAX_EGGPASS_VERSION_BYTES: usize = 64;
const MAX_CA_BUNDLE_BYTES: usize = 1024 * 1024;
const MAX_CA_CERT_DER_BYTES: usize = 768 * 1024;

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
    #[error("ticket agent label, EggPass metadata, or provider list is invalid")]
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

struct ApiKeySecret(Zeroizing<String>);

impl<'de> Deserialize<'de> for ApiKeySecret {
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
    v: Option<serde_json::Value>,
    control: Option<String>,
    server_name: Option<String>,
    ca_sha256: Option<String>,
    enroll: Option<EnrollmentSecret>,
    expires_at: Option<serde_json::Value>,
    agent: Option<String>,
    eggpass: Option<String>,
    api_key: Option<ApiKeySecret>,
    #[serde(default)]
    providers: Vec<String>,
}

/// Parsed public ticket metadata and its zeroizing, private credentials.
pub struct EnrollmentTicket {
    pub control: String,
    pub server_name: String,
    pub ca_pin: String,
    pub agent_label: Option<String>,
    pub providers: Vec<String>,
    expires_at: i64,
    ticket_digest: [u8; 32],
    enroll: Zeroizing<String>,
    api_key: Zeroizing<String>,
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
            .field("ticket_digest", &"[REDACTED]")
            .field("enroll", &"[REDACTED]")
            .field("api_key", &"[REDACTED]")
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
            .map_err(|_| TicketError::Base64)?;
        let payload_bytes = Zeroizing::new(decoded);
        let raw: RawTicket =
            serde_json::from_slice(&payload_bytes).map_err(|_| TicketError::Malformed)?;
        let version = raw
            .v
            .as_ref()
            .and_then(serde_json::Value::as_u64)
            .ok_or(TicketError::Version)?;
        if version != 1 {
            return Err(TicketError::Version);
        }
        let control = raw.control.ok_or(TicketError::MissingField)?;
        let server_name = raw.server_name.ok_or(TicketError::MissingField)?;
        let ca_pin = raw.ca_sha256.ok_or(TicketError::MissingField)?;
        let enroll = raw.enroll.ok_or(TicketError::MissingField)?.0;
        let agent = raw.agent.ok_or(TicketError::MissingField)?;
        let eggpass = raw.eggpass.ok_or(TicketError::MissingField)?;
        let api_key = raw.api_key.ok_or(TicketError::MissingField)?.0;
        let providers = raw.providers;
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
        validate_api_key(&api_key)?;
        if !valid_agent_label(&agent)
            || agent == enroll.as_str()
            || !valid_eggpass_version(&eggpass)
            || providers.len() > 32
            || providers.iter().any(|provider| !valid_identifier(provider))
        {
            return Err(TicketError::OptionalField);
        }
        let ticket_digest = Sha256::digest(input.as_bytes()).into();
        Ok(Self {
            control,
            server_name,
            ca_pin,
            agent_label: Some(agent),
            providers,
            expires_at,
            ticket_digest,
            enroll,
            api_key,
        })
    }

    pub fn expires_at(&self) -> i64 {
        self.expires_at
    }

    /// Digest of the original bearer ticket for durable replay protection.
    pub fn ticket_digest(&self) -> [u8; 32] {
        self.ticket_digest
    }

    /// Borrow the credential only for tunnel startup and secret-file staging.
    pub fn enroll_secret(&self) -> &str {
        self.enroll.as_str()
    }

    /// Borrow EggPass's inference API credential without exposing it via Debug.
    pub fn api_key(&self) -> &str {
        self.api_key.as_str()
    }

    /// Recheck the display label at the configuration publication boundary.
    pub fn has_safe_display_label(&self) -> bool {
        !self
            .agent_label
            .as_deref()
            .is_some_and(|label| label == self.enroll.as_str())
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
        let file = std::fs::File::open(crate::Config::runtime_path(path))
            .map_err(|_| TicketError::MissingCaAuthority)?;
        let mut pem = Vec::new();
        file.take((MAX_CA_BUNDLE_BYTES + 1) as u64)
            .read_to_end(&mut pem)
            .map_err(|_| TicketError::MissingCaAuthority)?;
        if pem.len() > MAX_CA_BUNDLE_BYTES {
            return Err(TicketError::MissingCaAuthority);
        }
        let certificate = first_pem_certificate_der(&pem).ok_or(TicketError::MissingCaAuthority)?;
        let fingerprint = URL_SAFE_NO_PAD.encode(Sha256::digest(certificate.as_slice()));
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
    let decoded = URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| TicketError::CaPin)?;
    if decoded.len() == 32 {
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
            .map_err(|_| TicketError::EnrollmentSecret)?,
    );
    if decoded.len() < 32 {
        return Err(TicketError::EnrollmentSecret);
    }
    Ok(())
}

fn validate_api_key(value: &str) -> Result<(), TicketError> {
    if value.is_empty()
        || value.len() > MAX_API_KEY_BYTES
        || !value.bytes().all(|byte| byte.is_ascii_graphic())
    {
        Err(TicketError::Malformed)
    } else {
        Ok(())
    }
}

fn valid_agent_label(value: &str) -> bool {
    !value.is_empty()
        && value.chars().count() <= 64
        && !value.chars().any(char::is_control)
        && !value.contains('=')
        && !value.contains(':')
        && !value.contains("://")
        && !value.to_ascii_lowercase().starts_with("bearer ")
        && !value.starts_with("sk-")
}

fn valid_eggpass_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_EGGPASS_VERSION_BYTES
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'+'))
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn first_pem_certificate_der(pem: &[u8]) -> Option<Zeroizing<Vec<u8>>> {
    const BEGIN: &[u8] = b"-----BEGIN CERTIFICATE-----";
    const END: &[u8] = b"-----END CERTIFICATE-----";

    let start = find_bytes(pem, BEGIN)?.checked_add(BEGIN.len())?;
    let end = find_bytes(pem.get(start..)?, END)?.checked_add(start)?;
    let encoded = &pem[start..end];
    if encoded.len() > MAX_CA_CERT_DER_BYTES * 4 / 3 + 8 {
        return None;
    }
    let compact: Vec<u8> = encoded
        .iter()
        .copied()
        .filter(|byte| !byte.is_ascii_whitespace())
        .collect();
    let der = STANDARD.decode(compact).ok()?;
    if der.is_empty() || der.len() > MAX_CA_CERT_DER_BYTES {
        return None;
    }
    Some(Zeroizing::new(der))
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

#[cfg(test)]
mod tests {
    use base64::{
        Engine as _,
        engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    };
    use serde_json::{Value, json};
    use sha2::{Digest, Sha256};

    use super::{EnrollmentTicket, MAX_TICKET_LIFETIME_SECONDS, TicketError};

    const NOW: i64 = 1_800_000_000;
    const ENROLL_SECRET: &str = "ticket-secret-sentinel-that-must-never-be-echoed";
    const API_KEY_SECRET: &str = "sk-eggpass-test-api-key-sentinel";

    fn payload() -> Value {
        json!({
            "v": 1,
            "control": "eggpool.example:7443",
            "server_name": "agent.example",
            "ca_sha256": URL_SAFE_NO_PAD.encode([0x11_u8; 32]),
            "enroll": URL_SAFE_NO_PAD.encode([0x5a_u8; 32]),
            "expires_at": NOW + 60,
            "agent": "workstation",
            "eggpass": "0.1.3",
            "api_key": API_KEY_SECRET,
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
        for secret in [ENROLL_SECRET, API_KEY_SECRET] {
            assert!(
                !error.to_string().contains(secret),
                "ticket error must not expose credentials"
            );
        }
        error
    }

    fn certificate_pem(der: &[u8]) -> Vec<u8> {
        let encoded = STANDARD.encode(der);
        format!(
            "-----BEGIN CERTIFICATE-----\n{}\n-----END CERTIFICATE-----\n",
            encoded
        )
        .into_bytes()
    }

    fn write_ca_and_ticket(pin: &str, pem: &[u8]) -> (tempfile::TempDir, EnrollmentTicket) {
        let directory = tempfile::tempdir().expect("temporary directory");
        let ca_path = directory.path().join("server-ca.pem");
        std::fs::write(&ca_path, pem).expect("CA file writes");
        let mut value = payload();
        value["ca_sha256"] = json!(pin);
        let ticket = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid pin format");
        (directory, ticket)
    }

    #[test]
    fn producer_shaped_ticket_keeps_credentials_distinct_and_redacts_debug() {
        let mut value = payload();
        value["enroll"] = json!(ENROLL_SECRET);
        value["agent"] = json!("agent-label-sentinel");
        value["api_key"] = json!(API_KEY_SECRET);
        let ticket = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid producer ticket");
        assert_eq!(
            ticket.remaining_lifetime(NOW),
            std::time::Duration::from_secs(60)
        );
        assert_eq!(ticket.control, "eggpool.example:7443");
        assert_eq!(ticket.enroll_secret(), ENROLL_SECRET);
        assert_eq!(ticket.api_key(), API_KEY_SECRET);
        assert_ne!(ticket.enroll_secret(), ticket.api_key());

        let debug = format!("{ticket:?}");
        for sentinel in [
            ENROLL_SECRET,
            API_KEY_SECRET,
            "eggpool.example",
            "agent.example",
            "agent-label-sentinel",
        ] {
            assert!(
                !debug.contains(sentinel),
                "Debug leaked sensitive ticket data"
            );
        }
        assert!(debug.contains("[REDACTED]"));
    }

    #[test]
    fn accepts_exact_maximum_lifetime_and_rejects_expiry_boundary_and_excess() {
        let mut value = payload();
        value["expires_at"] = json!(NOW + MAX_TICKET_LIFETIME_SECONDS);
        let ticket = EnrollmentTicket::decode(&encode(&value), NOW)
            .expect("exactly the maximum ticket lifetime is accepted");
        assert_eq!(
            ticket.remaining_lifetime(NOW),
            std::time::Duration::from_secs(MAX_TICKET_LIFETIME_SECONDS as u64)
        );

        value["expires_at"] = json!(NOW + MAX_TICKET_LIFETIME_SECONDS + 1);
        assert_eq!(decode_error(&encode(&value)), TicketError::Expired);
        value["expires_at"] = json!(NOW);
        assert_eq!(decode_error(&encode(&value)), TicketError::Expired);
    }

    #[test]
    fn decoder_rejects_unknown_version_fields_and_malformed_input() {
        let mut value = payload();
        value["v"] = json!(9);
        assert_eq!(decode_error(&encode(&value)), TicketError::Version);

        value = payload();
        value["unrecognized"] = json!("ignored fields are forbidden");
        assert_eq!(decode_error(&encode(&value)), TicketError::Malformed);

        value = payload();
        value["version"] = json!(1);
        assert_eq!(decode_error(&encode(&value)), TicketError::Malformed);

        assert_eq!(decode_error("epagent1.not*base64"), TicketError::Base64);
        assert_eq!(
            decode_error(&format!("epagent1.{}", URL_SAFE_NO_PAD.encode(b"{"))),
            TicketError::Malformed
        );
        assert_eq!(
            decode_error(&format!("epagent1.{}", "a".repeat(2 * 1024))),
            TicketError::TooLarge
        );
    }

    #[test]
    fn decoder_requires_all_producer_fields_and_valid_credentials() {
        let mut value = payload();
        value.as_object_mut().expect("object").remove("ca_sha256");
        assert_eq!(decode_error(&encode(&value)), TicketError::MissingField);

        value = payload();
        value.as_object_mut().expect("object").remove("api_key");
        assert_eq!(decode_error(&encode(&value)), TicketError::MissingField);

        value = payload();
        value["expires_at"] = json!("tomorrow");
        assert_eq!(decode_error(&encode(&value)), TicketError::Expiry);

        value = payload();
        value["control"] = json!("https://agent.example:7443");
        assert_eq!(decode_error(&encode(&value)), TicketError::ControlAddress);

        value = payload();
        value["enroll"] = json!(URL_SAFE_NO_PAD.encode(b"short"));
        assert_eq!(decode_error(&encode(&value)), TicketError::EnrollmentSecret);

        value = payload();
        value["api_key"] = json!("bad key with whitespace");
        assert_eq!(decode_error(&encode(&value)), TicketError::Malformed);
    }

    #[test]
    fn agent_label_and_eggpass_metadata_are_safely_bounded() {
        let mut value = payload();
        value["agent"] = json!("https://example.invalid");
        assert_eq!(decode_error(&encode(&value)), TicketError::OptionalField);

        value = payload();
        value["agent"] = json!("a".repeat(65));
        assert_eq!(decode_error(&encode(&value)), TicketError::OptionalField);

        value = payload();
        value["eggpass"] = json!("0.1.3\nmalicious");
        assert_eq!(decode_error(&encode(&value)), TicketError::OptionalField);

        value = payload();
        value["eggpass"] = json!("0".repeat(65));
        assert_eq!(decode_error(&encode(&value)), TicketError::OptionalField);
    }

    #[test]
    fn enrollment_credential_cannot_be_used_as_the_agent_display_label() {
        let mut value = payload();
        value["enroll"] = json!(ENROLL_SECRET);
        value["agent"] = json!(ENROLL_SECRET);
        assert_eq!(decode_error(&encode(&value)), TicketError::OptionalField);

        value["agent"] = json!("safe display label");
        let mut ticket = EnrollmentTicket::decode(&encode(&value), NOW)
            .expect("distinct display label is accepted");
        assert!(ticket.has_safe_display_label());
        ticket.agent_label = Some(ticket.enroll_secret().to_owned());
        assert!(
            !ticket.has_safe_display_label(),
            "publication-boundary recheck refuses a credential-bearing label"
        );
    }

    #[test]
    fn ca_pin_uses_first_certificate_der_from_a_multi_certificate_pem_bundle() {
        let first_der = b"synthetic first certificate DER";
        let second_der = b"synthetic second certificate DER";
        let pem = [certificate_pem(first_der), certificate_pem(second_der)].concat();
        let ca_path = tempfile::tempdir().expect("temporary directory");
        let ca_file = ca_path.path().join("server-ca.pem");
        std::fs::write(&ca_file, &pem).expect("CA file writes");

        let first_pin = URL_SAFE_NO_PAD.encode(Sha256::digest(first_der));
        let mut value = payload();
        value["ca_sha256"] = json!(first_pin);
        let first_ticket = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid pin");
        first_ticket
            .verify_server_ca(ca_file.to_str())
            .expect("first certificate DER pin matches");

        let pem_text_pin = URL_SAFE_NO_PAD.encode(Sha256::digest(&pem));
        value["ca_sha256"] = json!(pem_text_pin);
        let pem_text_ticket = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid pin");
        assert_eq!(
            pem_text_ticket.verify_server_ca(ca_file.to_str()),
            Err(TicketError::CaMismatch)
        );

        value["ca_sha256"] = json!(URL_SAFE_NO_PAD.encode(Sha256::digest(second_der)));
        let second_ticket = EnrollmentTicket::decode(&encode(&value), NOW).expect("valid pin");
        assert_eq!(
            second_ticket.verify_server_ca(ca_file.to_str()),
            Err(TicketError::CaMismatch)
        );
    }

    #[test]
    fn ca_pin_rejects_missing_or_malformed_pem_and_wrong_pin_encoding() {
        let encoded_pin = URL_SAFE_NO_PAD.encode([0x11_u8; 32]);
        let (directory, ticket) = write_ca_and_ticket(&encoded_pin, b"not a PEM certificate");
        let ca_path = directory.path().join("server-ca.pem");
        assert_eq!(
            ticket.verify_server_ca(ca_path.to_str()),
            Err(TicketError::MissingCaAuthority)
        );

        let mut value = payload();
        value["ca_sha256"] = json!("a".repeat(64));
        assert_eq!(decode_error(&encode(&value)), TicketError::CaPin);
    }
}
