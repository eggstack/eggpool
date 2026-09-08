//! R005 config classification, semantic diff, digest, and redaction tests.

use std::{collections::BTreeMap, fs};

use eggpool::{
    Config,
    config::{AccountConfig, ProviderConfig},
    config_reload_policy::{
        ConfigDiff, ReloadDisposition, compute_diff, disposition_for, dynamic_rules,
        field_dispositions, sanitize_text_for_audit, schema_paths, semantic_digest,
        verify_expected_digest,
    },
};
use serde_json::{Value, json};
use tempfile::tempdir;

const ORACLE: &str =
    include_str!("../../migration-rs/fixtures/runtime-lifecycle/r001-python-observations.json");
const SERVER_SECRET: &str = "ep-r005-server-secret-sentinel";
const ACCOUNT_SECRET: &str = "sk-r005-account-secret-sentinel";
const PROXY_SECRET: &str = "proxy-r005-secret-sentinel";

fn oracle() -> Value {
    serde_json::from_str(ORACLE).expect("R001 fixture is valid JSON")
}

fn oracle_policy_rows() -> Vec<(String, String)> {
    oracle()["config_policy"]["field_dispositions"]
        .as_array()
        .expect("field dispositions")
        .iter()
        .map(|row| {
            (
                row["path"].as_str().expect("path").to_owned(),
                row["disposition"].as_str().expect("disposition").to_owned(),
            )
        })
        .collect()
}

fn diff(old: &Config, new: &Config) -> ConfigDiff {
    compute_diff(old, new).expect("validated config projection")
}

fn provider(id: &str, account: Option<AccountConfig>) -> ProviderConfig {
    ProviderConfig {
        id: id.to_owned(),
        base_url: format!("https://{id}.example.test"),
        accounts: account.into_iter().collect(),
        ..ProviderConfig::default()
    }
}

fn account(name: &str, api_key: &str) -> AccountConfig {
    AccountConfig {
        name: name.to_owned(),
        api_key: Some(api_key.to_owned()),
        ..AccountConfig::default()
    }
}

fn diff_projection(diff: &ConfigDiff) -> Value {
    json!({
        "paths": diff.changes.iter().map(|change| &change.path).collect::<Vec<_>>(),
        "dispositions": diff.changes.iter().map(|change| change.disposition).collect::<Vec<_>>(),
        "changes": diff.changes,
        "live": diff.live().iter().map(|change| &change.path).collect::<Vec<_>>(),
        "restart_required": diff.restart_required().iter().map(|change| &change.path).collect::<Vec<_>>(),
        "sections": diff.changed_sections(),
    })
}

fn assert_fixture_diff(name: &str, old: &Config, new: &Config) {
    let expected = oracle()["reload"]["diff_observations"][name].clone();
    assert_eq!(
        diff_projection(&diff(old, new)),
        expected,
        "fixture case {name}"
    );
}

#[test]
fn exact_r001_field_dispositions_are_ported() {
    let expected = oracle_policy_rows();
    let actual = field_dispositions()
        .iter()
        .map(|(path, disposition)| ((*path).to_owned(), disposition.to_string()))
        .collect::<Vec<_>>();
    assert_eq!(actual, expected);
    assert_eq!(actual.len(), 153);
    assert_eq!(
        actual
            .iter()
            .filter(|(_, disposition)| disposition == "live")
            .count(),
        59
    );
    assert_eq!(
        actual
            .iter()
            .filter(|(_, disposition)| disposition == "restart_required")
            .count(),
        94
    );
}

#[test]
fn serialized_schema_projection_is_the_coverage_guard() {
    let expected = oracle_policy_rows()
        .into_iter()
        .map(|(path, _)| path)
        .collect::<Vec<_>>();
    assert_eq!(schema_paths().expect("schema projection"), expected);
}

