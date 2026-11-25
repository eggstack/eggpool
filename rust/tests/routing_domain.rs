use std::{
    collections::BTreeSet,
    fs,
    net::TcpListener,
    path::PathBuf,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

// This shared fixture carries ticket variants unused by this routing-only target.
#[allow(dead_code)]
#[path = "common/agent_ticket.rs"]
mod agent_ticket;

use agent_ticket::{SERVER_NAME, TestTicketProducer};
use eggpool::{
    Config,
    accounts::{AccountIdentity, AccountRegistry, CredentialStore, QuotaOffsets, RequestSurface},
    catalog::{CatalogCacheError, ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    db::{Account, AccountRepository, Database, DatabaseConfig, MigrationRunner},
    health::{BackoffReason, HealthManager, get_backoff_policy},
    quota::{AccountQuota, QuotaEstimator},
    routing::{
        EligibilityPolicy, FairnessMode, FairnessScope, LocalQuotaMode, RoutingRequestFacts,
        RoutingRouter,
    },
};
use eggress_protocol_reverse::{
    client::{ReverseClient, ReverseClientConfig},
    tls::ReverseClientTlsConfig,
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
                "../../tests/fixtures/database/schema54-routing-domain-seed.sql"
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
                "../../tests/fixtures/database/schema54-routing-domain-seed.sql"
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
    // Never-refreshed accounts stay stale: per-account freshness is never
    // synthesized from other accounts or model timestamps, so the stale
    // gate can exclude them.
    assert!(
        cache.freshness("account-a").is_none(),
        "legacy timestamps must not synthesize freshness"
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
                "../../tests/fixtures/database/schema54-routing-domain-seed.sql"
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

/// A router with one credentialed Agent account, used to verify the process
/// gate and owner-binding rules without starting listeners.
fn agent_router(agents_enabled: bool, include_shared: bool) -> (RoutingRouter, HealthManager) {
    let agent_account = eggpool::config::AccountConfig {
        name: "agent-a".into(),
        auth_mode: eggpool::config::AccountAuthMode::Agent,
        api_key: Some("agent-upstream-key".into()),
        base_url: Some("http://127.0.0.1:19002/v1".into()),
        agent: Some(eggpool::config::AgentAccountConfig {
            control_bind: "127.0.0.1:19001".into(),
            external_bind: "127.0.0.1:19002".into(),
            transport_secret_env: "AGENT_TRANSPORT_SECRET".into(),
            ..Default::default()
        }),
        ..Default::default()
    };
    let shared_account = eggpool::config::AccountConfig {
        name: "shared-a".into(),
        api_key: Some("shared-static-key".into()),
        api_key_env: "SHARED_A_KEY".into(),
        ..Default::default()
    };
    let mut config = Config::default();
    config.agents.enabled = agents_enabled;
    config.providers.insert(
        "agent-provider".into(),
        eggpool::config::ProviderConfig {
            id: "agent-provider".into(),
            base_url: "https://provider.example/v1".into(),
            protocols: vec!["openai".into()],
            auth: eggpool::config::ProviderAuthConfig {
                mode: "none".into(),
                ..Default::default()
            },
            accounts: if include_shared {
                vec![agent_account, shared_account]
            } else {
                vec![agent_account]
            },
            ..Default::default()
        },
    );
    let mut credentials = CredentialStore::default();
    credentials.insert("agent-a", "agent-upstream-key");
    if include_shared {
        credentials.insert("shared-a", "shared-static-key");
    }
    let mut durable = vec![durable_account(1, "agent-a", "agent-provider", true)];
    if include_shared {
        durable.push(durable_account(2, "shared-a", "agent-provider", true));
    }
    let registry = AccountRegistry::from_config(&config, &durable, &credentials)
        .expect("Agent registry builds");
    let mut catalog = ModelCatalogCache::default();
    let mut account_names = vec!["agent-a"];
    if include_shared {
        account_names.push("shared-a");
    }
    for account_name in account_names {
        catalog.set_account_provider(account_name, "agent-provider");
        let mut model = ModelInput::new("model-a");
        model.protocol = Some("openai".into());
        model.protocol_source = Some("fixture".into());
        model.resolution_status = ProtocolResolutionStatus::Resolved;
        catalog
            .update_from_account(account_name, "agent-provider", &[model], true, true)
            .expect("account catalog model");
    }
    let quotas = if include_shared {
        vec![AccountQuota::new("agent-a"), AccountQuota::new("shared-a")]
    } else {
        vec![AccountQuota::new("agent-a")]
    };
    let estimator = QuotaEstimator::new(quotas);
    let health = HealthManager::new();
    health.register_account(1, "agent-a");
    if include_shared {
        health.register_account(2, "shared-a");
    }
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator,
        Some(health.clone()),
        EligibilityPolicy {
            fairness_mode: FairnessMode::RoundRobin,
            fairness_scope: FairnessScope::ProviderModelProtocol,
            local_quota_mode: LocalQuotaMode::ScoreOnly,
            ..Default::default()
        },
    );
    (router, health)
}

/// A router whose only health state is the health manager the live failure
/// path updates.
fn health_router() -> (RoutingRouter, HealthManager) {
    let registry = AccountRegistry::from_config(
        &config(),
        &[
            durable_account(1, "account-a", "provider-a", true),
            durable_account(2, "disabled-a", "provider-a", false),
        ],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("account-a", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("catalog account");
    let estimator = QuotaEstimator::new([AccountQuota::new("account-a")]);
    // The health gate treats an account with no manager as not healthy, so the
    // fixture registers every account the router can select.
    let health = HealthManager::new();
    for identity in registry.all() {
        health.register_account(identity.account_id, &identity.account_name);
    }
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator,
        Some(health.clone()),
        EligibilityPolicy {
            fairness_mode: FairnessMode::RoundRobin,
            fairness_scope: FairnessScope::ProviderModelProtocol,
            local_quota_mode: LocalQuotaMode::ScoreOnly,
            ..Default::default()
        },
    );
    (router, health)
}

fn routing_facts(model: &str) -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new(model);
    facts.requested_protocol = Some("openai".into());
    facts.projected_tokens = 128;
    facts.now = 1_700_000_000;
    facts
}

#[tokio::test(flavor = "current_thread")]
async fn a_rate_limited_failure_still_backs_the_account_off() {
    let (router, health) = health_router();
    let claim = router
        .select_and_claim(&routing_facts("model-a"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");
    // The rate limit the live classifier produces, with the provider's own
    // retry-after hint.
    router
        .apply_failure_effects(
            &claim,
            true,
            false,
            "none",
            Some("rate_limited"),
            Some(Duration::from_secs(30)),
            false,
        )
        .await;
    let snapshot = health
        .snapshot(claim.account_name())
        .expect("the account has health state");
    assert!(
        snapshot.cooldown_until > health.now(),
        "a rate-limited failure must still back the account off"
    );
    assert_eq!(snapshot.health_state, "rate_limited");
    assert_eq!(
        snapshot.last_failure_category,
        Some(BackoffReason::RateLimited)
    );
}

#[tokio::test(flavor = "current_thread")]
async fn an_authentication_failure_still_produces_no_timed_backoff() {
    let (router, health) = health_router();
    let claim = router
        .select_and_claim(&routing_facts("model-a"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");
    router
        .apply_failure_effects(
            &claim,
            true,
            false,
            "none",
            Some("authentication_failed"),
            None,
            true,
        )
        .await;
    let snapshot = health
        .snapshot(claim.account_name())
        .expect("the account has health state");
    assert_eq!(
        snapshot.cooldown_until, 0.0,
        "an authentication failure must still produce no timed backoff"
    );
    assert_eq!(snapshot.health_state, "authentication_failed");
    assert_eq!(
        snapshot.last_failure_category,
        Some(BackoffReason::AuthenticationFailed)
    );
    let policy =
        get_backoff_policy(BackoffReason::AuthenticationFailed).expect("the reason has a policy");
    assert_eq!(policy.base_delay, 0.0);
    assert_eq!(policy.cap, 0.0);
}

async fn wait_for_agent_control_count(tunnels: &eggpool::agents::AgentTunnelSet, expected: u32) {
    let reached = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let snapshot = tunnels.snapshot();
            if snapshot.tunnels.len() == 1 && snapshot.tunnels[0].active_control == expected {
                return;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .is_ok();
    let snapshot = tunnels.snapshot();
    assert!(
        reached,
        "reverse control count did not reach {expected}; snapshot={snapshot:?}"
    );
}

async fn assert_account_claimed(
    router: &RoutingRouter,
    facts: &RoutingRequestFacts,
    account_name: &str,
) {
    let sibling = if account_name == "agent-a" {
        "shared-a"
    } else {
        "agent-a"
    };
    let excluded = [sibling.to_owned()].into_iter().collect();
    let claim = router
        .select_and_claim(facts, &excluded)
        .await
        .expect("account selection succeeds")
        .unwrap_or_else(|| {
            let plan = router.build_routing_plan(facts);
            panic!(
                "requested {account_name} account is not selectable; eligible={:?}, exclusions={:?}",
                plan.eligible_account_names, plan.exclusions
            );
        });
    assert_eq!(claim.account_name(), account_name);
    drop(claim);
}

#[tokio::test(flavor = "current_thread")]
async fn online_agent_becomes_offline_without_affecting_shared_static_routing() {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let directory = tempfile::tempdir().expect("temporary Agent TLS fixture");
    let producer = TestTicketProducer::new();
    let cert_path = directory.path().join("agent-server.pem");
    let key_path = directory.path().join("agent-server-key.pem");
    fs::write(&cert_path, producer.server_cert_pem()).expect("write Agent certificate");
    fs::write(&key_path, producer.server_key_pem()).expect("write Agent private key");

    let control_port = TcpListener::bind(("127.0.0.1", 0))
        .expect("reserve control port")
        .local_addr()
        .expect("control address")
        .port();
    let external_port = loop {
        let port = TcpListener::bind(("127.0.0.1", 0))
            .expect("reserve external port")
            .local_addr()
            .expect("external address")
            .port();
        if port != control_port {
            break port;
        }
    };
    let transport_secret = "fixture-agent-transport-secret";
    let tls = eggpool::config::AgentTlsConfig {
        cert_path: cert_path.display().to_string(),
        key_path: key_path.display().to_string(),
        ..Default::default()
    };
    let account = eggpool::config::AccountConfig {
        name: "agent-a".into(),
        auth_mode: eggpool::config::AccountAuthMode::Agent,
        api_key: Some("agent-upstream-key".into()),
        api_key_env: "AGENT_UPSTREAM_KEY".into(),
        base_url: Some(format!("http://127.0.0.1:{external_port}/v1")),
        agent: Some(eggpool::config::AgentAccountConfig {
            control_bind: format!("127.0.0.1:{control_port}"),
            external_bind: format!("127.0.0.1:{external_port}"),
            transport_secret_env: "AGENT_TRANSPORT_SECRET".into(),
            tls: Some(tls),
            ..Default::default()
        }),
        ..Default::default()
    };
    let mut tunnel_config = Config::default();
    tunnel_config.agents.enabled = true;
    tunnel_config.providers.insert(
        "agent-provider".into(),
        eggpool::config::ProviderConfig {
            id: "agent-provider".into(),
            base_url: "https://provider.example/v1".into(),
            protocols: vec!["openai".into()],
            auth: eggpool::config::ProviderAuthConfig {
                mode: "none".into(),
                ..Default::default()
            },
            accounts: vec![account.clone()],
            ..Default::default()
        },
    );
    let tunnels = eggpool::agents::AgentTunnelManager::new()
        .start_account(
            &tunnel_config,
            "agent-provider",
            &account,
            None,
            transport_secret,
        )
        .await
        .expect("real reverse tunnel starts");
    let (database, database_path) = database("agent-routing-transition").await;
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state)
                 VALUES ('agent-provider', 'agent-a', 'agent-owner', 'active')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("Agent owner binding inserts");
    let bindings = eggpool::personal_upstream::load_snapshot(&database)
        .await
        .expect("Agent owner binding loads");
    let (router, _) = agent_router(true, true);
    let router = router.with_personal_bindings(bindings);
    router.set_agent_tunnel_availability(tunnels.availability());
    let mut facts = routing_facts("model-a");
    facts.principal_user_id = Some("agent-owner".into());

    // The shared account remains routable even while the Agent has not
    // established its first control connection.
    assert_eq!(
        router.agent_tunnel_state("agent-provider", "agent-a"),
        eggpool::agents::AgentTunnelState::Starting
    );
    assert_account_claimed(&router, &facts, "shared-a").await;
    assert!(
        router
            .select_and_claim_for_account(&facts, "agent-a")
            .await
            .expect("offline Agent selection checks")
            .is_none()
    );

    let provider_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("reverse client target binds");
    let provider_address = provider_listener
        .local_addr()
        .expect("reverse client target address");
    let client = ReverseClient::new(ReverseClientConfig {
        server_addr: format!("127.0.0.1:{control_port}")
            .parse()
            .expect("control address parses"),
        auth_username: Some("agent-a".into()),
        auth_password: Some(transport_secret.to_owned()),
        reconnect_initial_ms: 5,
        reconnect_max_ms: 20,
        default_target_host: Some(provider_address.ip().to_string()),
        default_target_port: Some(provider_address.port()),
        tls: Some(ReverseClientTlsConfig {
            ca_pem: Some(producer.ca_pem().as_bytes().to_vec()),
            server_name: SERVER_NAME.to_owned(),
            client_cert_pem: None,
            client_key_pem: None,
        }),
        ..ReverseClientConfig::default()
    });
    let cancel_client = client.cancel_token();
    let mut client_task = tokio::spawn(async move { client.run().await });
    tokio::select! {
        _ = wait_for_agent_control_count(&tunnels, 1) => {},
        result = &mut client_task => panic!("reverse client exited before connecting: {result:?}"),
    }
    let (provider_stream, _) =
        tokio::time::timeout(Duration::from_secs(5), provider_listener.accept())
            .await
            .expect("reverse client connects to its local target before deadline")
            .expect("local target accepts the reverse client connection");
    assert_eq!(
        router.agent_tunnel_state("agent-provider", "agent-a"),
        eggpool::agents::AgentTunnelState::Online
    );
    assert_account_claimed(&router, &facts, "agent-a").await;
    assert_account_claimed(&router, &facts, "shared-a").await;

    // The reverse server queues authenticated idle control sessions until an
    // external connection consumes one. Pair this real control session through
    // its loopback listener, then close both sides to disconnect the client.
    cancel_client.cancel();
    let external_stream = tokio::net::TcpStream::connect(("127.0.0.1", external_port))
        .await
        .expect("external listener consumes the active control session");
    drop(external_stream);
    drop(provider_stream);
    match tokio::time::timeout(Duration::from_secs(5), &mut client_task).await {
        Ok(result) => result
            .expect("reverse client task joins")
            .expect("reverse client exits cleanly"),
        Err(_) => {
            // Match the other reverse-client integration fixtures: cancellation
            // is best-effort, but aborting the owner must close the TCP tunnel.
            client_task.abort();
            let _ = client_task.await;
        }
    }
    wait_for_agent_control_count(&tunnels, 0).await;
    assert_eq!(
        router.agent_tunnel_state("agent-provider", "agent-a"),
        eggpool::agents::AgentTunnelState::Offline
    );
    assert!(
        router
            .select_and_claim_for_account(&facts, "agent-a")
            .await
            .expect("offline Agent selection checks")
            .is_none()
    );
    assert_account_claimed(&router, &facts, "shared-a").await;
    let closed = tunnels.close(Duration::from_secs(2)).await;
    assert_eq!(closed.released, 1);
    database
        .close()
        .await
        .expect("owner binding database closes");
    fs::remove_file(database_path).expect("owner binding database is removed");
}

#[tokio::test(flavor = "current_thread")]
async fn offline_agent_exclusion_preserves_shared_routing_and_origin_ownership() {
    let (database, path) = database("agent-personal-binding").await;
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state)
                 VALUES ('agent-provider', 'agent-a', 'owner-user', 'active')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("Agent owner binding inserts");
    let bindings = eggpool::personal_upstream::load_snapshot(&database)
        .await
        .expect("Agent owner binding loads");
    let (router, _) = agent_router(true, true);
    let router = router.with_personal_bindings(bindings);

    let mut owner = routing_facts("model-a");
    owner.principal_user_id = Some("owner-user".into());
    let owner_plan = router.build_routing_plan(&owner);
    assert!(
        owner_plan
            .eligible_account_names
            .contains(&"shared-a".to_owned())
    );
    assert!(
        !owner_plan
            .eligible_account_names
            .contains(&"agent-a".to_owned())
    );
    assert!(owner_plan.exclusions.iter().any(|exclusion| {
        exclusion.account_name == "agent-a" && exclusion.reason_code == "agent_offline"
    }));
    let owner_origins = router.model_origins_for_user("model-a", "owner-user");
    assert!(
        owner_origins
            .iter()
            .any(|origin| origin.account_name == "agent-a")
    );
    assert!(
        owner_origins
            .iter()
            .any(|origin| origin.account_name == "shared-a")
    );

    let owner_claim = router
        .select_and_claim(&owner, &BTreeSet::new())
        .await
        .expect("owner selection succeeds")
        .expect("the unaffected shared account remains available");
    assert_eq!(owner_claim.account_name(), "shared-a");
    drop(owner_claim);

    let mut other = routing_facts("model-a");
    other.principal_user_id = Some("other-user".into());
    let other_plan = router.build_routing_plan(&other);
    assert!(
        other_plan
            .eligible_account_names
            .contains(&"shared-a".to_owned())
    );
    assert!(
        !other_plan
            .eligible_account_names
            .contains(&"agent-a".to_owned())
    );
    assert!(other_plan.exclusions.iter().any(|exclusion| {
        exclusion.account_name == "agent-a" && exclusion.reason_code == "personal_upstream"
    }));
    let other_origins = router.model_origins_for_user("model-a", "other-user");
    assert!(
        !other_origins
            .iter()
            .any(|origin| origin.account_name == "agent-a")
    );
    assert!(
        other_origins
            .iter()
            .any(|origin| origin.account_name == "shared-a")
    );
    let other_claim = router
        .select_and_claim(&other, &BTreeSet::new())
        .await
        .expect("other-user selection succeeds")
        .expect("the shared account remains available to other users");
    assert_eq!(other_claim.account_name(), "shared-a");
    drop(other_claim);

    database.close().await.expect("database closes");
    fs::remove_file(path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn a_personal_binding_is_selectable_only_by_its_owner() {
    let (database, path) = database("personal-binding").await;
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO personal_upstream_bindings(provider_id, account_name, user_id, state)
                 VALUES ('provider-a', 'account-a', 'owner-user', 'active')",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    let snapshot = eggpool::personal_upstream::load_snapshot(&database)
        .await
        .unwrap();
    let (router, _) = health_router();
    let router = router.with_personal_bindings(snapshot);
    let mut owner = routing_facts("model-a");
    owner.principal_user_id = Some("owner-user".into());
    let claim = router
        .select_and_claim(&owner, &BTreeSet::new())
        .await
        .unwrap()
        .expect("the owner can select the bound account");
    assert_eq!(claim.account_name(), "account-a");
    drop(claim);
    let mut other = routing_facts("model-a");
    other.principal_user_id = Some("other-user".into());
    assert!(
        router
            .select_and_claim(&other, &BTreeSet::new())
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        router
            .select_and_claim(&routing_facts("model-a"), &BTreeSet::new())
            .await
            .unwrap()
            .is_none()
    );
    database
        .call(|connection| {
            connection.execute(
                "UPDATE personal_upstream_bindings SET state = 'withdrawn'",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    let withdrawn = eggpool::personal_upstream::load_snapshot(&database)
        .await
        .unwrap();
    let (router, _) = health_router();
    let router = router.with_personal_bindings(withdrawn);
    assert!(
        router
            .select_and_claim(&owner, &BTreeSet::new())
            .await
            .unwrap()
            .is_none(),
        "a withdrawn account is selected for nobody"
    );
    database.close().await.unwrap();
    fs::remove_file(path).unwrap();
}
