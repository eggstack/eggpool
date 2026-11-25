use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use rcgen::{BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const SERVER_NAME: &str = "agent.example";

#[derive(Debug, Clone, Copy)]
pub enum TicketVariant {
    Valid,
    WrongVersion,
    BadBase64,
    MalformedJson,
    Oversized,
    Expired,
    MissingRequired,
    BadControlAddress,
    MalformedExpiry,
    BadCa,
    ShortEnrollment,
    Tampered,
}

/// TEST-TREE ONLY producer for valid and deliberately invalid enrollment tickets.
pub struct TestTicketProducer {
    ca_pem: String,
    server_cert_pem: String,
    server_key_pem: String,
    enrollment_secret: String,
}

impl TestTicketProducer {
    pub fn new() -> Self {
        let mut ca_params =
            CertificateParams::new(vec!["agent-test-ca".to_owned()]).expect("CA parameters");
        ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let ca_key = KeyPair::generate().expect("CA key");
        let ca_cert = ca_params
            .self_signed(&ca_key)
            .expect("self-signed CA certificate");
        let ca_pem = ca_cert.pem();
        let issuer = Issuer::new(ca_params, ca_key);

        let mut server_params =
            CertificateParams::new(vec![SERVER_NAME.to_owned()]).expect("server parameters");
        server_params
            .extended_key_usages
            .push(ExtendedKeyUsagePurpose::ServerAuth);
        let server_key = KeyPair::generate().expect("server key");
        let server_cert = server_params
            .signed_by(&server_key, &issuer)
            .expect("server certificate signed by test CA");
        let enrollment_secret = URL_SAFE_NO_PAD.encode([0x5a_u8; 32]);

        Self {
            ca_pem,
            server_cert_pem: server_cert.pem(),
            server_key_pem: server_key.serialize_pem(),
            enrollment_secret,
        }
    }

    pub fn ca_pem(&self) -> &str {
        &self.ca_pem
    }

    pub fn server_cert_pem(&self) -> &str {
        &self.server_cert_pem
    }

    pub fn server_key_pem(&self) -> &str {
        &self.server_key_pem
    }

    pub fn ticket(&self, now_unix: i64, control_port: u16, variant: TicketVariant) -> String {
        self.ticket_with_enrollment_secret(now_unix, control_port, variant, &self.enrollment_secret)
    }

    pub fn ticket_with_enrollment_secret(
        &self,
        now_unix: i64,
        control_port: u16,
        variant: TicketVariant,
        enrollment_secret: &str,
    ) -> String {
        match variant {
            TicketVariant::BadBase64 => return "epagent1.not*base64".to_owned(),
            TicketVariant::MalformedJson => {
                return format!("epagent1.{}", URL_SAFE_NO_PAD.encode(b"{"));
            }
            TicketVariant::Tampered => {
                let valid = self.ticket(now_unix, control_port, TicketVariant::Valid);
                return format!("{valid}!");
            }
            _ => {}
        }

        let ca_pin = format!("{:x}", Sha256::digest(self.ca_pem.as_bytes()));
        let mut payload = json!({
            "version": 1,
            "control": format!("{SERVER_NAME}:{control_port}"),
            "server_name": SERVER_NAME,
            "ca_pin": ca_pin,
            "enroll": enrollment_secret,
            "expires_at": now_unix + 60,
            "agent": "test-agent",
            "providers": ["opencode-go"]
        });
        match variant {
            TicketVariant::Valid => {}
            TicketVariant::WrongVersion => payload["version"] = json!(9),
            TicketVariant::Oversized => payload["agent"] = json!("x".repeat(3000)),
            TicketVariant::Expired => payload["expires_at"] = json!(now_unix - 1),
            TicketVariant::MissingRequired => {
                payload
                    .as_object_mut()
                    .expect("ticket payload object")
                    .remove("ca_pin");
            }
            TicketVariant::BadControlAddress => {
                payload["control"] = json!("https://bad.example:7443")
            }
            TicketVariant::MalformedExpiry => payload["expires_at"] = json!("tomorrow"),
            TicketVariant::BadCa => payload["ca_pin"] = json!("0".repeat(64)),
            TicketVariant::ShortEnrollment => {
                payload["enroll"] = json!(URL_SAFE_NO_PAD.encode(b"short"));
            }
            TicketVariant::BadBase64 | TicketVariant::MalformedJson | TicketVariant::Tampered => {
                unreachable!("handled before JSON ticket construction")
            }
        }
        encode_payload(payload)
    }

    pub fn enroll_secret_token(&self) -> &str {
        &self.enrollment_secret
    }
}

impl Default for TestTicketProducer {
    fn default() -> Self {
        Self::new()
    }
}

fn encode_payload(payload: Value) -> String {
    format!(
        "epagent1.{}",
        URL_SAFE_NO_PAD.encode(serde_json::to_vec(&payload).expect("ticket JSON"))
    )
}
