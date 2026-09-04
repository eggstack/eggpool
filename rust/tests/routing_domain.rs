use std::{
    fs,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

use eggpool::{
    Config,
    accounts::{AccountIdentity, AccountRegistry, CredentialStore, QuotaOffsets, RequestSurface},
    catalog::{CatalogCacheError, ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    db::{Account, AccountRepository, Database, DatabaseConfig, MigrationRunner},
};

fn temporary_database_path(label: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "eggpool-d002-{label}-{}-{nanos}.sqlite3",
        std::process::id()
    ))
}

async fn database(label: &str) -> (Database, PathBuf) {
    let path = temporary_database_path(label);
    let database = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    (database, path)
}

fn durable_account(id: i64, name: &str, provider_id: &str, enabled: bool) -> Account {
    Account {
        id,
        name: name.into(),
        api_key_env: format!("{name}_KEY"),
        enabled,
        weight: 1.0,
        provider_id: provider_id.into(),
    }
}

fn config() -> Config {
    let mut config = Config::default();
    let mut provider = eggpool::config::ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider-a.invalid/v1".into(),
        protocols: vec!["openai".into(), "anthropic".into()],
        auth: eggpool::config::ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    provider.accounts.push(eggpool::config::AccountConfig {
        name: "account-a".into(),
        api_key_env: "ACCOUNT_A_KEY".into(),
        weight: 2.0,
        weekly_offset_microdollars: -11,
        ..Default::default()
    });
    provider.accounts.push(eggpool::config::AccountConfig {
        name: "disabled-a".into(),
        api_key_env: "DISABLED_KEY".into(),
        enabled: false,
        ..Default::default()
    });
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("test config validates");
    config
}

#[test]
fn account_identity_is_non_secret_and_surface_aware() {
    let config = config();
    let mut credentials = CredentialStore::default();
    credentials.insert("account-a", "secret-that-must-not-leak");
    let rows = [
        durable_account(1, "account-a", "provider-a", true),
        durable_account(2, "disabled-a", "provider-a", false),
    ];
    let registry =
        AccountRegistry::from_config(&config, &rows, &credentials).expect("registry builds");
    let identity = registry.get("account-a").expect("identity exists");
    assert_eq!(identity.routing_priority, 0);
    assert_eq!(identity.weight, 2.0);
    assert_eq!(
        identity.quota_offsets,
        QuotaOffsets {
            five_hour: 0,
            weekly: -11,
            monthly: 0
        }
    );
    assert!(registry.supports_protocol("account-a", "anthropic"));
    assert!(registry.supports_request_surface("account-a", RequestSurface::ChatCompletions));
    assert!(registry.supports_request_surface("account-a", RequestSurface::Responses));
    assert_eq!(registry.enabled_snapshot().len(), 1);
    let debug = format!("{identity:?}");
    assert!(!debug.contains("secret-that-must-not-leak"));
    let json = serde_json::to_string(identity).expect("identity serializes");
    assert!(!json.contains("api_key"));
}

#[test]
fn d001_account_observation_is_represented_by_the_rust_registry() {
    let mut config = config();
    config.providers.insert(
        "provider-b".into(),
        eggpool::config::ProviderConfig {
            id: "provider-b".into(),
            base_url: "https://provider-b.invalid/v1".into(),
            protocols: vec!["openai".into()],
            auth: eggpool::config::ProviderAuthConfig {
                mode: "none".into(),
                ..Default::default()
            },
            accounts: vec![eggpool::config::AccountConfig {
                name: "account-b".into(),
                ..Default::default()
            }],
            ..Default::default()
        },
    );
    config.validate().expect("D001-shaped config validates");
    let rows = [
        durable_account(1, "account-a", "provider-a", true),
        durable_account(2, "account-b", "provider-b", true),
        durable_account(3, "disabled-a", "provider-a", false),
    ];
    let registry = AccountRegistry::from_config(&config, &rows, &CredentialStore::default())
        .expect("D001 registry builds");
    assert_eq!(registry.enabled_snapshot().len(), 2);
    assert_eq!(
        registry.provider_for_account("account-a"),
        Some("provider-a")
    );
    assert_eq!(
        registry.provider_for_account("account-b"),
        Some("provider-b")
    );
    assert!(registry.supports_protocol("account-a", "anthropic"));
    assert!(!registry.supports_protocol("account-b", "anthropic"));
    assert!(registry.supports_request_surface("account-b", RequestSurface::Responses));
}

