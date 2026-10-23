//! Durable terminal-detail redaction guards.
//!
//! A persisted terminal detail is an operator-facing diagnostic column, so it
//! must never carry credential-shaped material, and it must keep ordinary
//! diagnostic prose substantially intact. The durable rows are seeded
//! directly, so the guard exercises the persistence path itself and does not
//! depend on routing selection.

use eggpool::{
    coordinator::{DurableFinalizer, FinalizationData, FinalizationIdentity, FinalizationOutcome},
    db::{Database, DatabaseConfig, MigrationRunner},
    identity::{AttributedRequestKind, PrincipalKind},
};
use tokio_rusqlite::rusqlite::params;

const ACCOUNT_ID: i64 = 7;
const REQUEST_ID: i64 = 1;
const ATTEMPT_ID: i64 = 1;

/// Credential-shaped material that must never reach durable storage.
const REDACTED_SECRETS: [&str; 3] = [
    "sk-live-EXAMPLEKEY1234567890abcdef",
    "abcdef1234567890abcdef1234567890",
    "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF",
];

/// A detail that mixes ordinary diagnostic prose with echoed credentials.
fn credential_bearing_detail() -> String {
    format!(
        "upstream rejected the call: Authorization: Bearer {0}; x-api-key: {1}; api_key={2}; \"api_key\":\"{1}\"",
        REDACTED_SECRETS[0], REDACTED_SECRETS[1], REDACTED_SECRETS[2],
    )
}

async fn seeded_database() -> (tempfile::TempDir, Database) {
    let directory = tempfile::tempdir().expect("temporary state directory");
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("eggpool.db")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO providers (provider_id, base_url, protocols) \
                 VALUES ('provider-a', 'https://provider-a.invalid/v1', '[\"openai\"]')",
                [],
            )?;
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, weight, provider_id) \
                 VALUES (?1, 'account-a', 'UNUSED', 1, 1.0, 'provider-a')",
                params![ACCOUNT_ID],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id) \
                 VALUES ('model-a', 'openai', 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO requests
                 (id, proxy_request_id, account_id, model_id, provider_id, protocol,
                  upstream_protocol, streamed, status, reserved_microdollars,
                  user_id, credential_id, auth_epoch, principal_kind, request_kind,
                  parent_proxy_request_id)
                 VALUES (?1, 'proxy-1', ?2, 'model-a', 'provider-a', 'openai',
                         'openai', 0, 'pending', 0, NULL, NULL, NULL,
                         'legacy_shared_key', 'logical', NULL)",
                params![REQUEST_ID, ACCOUNT_ID],
            )?;
            connection.execute(
                "INSERT INTO reservations
                 (id, request_id, account_id, model_id, reserved_microdollars, status)
                 VALUES (1, ?1, ?2, 'model-a', 0, 'active')",
                params![REQUEST_ID, ACCOUNT_ID],
            )?;
            connection.execute(
                "INSERT INTO request_attempts
                 (id, request_id, attempt_number, account_id, provider_id, model_id,
                  protocol, streamed)
                 VALUES (?1, ?2, 1, ?3, 'provider-a', 'model-a', 'openai', 0)",
                params![ATTEMPT_ID, REQUEST_ID, ACCOUNT_ID],
            )?;
            Ok(())
        })
        .await
        .expect("durable rows insert");
    (directory, database)
}

fn identity() -> FinalizationIdentity {
    FinalizationIdentity {
        proxy_request_id: "proxy-1".into(),
        db_request_id: REQUEST_ID,
        attempt_id: ATTEMPT_ID,
        reservation_id: 1,
        account_id: ACCOUNT_ID,
        account_name: "account-a".into(),
        provider_id: "provider-a".into(),
        model_id: "model-a".into(),
        upstream_model_id: "model-a".into(),
        client_protocol: "openai".into(),
        upstream_protocol: "openai".into(),
        attempt_number: 1,
        user_id: None,
        credential_id: None,
        auth_epoch: None,
        principal_kind: PrincipalKind::LegacySharedKey,
        request_kind: AttributedRequestKind::Logical,
        parent_proxy_request_id: None,
    }
}

/// The durable detail columns of the request row and its attempt row.
async fn persisted_details(database: &Database) -> (Option<String>, Option<String>) {
    database
        .call(|connection| {
            Ok((
                connection.query_row(
                    "SELECT error_detail FROM requests WHERE id = ?1",
                    params![REQUEST_ID],
                    |row| row.get::<_, Option<String>>(0),
                )?,
                connection.query_row(
                    "SELECT error_detail FROM request_attempts WHERE id = ?1",
                    params![ATTEMPT_ID],
                    |row| row.get::<_, Option<String>>(0),
                )?,
            ))
        })
        .await
        .expect("durable detail rows")
}

