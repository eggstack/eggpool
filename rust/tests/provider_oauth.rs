use eggpool::config::{AccountAuthMode, OAuthConfig, SecretString};

#[test]
fn oauth_is_enabled_by_default_without_changing_static_key_accounts() {
    assert!(OAuthConfig::default().enabled);
    assert_eq!(AccountAuthMode::default(), AccountAuthMode::StaticKey);
}

#[test]
fn secret_string_debug_is_redacted() {
    let secret = "oauth-access-test-secret";
    let wrapped = SecretString::from(secret.to_owned());
    assert!(!format!("{wrapped:?}").contains(secret));
}
