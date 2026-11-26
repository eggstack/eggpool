//! One-time EggPass bootstrap helpers: issue a per-agent client identity and deliver its callback.

use std::{net::SocketAddr, time::Duration};

use rcgen::{
    BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose,
};
use thiserror::Error;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};
use zeroize::Zeroizing;

const MAX_PEM_BYTES: usize = 64 * 1024;
const MAX_RESPONSE_HEADERS: usize = 16 * 1024;
const CALLBACK_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum BootstrapError {
    #[error("client certificate could not be issued")]
    CertificateIssue,
    #[error("enrollment callback material was rejected")]
    MaterialRejected,
    #[error("enrollment callback transport failed")]
    CallbackTransport,
    #[error("enrollment callback was rejected")]
    CallbackRejected,
}

/// Per-enrollment certificate payload. Private key material zeroizes on drop.
pub struct IssuedClientCertificate {
    pub ca_pem: String,
    pub certificate_pem: String,
    pub private_key_pem: Zeroizing<String>,
}

/// Issue a short-lived-by-ticket, self-contained CA and its single client-auth leaf.
/// The CA private key is discarded; only its public certificate is retained by EggPool.
pub fn issue_client_certificate() -> Result<IssuedClientCertificate, BootstrapError> {
    let mut ca_params = CertificateParams::new(vec!["eggpool-agent-client-ca".to_owned()])
        .map_err(|_| BootstrapError::CertificateIssue)?;
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    ca_params.key_usages.push(KeyUsagePurpose::KeyCertSign);
    let ca_key = KeyPair::generate().map_err(|_| BootstrapError::CertificateIssue)?;
    let ca_cert = ca_params
        .self_signed(&ca_key)
        .map_err(|_| BootstrapError::CertificateIssue)?;
    let issuer = Issuer::new(ca_params, ca_key);

    let mut client_params = CertificateParams::new(vec!["eggpool-agent-client".to_owned()])
        .map_err(|_| BootstrapError::CertificateIssue)?;
    client_params
        .extended_key_usages
        .push(ExtendedKeyUsagePurpose::ClientAuth);
    client_params
        .key_usages
        .push(KeyUsagePurpose::DigitalSignature);
    let client_key = KeyPair::generate().map_err(|_| BootstrapError::CertificateIssue)?;
    let client_cert = client_params
        .signed_by(&client_key, &issuer)
        .map_err(|_| BootstrapError::CertificateIssue)?;
    let ca_pem = ca_cert.pem();
    let certificate_pem = client_cert.pem();
    let private_key_pem = Zeroizing::new(client_key.serialize_pem());
    if ca_pem.is_empty()
        || certificate_pem.is_empty()
        || private_key_pem.is_empty()
        || ca_pem.len() > MAX_PEM_BYTES
        || certificate_pem.len() > MAX_PEM_BYTES
        || private_key_pem.len() > MAX_PEM_BYTES
    {
        return Err(BootstrapError::CertificateIssue);
    }
    Ok(IssuedClientCertificate {
        ca_pem,
        certificate_pem,
        private_key_pem,
    })
}

/// Forward the exact documented callback to the fixed loopback tunnel listener.
/// No caller-provided Host or destination is consulted.
pub async fn deliver_enrollment_callback(
    external_bind: SocketAddr,
    enrollment_secret: &str,
    certificate_pem: &str,
    private_key_pem: &str,
) -> Result<(), BootstrapError> {
    if !external_bind.ip().is_loopback()
        || enrollment_secret.is_empty()
        || enrollment_secret.len() > 128
        || enrollment_secret.contains(['\r', '\n'])
        || certificate_pem.is_empty()
        || private_key_pem.is_empty()
        || certificate_pem.len() > MAX_PEM_BYTES
        || private_key_pem.len() > MAX_PEM_BYTES
    {
        return Err(BootstrapError::MaterialRejected);
    }
    let body = Zeroizing::new(
        serde_json::to_vec(&serde_json::json!({
            "client_cert_pem": certificate_pem,
            "client_key_pem": private_key_pem,
        }))
        .map_err(|_| BootstrapError::MaterialRejected)?,
    );
    let authorization = Zeroizing::new(format!("Authorization: Bearer {enrollment_secret}\r\n"));
    let request_head = format!(
        "POST /v1/eggpool/enroll HTTP/1.1\r\nHost: 127.0.0.1\r\n{}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        authorization.as_str(),
        body.len()
    );
    let mut request = Zeroizing::new(request_head.into_bytes());
    request.extend_from_slice(&body);

    tokio::time::timeout(CALLBACK_TIMEOUT, async {
        let mut stream = TcpStream::connect(external_bind)
            .await
            .map_err(|_| BootstrapError::CallbackTransport)?;
        stream
            .write_all(&request)
            .await
            .map_err(|_| BootstrapError::CallbackTransport)?;
        let mut response = Vec::with_capacity(1024);
        let mut buffer = [0_u8; 2048];
        loop {
            let read = stream
                .read(&mut buffer)
                .await
                .map_err(|_| BootstrapError::CallbackTransport)?;
            if read == 0 || response.len().saturating_add(read) > MAX_RESPONSE_HEADERS {
                return Err(BootstrapError::CallbackTransport);
            }
            response.extend_from_slice(&buffer[..read]);
            if let Some(header_end) = response.windows(4).position(|window| window == b"\r\n\r\n") {
                let status = response[..header_end]
                    .split(|byte| *byte == b'\n')
                    .next()
                    .and_then(|line| std::str::from_utf8(line).ok())
                    .and_then(|line| line.trim_end_matches('\r').split_ascii_whitespace().nth(1))
                    .and_then(|status| status.parse::<u16>().ok())
                    .ok_or(BootstrapError::CallbackTransport)?;
                return if (200..300).contains(&status) {
                    Ok(())
                } else {
                    Err(BootstrapError::CallbackRejected)
                };
            }
        }
    })
    .await
    .map_err(|_| BootstrapError::CallbackTransport)?
}
