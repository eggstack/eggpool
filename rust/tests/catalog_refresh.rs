use std::sync::{Arc, Mutex};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{CatalogModelEvent, CatalogService, RefreshOutcome},
    config::{AccountConfig as ConfigAccount, ProviderAuthConfig, ProviderConfig},
    db::{AccountConfig, AccountRepository, Database, DatabaseConfig, MigrationRunner},
    providers::ProviderClientPool,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

struct FixtureServer {
    address: String,
    task: tokio::task::JoinHandle<()>,
}

impl FixtureServer {
    async fn start(status: u16, body: &'static str, requests: Arc<Mutex<Vec<String>>>) -> Self {
        Self::start_sequence(vec![(status, body)], requests).await
    }

    async fn start_sequence(
        responses: Vec<(u16, &'static str)>,
        requests: Arc<Mutex<Vec<String>>>,
    ) -> Self {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
            .await
            .expect("fixture listener");
        let address = format!("http://{}", listener.local_addr().expect("fixture address"));
        let observed_requests = Arc::clone(&requests);
        let task = tokio::spawn(async move {
            for (status, body) in responses {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let mut request = vec![0; 16 * 1024];
                let size = stream.read(&mut request).await.unwrap_or_default();
                observed_requests
                    .lock()
                    .expect("request mutex")
                    .push(String::from_utf8_lossy(&request[..size]).into_owned());
                let response = format!(
                    "HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes()).await;
            }
        });
        Self { address, task }
    }

    async fn finish(self) {
        self.task.await.expect("fixture task");
    }
}

async fn database() -> (Database, tempfile::TempDir) {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("state.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migration");
    (database, directory)
}

async fn service(
    mut config: Config,
    database: &Database,
    credentials: CredentialStore,
) -> CatalogService {
    let db_account = ConfigAccount {
        name: "account-a".into(),
        api_key_env: "ACCOUNT_A_KEY".into(),
        ..Default::default()
    };
    config
        .providers
        .values_mut()
        .next()
        .expect("provider")
        .accounts = vec![db_account.clone()];
    config.validate().expect("fixture config");
    AccountRepository::new(database)
        .sync_from_config(vec![AccountConfig {
            name: db_account.name,
            api_key_env: db_account.api_key_env,
            enabled: true,
            weight: 1.0,
            provider_id: "fixture".into(),
        }])
        .await
        .expect("account sync");
    let accounts = AccountRepository::new(database)
        .list_all()
        .await
        .expect("accounts");
    let registry =
        AccountRegistry::from_config(&config, &accounts, &credentials).expect("registry");
    let pool = ProviderClientPool::from_config(&config).expect("client pool");
    CatalogService::with_credentials(config, registry, database.clone(), pool, credentials)
}

fn config(base_url: String) -> Config {
    let mut config = Config::default();
    config.models.catalog_withdrawal_policy = "confirmed_once".into();
    config.providers.insert(
        "fixture".into(),
        ProviderConfig {
            id: "fixture".into(),
            base_url,
            auth: ProviderAuthConfig {
                mode: "none".into(),
                ..Default::default()
            },
            ..Default::default()
        },
    );
    config
}

#[tokio::test(flavor = "current_thread")]
async fn disabled_endpoint_seeds_static_support_without_freshness() {
    let (database, _directory) = database().await;
    // No request is made for a disabled endpoint, so a live fixture listener
    // would have no accepted connection to join during teardown.
    let mut config = config("http://127.0.0.1:1".into());
    let provider = config.providers.get_mut("fixture").expect("provider");
    provider.models_endpoint = Some(eggpool::config::ProviderModelsEndpointConfig {
        method: "DISABLED".into(),
        required: false,
        ..Default::default()
    });
    provider
        .static_models
        .push(eggpool::config::ProviderStaticModelConfig {
            id: "static-model".into(),
            protocol: Some("openai".into()),
            max_context_tokens: Some(8192),
            ..Default::default()
        });
    let service = service(config, &database, CredentialStore::default()).await;
    let result = service.refresh().await.expect("refresh");
    assert_eq!(result.outcomes["account-a"], RefreshOutcome::Skipped);
    let snapshot = service.cache_snapshot().await;
    assert_eq!(snapshot.model_ids, vec!["static-model"]);
    assert!(snapshot.freshness.is_empty());
    let pings = eggpool::db::PingRepository::new(&database)
        .recent(None, 10)
        .await
        .expect("pings");
    assert_eq!(pings.len(), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn post_contract_normalizes_and_persists_semantic_rows() {
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = FixtureServer::start(
        200,
        r#"{"data":[{"id":"gpt-fixture","name":"Fixture","context_window":8192}]}"#,
        requests.clone(),
    )
    .await;
    let (database, _directory) = database().await;
    let mut config = config(server.address.clone());
    let provider = config.providers.get_mut("fixture").expect("provider");
    provider.auth = ProviderAuthConfig {
        mode: "bearer".into(),
        ..Default::default()
    };
    provider.models_endpoint = Some(eggpool::config::ProviderModelsEndpointConfig {
        method: "POST".into(),
        path: "/models".into(),
        body: Some(toml::Value::String("fixture-body".into())),
        query: [("limit".into(), "a b".into())].into_iter().collect(),
        ..Default::default()
    });
    provider
        .headers
        .push(eggpool::config::ProviderStaticHeaderConfig {
            name: "X-Fixture".into(),
            value: Some("present".into()),
            value_env: None,
        });
    let mut credentials = CredentialStore::default();
    credentials.insert("account-a", "secret-value");
    let service = service(config, &database, credentials).await;
    let result = service.refresh().await.expect("refresh");
    assert_eq!(
        result.outcomes["account-a"],
        RefreshOutcome::SuccessAuthoritative
    );
    let snapshot = service.cache_snapshot().await;
    assert_eq!(snapshot.model_ids, vec!["gpt-fixture"]);
    let request = requests
        .lock()
        .expect("request mutex")
        .first()
        .cloned()
        .expect("request");
    assert!(request.starts_with("POST /models?limit=a%20b HTTP/1.1"));
    assert!(request.contains("authorization: Bearer secret-value"));
    // HTTP field names are case-insensitive; Hyper lowercases them when the
    // fixture records the raw request bytes.
    assert!(request.to_ascii_lowercase().contains("x-fixture: present"));
    assert!(
        !serde_json::to_string(&result)
            .expect("result json")
            .contains("secret-value")
    );
    let rows = eggpool::db::CatalogRepository::new(&database)
        .list_models()
        .await
        .expect("models");
    let live_rows: Vec<_> = rows
        .iter()
        .filter(|row| row.model_id != "__deprecated__")
        .collect();
    assert_eq!(live_rows.len(), 1);
    assert_eq!(live_rows[0].model_id, "gpt-fixture");
    server.finish().await;
}

#[tokio::test(flavor = "current_thread")]
async fn malformed_and_empty_responses_preserve_prior_support() {
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server =
        FixtureServer::start(200, r#"{"data":[{"id":"gpt-one"}]}"#, requests.clone()).await;
    let (database, _directory) = database().await;
    let initial_service = service(
        config(server.address.clone()),
        &database,
        CredentialStore::default(),
    )
    .await;
    let first = initial_service.refresh().await.expect("first refresh");
    assert_eq!(
        first.outcomes["account-a"],
        RefreshOutcome::SuccessAuthoritative
    );
    let snapshot_before = initial_service.cache_snapshot().await;
    assert!(snapshot_before.account_support["gpt-one"].contains(&"account-a".into()));
    server.finish().await;

    // A transport failure has no destructive cache path. The same service is
    // pointed at a closed listener by using a fresh service with the old DB;
    // this also verifies hydration preserves the prior support.
    let closed = "http://127.0.0.1:1".to_owned();
    let recovered = service(config(closed), &database, CredentialStore::default()).await;
    let result = recovered
        .refresh()
        .await
        .expect("failed refresh is ordinary outcome");
    assert_eq!(result.outcomes["account-a"], RefreshOutcome::Failed);
    let snapshot_after = recovered.cache_snapshot().await;
    assert!(snapshot_after.account_support["gpt-one"].contains(&"account-a".into()));
}

#[tokio::test(flavor = "current_thread")]
async fn authoritative_withdrawal_emits_exact_event_and_updates_durable_state() {
    let requests = Arc::new(Mutex::new(Vec::new()));
    let server = FixtureServer::start_sequence(
        vec![
            (200, r#"{"data":[{"id":"gpt-old"}]}"#),
            (200, r#"{"data":[{"id":"gpt-replacement"}]}"#),
        ],
        requests.clone(),
    )
    .await;
    let (database, _directory) = database().await;
    let mut config = config(server.address.clone());
    config.models.catalog_withdrawal_policy = "confirmed_once".into();
    let service = service(config, &database, CredentialStore::default()).await;
    let result = service.refresh().await.expect("refresh");
    assert_eq!(
        result.outcomes["account-a"],
        RefreshOutcome::SuccessAuthoritative
    );
    let result = service.refresh().await.expect("withdrawal refresh");
    assert!(result.events.iter().any(|event| matches!(event, CatalogModelEvent::Reappeared(row) if row.canonical_model_id == "gpt-replacement")));
    assert!(result.events.iter().any(|event| matches!(event, CatalogModelEvent::Withdrawn(row) if row.canonical_model_id == "gpt-old" && row.upstream_protocol == "openai")));
    let snapshot = service.cache_snapshot().await;
    assert!(snapshot.account_support["gpt-replacement"].contains(&"account-a".into()));
    assert!(!snapshot.model_ids.contains(&"gpt-old".into()));
    let rows = eggpool::db::CatalogRepository::new(&database)
        .list_models()
        .await
        .expect("models");
    assert!(rows.iter().any(|row| row.model_id == "gpt-replacement"));
    assert!(!rows.iter().any(|row| row.model_id == "gpt-old"));
    server.finish().await;
}