#[test]
fn unknown_paths_and_dynamic_rules_fail_closed_or_match_oracle() {
    assert_eq!(
        disposition_for("unknown.future.field"),
        ReloadDisposition::RestartRequired
    );
    assert_eq!(
        disposition_for("providers.new-provider.secret"),
        ReloadDisposition::Live
    );
    assert_eq!(
        disposition_for("accounts.new-provider/new-account.api_key"),
        ReloadDisposition::Live
    );
    assert_eq!(
        disposition_for("models.future_field"),
        ReloadDisposition::RestartRequired
    );
    assert_eq!(dynamic_rules()[6].1, ReloadDisposition::Live);
}

#[test]
fn live_restart_and_mixed_mutations_are_classified_without_partial_semantics() {
    let old = Config::default();

    let mut live_new = old.clone();
    live_new.server.max_request_body_bytes += 1;
    let live = diff(&old, &live_new);
    assert_eq!(
        live.changes
            .iter()
            .map(|change| change.path.as_str())
            .collect::<Vec<_>>(),
        vec!["server.max_request_body_bytes"]
    );
    assert_eq!(live.live().len(), 1);
    assert!(live.restart_required().is_empty());

    let mut restart_new = old.clone();
    restart_new.server.port += 1;
    let restart = diff(&old, &restart_new);
    assert_eq!(restart.restart_required().len(), 1);
    assert!(restart.live().is_empty());

    let mut mixed_new = old;
    mixed_new.server.max_request_body_bytes += 1;
    mixed_new.server.port += 1;
    let mixed = diff(&Config::default(), &mixed_new);
    assert!(mixed.has_restart_required());
    assert_eq!(mixed.live().len(), 1);
    assert_eq!(mixed.restart_required().len(), 1);
    assert_eq!(mixed.changed_sections(), vec!["server"]);
}

#[test]
fn diff_projections_match_the_frozen_r001_cases() {
    let old = Config::default();
    assert_fixture_diff("identical", &old, &old);

    let mut live = old.clone();
    live.server.max_request_body_bytes += 1;
    assert_fixture_diff("live_only", &old, &live);

    let mut restart = old.clone();
    restart.server.port += 1;
    assert_fixture_diff("restart_only", &old, &restart);

    let mut mixed = old.clone();
    mixed.server.max_request_body_bytes += 1;
    mixed.server.port += 1;
    assert_fixture_diff("mixed", &old, &mixed);

    let mut secret = old.clone();
    secret.pricing.catalogs.openrouter.api_key = Some("sk-r005-fixture-secret".to_owned());
    assert_fixture_diff("secret", &old, &secret);
}

#[test]
fn provider_account_and_router_paths_are_stable() {
    let old = Config::default();
    let mut added = old.clone();
    added
        .providers
        .insert("z-provider".to_owned(), provider("z-provider", None));
    let provider_add = diff(&old, &added);
    assert_eq!(
        provider_add
            .changes
            .iter()
            .map(|change| change.path.as_str())
            .collect::<Vec<_>>(),
        vec!["providers.z-provider"]
    );

    let mut with_account = added.clone();
    with_account.providers.insert(
        "a-provider".to_owned(),
        provider("a-provider", Some(account("default", ACCOUNT_SECRET))),
    );
    let account_add = diff(&added, &with_account);
    assert_eq!(
        account_add
            .changes
            .iter()
            .map(|change| change.path.as_str())
            .collect::<Vec<_>>(),
        vec!["accounts.a-provider/default", "providers.a-provider"]
    );

    let routers_a = Config {
        model_routers: BTreeMap::from([
            ("z".to_owned(), Default::default()),
            ("a".to_owned(), Default::default()),
        ]),
        ..Config::default()
    };
    let mut routers_b = routers_a.clone();
    routers_b.model_routers.remove("a");
    let router_diff = diff(&routers_a, &routers_b);
    assert_eq!(router_diff.changes[0].path, "model_routers");
    assert_eq!(router_diff.changes[0].old_display, "2 configured routers");
    assert_eq!(router_diff.changes[0].new_display, "1 configured router");
}