#[test]
fn account_registry_fails_on_missing_durable_relationship_and_credentials() {
    let mut config = config();
    let error = AccountRegistry::from_config(
        &config,
        &[durable_account(1, "account-a", "wrong", true)],
        &CredentialStore::default(),
    )
    .expect_err("invalid registry");
    assert!(error.to_string().contains("belongs to provider"));
    config
        .providers
        .get_mut("provider-a")
        .expect("provider")
        .auth
        .mode = "bearer".into();
    let rows = [
        durable_account(1, "account-a", "provider-a", true),
        durable_account(2, "disabled-a", "provider-a", false),
    ];
    let error = AccountRegistry::from_config(&config, &rows, &CredentialStore::default())
        .expect_err("credential is required");
    assert!(error.to_string().contains("no usable credentials"));
}

#[test]
fn provider_qualifier_requires_a_known_provider() {
    let known = ["opencode-go".to_owned()].into_iter().collect();
    assert_eq!(
        ModelCatalogCache::parse_model_provider("model/opencode-go", &known),
        ("model".into(), Some("opencode-go".into()))
    );
    assert_eq!(
        ModelCatalogCache::parse_model_provider("model/not-known", &known),
        ("model/not-known".into(), None)
    );
    assert_eq!(
        ModelCatalogCache::parse_model_provider("/model", &known),
        ("/model".into(), None)
    );
    assert_eq!(
        ModelCatalogCache::parse_model_provider("model/", &known),
        ("model/".into(), None)
    );
}

#[test]
fn account_weight_must_be_positive_and_finite() {
    let mut config = config();
    config
        .providers
        .get_mut("provider-a")
        .expect("provider")
        .accounts[0]
        .weight = f64::NAN;
    assert!(
        config
            .validate()
            .expect_err("NaN weight is invalid")
            .to_string()
            .contains("non-finite")
    );
}