/// Persist `detail` through the terminal write path and return the stored
/// request detail, which is the redacted projection of the written value.
async fn persisted_request_detail(database: &Database, detail: &str) -> String {
    let finalizer = DurableFinalizer::new(database.clone());
    finalizer
        .finalize_request(
            &identity(),
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(502),
                error_class: Some("upstream_error".into()),
                error_detail: Some(detail.to_owned()),
                release_reason: Some("attempt_failed".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("finalization succeeds");
    let (request_detail, _attempt_detail) = persisted_details(database).await;
    request_detail.expect("request detail persisted")
}

/// Neither the removed secret nor a truncated prefix of it may survive.
fn assert_no_secret_material(persisted: &str, context: &str) {
    for secret in REDACTED_SECRETS {
        assert!(
            !persisted.contains(secret),
            "{context} still holds {secret}: {persisted}"
        );
        let prefix = &secret[..8];
        assert!(
            !persisted.contains(prefix),
            "{context} still holds the truncated prefix {prefix}: {persisted}"
        );
    }
}

#[tokio::test]
async fn terminal_detail_redaction_removes_credential_material_from_persisted_rows() {
    let (_directory, database) = seeded_database().await;
    let finalizer = DurableFinalizer::new(database.clone());
    finalizer
        .finalize_request(
            &identity(),
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(502),
                error_class: Some("upstream_error".into()),
                error_detail: Some(credential_bearing_detail()),
                release_reason: Some("attempt_failed".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("finalization succeeds");
    let (request_detail, attempt_detail) = persisted_details(&database).await;
    let request_detail = request_detail.expect("request detail persisted");
    let attempt_detail = attempt_detail.expect("attempt detail persisted");
    assert_no_secret_material(&request_detail, "requests.error_detail");
    assert_no_secret_material(&attempt_detail, "request_attempts.error_detail");
    assert!(
        request_detail.contains("[redacted]"),
        "the request detail was not redacted: {request_detail}"
    );
    assert!(
        attempt_detail.contains("[redacted]"),
        "the attempt detail was not redacted: {attempt_detail}"
    );
    // The operator-facing context around the removed material survives.
    assert!(
        request_detail.contains("upstream rejected the call"),
        "{request_detail}"
    );
    assert!(request_detail.contains("Authorization"), "{request_detail}");
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn terminal_detail_redaction_applies_to_the_failed_attempt_update_path() {
    let (_directory, database) = seeded_database().await;
    let finalizer = DurableFinalizer::new(database.clone());
    finalizer
        .finalize_failed_attempt(
            &identity(),
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(503),
                error_class: Some("temporary".into()),
                error_detail: Some(credential_bearing_detail()),
                release_reason: Some("retryable".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("failed attempt finalization succeeds");
    let (request_detail, attempt_detail) = persisted_details(&database).await;
    let attempt_detail = attempt_detail.expect("attempt detail persisted");
    assert_no_secret_material(&attempt_detail, "request_attempts.error_detail");
    assert!(
        attempt_detail.contains("[redacted]"),
        "the attempt detail was not redacted: {attempt_detail}"
    );
    // The failed-attempt path leaves the request row pending and unwritten,
    // so the attempt row is the only durable detail this path owns.
    assert!(
        request_detail.is_none(),
        "the request row was written by the failed-attempt path: {request_detail:?}"
    );
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn terminal_detail_sanitizer_preserves_ordinary_diagnostics() {
    let (_directory, database) = seeded_database().await;
    let finalizer = DurableFinalizer::new(database.clone());
    let detail = "upstream provider returned 503 for model gpt-4o-mini after 2 attempts: connection reset by peer while streaming the response";
    finalizer
        .finalize_request(
            &identity(),
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(503),
                error_class: Some("temporary".into()),
                error_detail: Some(detail.to_owned()),
                release_reason: Some("attempt_failed".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("finalization succeeds");
    let (request_detail, attempt_detail) = persisted_details(&database).await;
    let request_detail = request_detail.expect("request detail persisted");
    assert_eq!(
        request_detail, detail,
        "an ordinary diagnostic detail must survive the sanitizer intact"
    );
    assert_eq!(attempt_detail.as_deref(), Some(detail));
    database.close().await.expect("database closes");
}

/// The JSON spelling of a labeled field puts the closing quote of the key
/// between the label and the separator, so `"api_key":"…"` must be redacted
/// exactly like the unquoted `api_key: …` spelling. The credential is short
/// enough that no opaque-run rule can reach it, so only the labeled rule can
/// remove it.
#[tokio::test]
async fn terminal_detail_redaction_removes_a_json_spelled_labeled_credential() {
    let (_directory, database) = seeded_database().await;
    let secret = "abc123secret";
    let detail =
        format!(r#"upstream rejected the call: {{"api_key":"{secret}","model":"gpt-4o-mini"}}"#);
    let persisted = persisted_request_detail(&database, &detail).await;
    assert!(
        !persisted.contains(secret),
        "the JSON credential survived: {persisted}"
    );
    assert!(
        persisted.contains("[redacted]"),
        "the JSON credential was not redacted: {persisted}"
    );
    assert_eq!(
        persisted,
        "upstream rejected the call: {\"api_key\":\"[redacted]\",\"model\":\"gpt-4o-mini\"}",
        "the JSON credential was not redacted in place: {persisted}"
    );
    database.close().await.expect("database closes");
}

/// A credential scheme word that carries no field label must be redacted for
/// every scheme, not only for `Bearer`.
#[tokio::test]
async fn terminal_detail_redaction_removes_standalone_scheme_credentials() {
    let (_directory, database) = seeded_database().await;
    let detail = "upstream rejected the call: Basic dXNlcjpwYXNzd29yZA==; \
                  Token abc123def456ghi789; Digest abc123def456ghi789";
    let persisted = persisted_request_detail(&database, detail).await;
    for secret in ["dXNlcjpwYXNzd29yZA==", "abc123def456ghi789"] {
        assert!(
            !persisted.contains(secret),
            "the standalone credential {secret} survived: {persisted}"
        );
    }
    for scheme in ["Basic", "Token", "Digest"] {
        assert!(
            persisted.contains(&format!("{scheme} [redacted]")),
            "the standalone {scheme} credential was not redacted: {persisted}"
        );
    }
    assert!(
        persisted.starts_with("upstream rejected the call: Basic [redacted]"),
        "the diagnostic context around the credentials did not survive: {persisted}"
    );
    database.close().await.expect("database closes");
}

/// A labeled scheme field keeps its label, its separator, and its scheme word,
/// and loses only the credential.
#[tokio::test]
async fn terminal_detail_redaction_keeps_the_label_and_scheme_of_a_labeled_credential() {
    let (_directory, database) = seeded_database().await;
    let detail = "upstream rejected the call: authorization: Basic dXNlcjpwYXNzd29yZA==";
    let persisted = persisted_request_detail(&database, detail).await;
    assert!(
        !persisted.contains("dXNlcjpwYXNzd29yZA=="),
        "the labeled scheme credential survived: {persisted}"
    );
    assert_eq!(
        persisted, "upstream rejected the call: authorization: Basic [redacted]",
        "the labeled scheme credential was not redacted in place: {persisted}"
    );
    database.close().await.expect("database closes");
}

/// A URL authority that carries userinfo credentials loses the userinfo and
/// keeps the host, so the diagnostic still shows which upstream was contacted.
#[tokio::test]
async fn terminal_detail_redaction_removes_url_userinfo_credentials() {
    let (_directory, database) = seeded_database().await;
    let detail =
        "upstream rejected the call: https://user:sup3rSecret@host.example/v1 returned 502";
    let persisted = persisted_request_detail(&database, detail).await;
    assert!(
        !persisted.contains("sup3rSecret"),
        "the URL userinfo credential survived: {persisted}"
    );
    assert!(
        !persisted.contains("user:"),
        "the URL userinfo section survived: {persisted}"
    );
    assert!(
        persisted.contains("https://[redacted]@host.example"),
        "the URL userinfo credential was not redacted: {persisted}"
    );
    assert!(
        persisted.contains("returned 502"),
        "the diagnostic context around the URL did not survive: {persisted}"
    );
    database.close().await.expect("database closes");
}

/// A vendor token prefix is matched case-insensitively, like a field label.
#[tokio::test]
async fn terminal_detail_redaction_removes_uppercase_vendor_token_prefixes() {
    let (_directory, database) = seeded_database().await;
    let detail = "upstream rejected the call: SK-proj-AAAABBBBCCCCDDDDEEEEFFFF";
    let persisted = persisted_request_detail(&database, detail).await;
    assert!(
        !persisted.contains("SK-proj-AAAABBBBCCCCDDDDEEEEFFFF"),
        "the upper-case vendor token survived: {persisted}"
    );
    assert!(
        !persisted.contains("SK-proj-"),
        "a truncated prefix of the upper-case vendor token survived: {persisted}"
    );
    assert!(
        persisted.contains("[redacted]"),
        "the upper-case vendor token was not redacted: {persisted}"
    );
    database.close().await.expect("database closes");
}

/// An ordinary URL without userinfo, a `mailto:`-style fragment, and a bare
/// `word@word` fragment are not URL authorities and must survive untouched.
#[tokio::test]
async fn terminal_detail_redaction_preserves_fragments_without_userinfo() {
    let (_directory, database) = seeded_database().await;
    let detail = "upstream https://provider-a.invalid/v1 failed; \
                  contact ops@example.com or mailto:user@example.com";
    let persisted = persisted_request_detail(&database, detail).await;
    assert_eq!(
        persisted, detail,
        "a fragment without URL userinfo must survive the sanitizer intact"
    );
    database.close().await.expect("database closes");
}

/// A compound identifier that merely contains a credential label is not a
/// labeled credential field.
#[tokio::test]
async fn terminal_detail_redaction_preserves_compound_identifiers() {
    let (_directory, database) = seeded_database().await;
    let detail = "request summary: input_tokens 128, token_count 4, api_key_id key-7";
    let persisted = persisted_request_detail(&database, detail).await;
    assert_eq!(
        persisted, detail,
        "a compound identifier must survive the sanitizer intact"
    );
    database.close().await.expect("database closes");
}