#[test]
fn secret_values_are_absent_from_all_change_projections() {
    let mut old = Config::default();
    old.server.api_key = Some(SERVER_SECRET.to_owned());
    old.pricing.catalogs.openrouter.api_key = Some(PROXY_SECRET.to_owned());
    old.proxies.insert(
        "corp".to_owned(),
        eggpool::config::ProxyConfig {
            url: Some(format!("http://user:{PROXY_SECRET}@proxy.example.test")),
            url_env: None,
        },
    );
    old.providers.insert(
        "provider".to_owned(),
        provider("provider", Some(account("default", ACCOUNT_SECRET))),
    );
    let mut new = old.clone();
    new.server.api_key = Some("ep-r005-server-rotated-sentinel".to_owned());
    new.pricing.catalogs.openrouter.api_key = Some("pricing-r005-rotated-sentinel".to_owned());
    new.providers
        .get_mut("provider")
        .expect("provider")
        .accounts[0]
        .api_key = Some("sk-r005-account-rotated-sentinel".to_owned());

    let changes = diff(&old, &new);
    let debug = format!("{changes:?}");
    let json = serde_json::to_string(&changes.changes).expect("change JSON");
    let diff_json = serde_json::to_string(&changes).expect("diff JSON");
    let display = changes
        .changes
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join("; ");
    for sentinel in [
        SERVER_SECRET,
        ACCOUNT_SECRET,
        PROXY_SECRET,
        "ep-r005-server-rotated-sentinel",
        "pricing-r005-rotated-sentinel",
        "sk-r005-account-rotated-sentinel",
    ] {
        assert!(!debug.contains(sentinel), "debug leaked {sentinel}");
        assert!(!json.contains(sentinel), "json leaked {sentinel}");
        assert!(!diff_json.contains(sentinel), "diff JSON leaked {sentinel}");
        assert!(!display.contains(sentinel), "display leaked {sentinel}");
    }
    assert!(diff_json.contains("\"live\""));
    assert!(diff_json.contains("\"restart_required\""));
    let account_change = changes
        .changes
        .iter()
        .find(|change| change.path == "accounts.provider/default.api_key")
        .expect("account secret change");
    assert!(account_change.secret);
    assert_eq!(account_change.old_display, "<changed>");
    assert_eq!(account_change.new_display, "<changed>");
}

#[test]
fn semantic_digest_and_noop_are_formatting_independent() {
    let old = Config::default();
    let new = old.clone();
    assert!(diff(&old, &new).is_noop());
    let digest = semantic_digest(&old).expect("semantic digest");
    assert_eq!(semantic_digest(&new).expect("semantic digest"), digest);
    verify_expected_digest(Some(&digest), &digest).expect("digest matches");
    let error = verify_expected_digest(Some("stale-digest"), &digest).expect_err("stale digest");
    assert_eq!(error.expected, "stale-digest");
    assert_eq!(error.actual, digest);

    let directory = tempdir().expect("temporary config directory");
    let first_path = directory.path().join("first.toml");
    let second_path = directory.path().join("second.toml");
    fs::write(
        &first_path,
        "# formatting and comments are not semantic\n[server]\nport=11300\n",
    )
    .expect("first config");
    fs::write(&second_path, "[server]\nport = 11300\n").expect("second config");
    let first = Config::from_toml(&first_path).expect("first config validates");
    let second = Config::from_toml(&second_path).expect("second config validates");
    assert_eq!(
        semantic_digest(&first).expect("first semantic digest"),
        semantic_digest(&second).expect("second semantic digest")
    );
    assert!(diff(&first, &second).is_noop());
}

#[test]
fn free_text_redaction_matches_r001_probe_and_proxy_uri_credentials() {
    assert_eq!(
        sanitize_text_for_audit("Authorization: Bearer XXXXXXXXXXXXXXXXXXXX", "test"),
        "Authorization: <redacted>"
    );
    assert_eq!(
        sanitize_text_for_audit("http://user:secret@proxy.example.test", "proxies.corp"),
        "http://<redacted>@proxy.example.test"
    );
}