#[test]
fn cache_updates_are_non_destructive_until_authorized_withdrawal() {
    let mut cache = ModelCatalogCache::default();
    cache.set_account_provider("account-a", "provider-a");
    cache.set_account_provider("account-b", "provider-a");
    let mut model = ModelInput::new("shared");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("static_config".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    model.capabilities.supports_tools = Some(true);
    model.limits.context_tokens = Some(8192);
    let first = cache
        .update_from_account("account-a", "provider-a", &[model.clone()], true, true)
        .expect("first update");
    assert_eq!(first.added_support, 1);
    cache
        .update_from_account("account-b", "provider-a", &[model.clone()], true, true)
        .expect("sibling update");
    let uncertain = cache
        .update_from_account("account-a", "provider-a", &[], false, false)
        .expect("uncertain update");
    assert_eq!(uncertain.preserved_support, 1);
    assert!(cache.account_supports_model("account-a", "shared"));
    let withdrawn = cache
        .update_from_account("account-a", "provider-a", &[], true, true)
        .expect("authorized withdrawal");
    assert_eq!(withdrawn.withdrawn_support, 1);
    assert!(!cache.account_supports_model("account-a", "shared"));
    assert!(cache.account_supports_model("account-b", "shared"));
    assert!(cache.get_provider_model("shared", "provider-a").is_some());
    assert_eq!(
        cache
            .get_effective_limits("shared", None)
            .expect("conservative limit")
            .context_tokens,
        Some(8192)
    );
}

#[test]
fn static_protocol_and_capability_facts_survive_weaker_live_metadata() {
    let mut cache = ModelCatalogCache::default();
    cache.set_account_provider("account-a", "provider-a");
    let mut static_model = ModelInput::new("static-model");
    static_model.protocol = Some("anthropic".into());
    static_model.protocol_source = Some("static_config".into());
    static_model.resolution_status = ProtocolResolutionStatus::Resolved;
    static_model.capabilities.supports_tools = Some(true);
    cache
        .update_from_account("account-a", "provider-a", &[static_model], false, false)
        .expect("static update");
    let mut weaker_live = ModelInput::new("static-model");
    weaker_live.resolution_status = ProtocolResolutionStatus::Unresolved;
    cache
        .update_from_account("account-a", "provider-a", &[weaker_live], false, false)
        .expect("live update");
    let row = cache
        .get_provider_model("static-model", "provider-a")
        .expect("provider row");
    assert_eq!(row.protocol.as_deref(), Some("anthropic"));
    assert_eq!(row.capabilities.supports_tools, Some(true));
}

#[test]
fn provider_and_global_limit_overrides_use_per_field_precedence() {
    let mut config = config();
    config.model_overrides.insert(
        "shared".into(),
        eggpool::config::ModelOverrideConfig {
            max_input_tokens: Some(100),
            ..Default::default()
        },
    );
    config
        .providers
        .get_mut("provider-a")
        .expect("provider")
        .model_overrides
        .insert(
            "shared".into(),
            eggpool::config::ModelOverrideConfig {
                max_context_tokens: Some(4096),
                ..Default::default()
            },
        );
    let mut cache = ModelCatalogCache::default();
    cache.set_config(&config);
    cache.set_account_provider("account-a", "provider-a");
    let mut model = ModelInput::new("shared");
    model.protocol = Some("openai".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    model.limits.context_tokens = Some(8192);
    model.limits.input_tokens = Some(2048);
    cache
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("model update");
    let limits = cache
        .get_effective_limits("shared", Some("provider-a"))
        .expect("provider limits");
    assert_eq!(limits.context_tokens, Some(4096));
    assert_eq!(limits.input_tokens, Some(100));
    assert_eq!(limits.input_source, "global_override");
}

#[tokio::test(flavor = "current_thread")]
async fn schema54_seed_hydrates_catalog_and_durable_freshness() {
    let (database, path) = database("seed").await;
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../migration-rs/fixtures/routing-domain/schema54-routing-domain-seed.sql"
            ))
        })
        .await
        .expect("seed applies");
    let mut cache = ModelCatalogCache::default();
    let loaded = cache
        .hydrate_from_db(&database)
        .await
        .expect("cache hydrates");
    assert_eq!(loaded, 2);
    assert_eq!(
        cache.exposed_model_ids(),
        vec!["shared-model".to_owned(), "withdrawn-model".to_owned()]
    );
    assert_eq!(
        cache.supporting_accounts("shared-model"),
        vec!["account-a", "account-b"]
    );
    assert_eq!(cache.provider_for_account("account-a"), Some("provider-a"));
    assert_eq!(
        cache
            .freshness("account-a")
            .expect("durable freshness")
            .source,
        "durable"
    );
    assert!(cache.account_model_is_fresh("account-a", 1, 1_000_000_000));
    assert!(!cache.account_model_is_fresh("account-a", 1, 2_000_000_000));
    assert!(
        cache
            .get_provider_capabilities("shared-model", "provider-a")
            .expect("provider row")
            .supports_tools
            == Some(true)
    );
    let snapshot = cache.snapshot();
    assert_eq!(snapshot.model_ids.len(), 2);
    database.close().await.expect("database closes");
    fs::remove_file(path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn missing_durable_refresh_rows_use_legacy_model_timestamp_fallback() {
    let (database, path) = database("legacy-freshness").await;
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../migration-rs/fixtures/routing-domain/schema54-routing-domain-seed.sql"
            ))?;
            connection.execute("DELETE FROM catalog_refresh_state", [])
        })
        .await
        .expect("seed applies");
    let mut cache = ModelCatalogCache::default();
    cache
        .hydrate_from_db(&database)
        .await
        .expect("legacy state hydrates");
    assert_eq!(
        cache
            .freshness("account-a")
            .expect("legacy freshness")
            .source,
        "legacy_model_timestamp"
    );
    database.close().await.expect("database closes");
    fs::remove_file(path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn advisory_json_isolated_but_invalid_protocol_fails_closed() {
    let (database, path) = database("corrupt").await;
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../migration-rs/fixtures/routing-domain/schema54-routing-domain-seed.sql"
            ))
        })
        .await
        .expect("seed applies");
    database
        .call(|connection| {
            connection.execute(
                "UPDATE models SET capabilities = ?1 WHERE model_id = 'shared-model'",
                ["not-json"],
            )
        })
        .await
        .expect("advisory corruption writes");
    let mut cache = ModelCatalogCache::default();
    cache
        .hydrate_from_db(&database)
        .await
        .expect("advisory corruption is isolated");
    database
        .call(|connection| {
            connection.execute(
                "UPDATE models SET protocol = ?1 WHERE model_id = 'shared-model'",
                ["made-up"],
            )
        })
        .await
        .expect("mandatory corruption writes");
    let error = ModelCatalogCache::default()
        .hydrate_from_db(&database)
        .await
        .expect_err("invalid protocol is rejected");
    assert!(matches!(error, CatalogCacheError::InvalidProtocol(_)));
    database.close().await.expect("database closes");
    fs::remove_file(path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn account_repository_returns_stable_ids_and_config_sync_keeps_schema_54() {
    let (database, path) = database("accounts").await;
    let repository = AccountRepository::new(&database);
    let ids = repository
        .sync_from_config(vec![eggpool::db::AccountConfig::new("one", "ONE_KEY")])
        .await
        .expect("sync works");
    assert_eq!(
        ids[0].1,
        repository.list_all().await.expect("list works")[0].id
    );
    assert_eq!(
        repository.list_enabled().await.expect("enabled list").len(),
        1
    );
    database.close().await.expect("database closes");
    fs::remove_file(path).expect("temporary database removed");
}

#[allow(dead_code)]
fn _identity_type_is_explicitly_non_secret(_: AccountIdentity) {}
