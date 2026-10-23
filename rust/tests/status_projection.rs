//! Operator status projection fidelity guards.
//!
//! The CLI's offline fallback and the server's published status projection
//! share one typed schema (`eggpool::operations::status`). These tests pin:
//!
//! - the offline fallback's serialized field set equals the published
//!   schema's field set (top level, `proxy`, `runtime`, and every provider
//!   row), so the two projections cannot drift;
//! - the fallback still reports the server as unavailable instead of
//!   claiming observed health;
//! - provider rows stay deterministically ordered and bounded;
//! - the redaction boundary removes secret-shaped material, keeps
//!   non-sensitive fields intact, and never panics on unusual input.
//!
//! The CLI is exercised end to end through its own binary so the guards
//! cover the shipped projection, not only the library entry points.

use std::collections::BTreeSet;
use std::path::Path;

use eggpool::operations::status as status_service;
use serde_json::{Map, Value, json};

const BASE_URL: &str = "http://127.0.0.1:11300";
const VERSION: &str = "0.8.2-test";
/// Exit code the CLI uses when the local control socket is unreachable and
/// the offline fallback is printed instead.
const EXIT_CONTROL_UNAVAILABLE: i32 = 3;

fn account(
    name: &str,
    provider: &str,
    enabled: bool,
    routable: bool,
    success: bool,
) -> status_service::AccountStatusInput {
    status_service::AccountStatusInput {
        account_name: name.to_owned(),
        provider_id: provider.to_owned(),
        enabled,
        has_usable_credentials: true,
        routable: enabled && routable,
        in_backoff: enabled && !routable,
        backoff_reason: None,
        circuit_open: false,
        has_success_observation: success,
        has_failure_observation: false,
        has_model_quarantine: false,
        auth_terminal: false,
    }
}

/// Build the snapshot the server publishes for a live generation.
///
/// The shape mirrors `server/health.rs::build_status_snapshot`: the same
/// typed structs, assembled from aggregated provider health plus the
/// readiness and runtime facts the server observes.
fn live_snapshot() -> status_service::ProxyStatusSnapshot {
    let providers = vec![
        status_service::aggregate_provider(&status_service::ProviderStatusInput {
            provider_id: "minimax".to_owned(),
            accounts: vec![account("minimax-a1", "minimax", true, true, true)],
            ping: status_service::PingEvidence::never(),
            catalog_model_count: Some(2),
            stale_after_secs: 7200,
        }),
        status_service::aggregate_provider(&status_service::ProviderStatusInput {
            provider_id: "opencode-go".to_owned(),
            accounts: vec![account("opencode-a1", "opencode-go", true, true, false)],
            ping: status_service::PingEvidence::never(),
            catalog_model_count: Some(1),
            stale_after_secs: 7200,
        }),
    ];
    status_service::ProxyStatusSnapshot {
        schema_version: status_service::STATUS_SCHEMA_VERSION,
        observed_at: status_service::observed_at_now(),
        proxy: status_service::ProxyHealthSummary {
            status: status_service::ProxyStatus::Ready,
            ready: true,
            available: true,
            version: VERSION.to_owned(),
            base_url: BASE_URL.to_owned(),
            uptime_seconds: Some(12.5),
            model_count: 3,
            routable_accounts: 2,
            enabled_accounts: 2,
            reason_code: None,
        },
        providers,
        runtime: status_service::RuntimeHealthSummary {
            generation: Some(7),
            digest_prefix: "abc123".to_owned(),
            reload: "idle".to_owned(),
            tasks: "2/3".to_owned(),
            db: "ok".to_owned(),
            retiring: 0,
        },
    }
}

fn offline_facts() -> Vec<status_service::OfflineProviderFacts> {
    vec![
        status_service::OfflineProviderFacts {
            provider_id: "opencode-go".to_owned(),
            total_accounts: 1,
            enabled_accounts: 1,
        },
        status_service::OfflineProviderFacts {
            provider_id: "minimax".to_owned(),
            total_accounts: 2,
            enabled_accounts: 1,
        },
    ]
}

fn object_keys(value: &Value) -> BTreeSet<String> {
    value
        .as_object()
        .expect("projection member is an object")
        .keys()
        .cloned()
        .collect()
}

fn write_config(path: &Path, providers: &[(String, usize, usize)]) {
    let mut text = String::new();
    text.push_str(
        "[server]\nhost = \"0.0.0.0\"\nport = 11300\n\n[database]\npath = \":memory:\"\n",
    );
    for (provider_id, total_accounts, enabled_accounts) in providers {
        text.push_str(&format!(
            "\n[providers.{provider_id}]\nid = \"{provider_id}\"\nbase_url = \"https://{provider_id}.example.invalid/v1\"\nprotocols = [\"openai\"]\n"
        ));
        for index in 0..*total_accounts {
            text.push_str(&format!(
                "\n[[providers.{provider_id}.accounts]]\nname = \"{provider_id}-account-{index}\"\napi_key = \"offline-cli-secret-sentinel\"\nenabled = {}\n",
                index < *enabled_accounts
            ));
        }
    }
    std::fs::write(path, text).expect("config writes");
}

/// Create the private runtime directory the local control socket requires.
fn private_runtime_dir(root: &Path) -> std::path::PathBuf {
    let runtime_dir = root.join("runtime");
    std::fs::create_dir(&runtime_dir).expect("runtime directory");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&runtime_dir, std::fs::Permissions::from_mode(0o700))
            .expect("private runtime directory");
    }
    runtime_dir
}

async fn run_cli(
    config_path: &Path,
    runtime_dir: &Path,
    home: &Path,
    args: &[&str],
) -> std::process::Output {
    tokio::process::Command::new(env!("CARGO_BIN_EXE_eggpool"))
        .args(args)
        .env("EGGPOOL_CONFIG", config_path)
        .env("EGGPOOL_RUNTIME_DIR", runtime_dir)
        .env("HOME", home)
        .env("XDG_CONFIG_HOME", home.join("config"))
        .env("XDG_DATA_HOME", home.join("data"))
        .env("XDG_STATE_HOME", home.join("state"))
        .output()
        .await
        .expect("status CLI executes")
}

/// Run `eggpool status --json` with no reachable server and return the
/// offline fallback projection the CLI printed.
async fn offline_status_json(config_path: &Path, runtime_dir: &Path, home: &Path) -> Value {
    let output = run_cli(config_path, runtime_dir, home, &["status", "--json"]).await;
    assert_eq!(
        output.status.code(),
        Some(EXIT_CONTROL_UNAVAILABLE),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("offline fallback is valid JSON")
}

/// Run `eggpool status --json` against a private control socket that serves
/// `payload`, and return the projection the CLI published.
async fn cli_status_json_over_control(
    config_path: &Path,
    runtime_dir: &Path,
    home: &Path,
    payload: Value,
) -> Value {
    use eggpool::operations::control::{ControlResponse, start};

    let socket = runtime_dir.join("eggpool.sock");
    let control = start(&socket, move |request| {
        let payload = payload.clone();
        async move { ControlResponse::with_payload(request.request_id, payload) }
    })
    .await
    .expect("private control listener");

    let output = run_cli(config_path, runtime_dir, home, &["status", "--json"]).await;
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    control.close().await.expect("control socket closes");
    serde_json::from_str(&stdout).expect("status JSON parses")
}

#[test]
fn offline_fallback_carries_the_published_status_schema() {
    let live = live_snapshot();
    let live_json = serde_json::to_value(&live).expect("live snapshot serializes");
    let offline = status_service::offline_snapshot(&offline_facts(), BASE_URL, VERSION);
    let offline_json = serde_json::to_value(&offline).expect("offline snapshot serializes");

    assert_eq!(
        object_keys(&offline_json),
        object_keys(&live_json),
        "the offline fallback's top-level field set drifted from the published schema"
    );
    assert_eq!(
        object_keys(&offline_json["proxy"]),
        object_keys(&live_json["proxy"]),
        "the offline fallback's proxy field set drifted from the published schema"
    );
    assert_eq!(
        object_keys(&offline_json["runtime"]),
        object_keys(&live_json["runtime"]),
        "the offline fallback's runtime field set drifted from the published schema"
    );

    // Every provider row carries the typed provider summary's full field set.
    let live_rows = live_json["providers"]
        .as_array()
        .expect("live providers are an array");
    let offline_rows = offline_json["providers"]
        .as_array()
        .expect("offline providers are an array");
    assert_eq!(offline_rows.len(), live_rows.len());
    for (offline_row, live_row) in offline_rows.iter().zip(live_rows) {
        assert_eq!(
            object_keys(offline_row),
            object_keys(live_row),
            "the offline fallback's provider field set drifted from the published schema"
        );
    }
    // The typed provider summary is the reference shape, not the row itself.
    let live_summary = serde_json::to_value(status_service::aggregate_provider(
        &status_service::ProviderStatusInput {
            provider_id: "minimax".to_owned(),
            accounts: vec![account("minimax-a1", "minimax", true, true, true)],
            ping: status_service::PingEvidence::never(),
            catalog_model_count: Some(2),
            stale_after_secs: 7200,
        },
    ))
    .expect("provider summary serializes");
    for offline_row in offline_rows {
        assert_eq!(
            object_keys(offline_row),
            object_keys(&live_summary),
            "the offline fallback's provider field set drifted from ProviderHealthSummary"
        );
    }
}

#[tokio::test]
async fn offline_fallback_carries_the_published_status_schema_end_to_end() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_dir = private_runtime_dir(root.path());
    let config_path = root.path().join("config.toml");
    write_config(
        &config_path,
        &[
            ("opencode-go".to_owned(), 1, 1),
            ("minimax".to_owned(), 2, 1),
        ],
    );

    let offline_json = offline_status_json(&config_path, &runtime_dir, root.path()).await;
    let live_json = serde_json::to_value(live_snapshot()).expect("live snapshot serializes");

    assert_eq!(
        object_keys(&offline_json),
        object_keys(&live_json),
        "the CLI's offline fallback drifted from the published schema"
    );
    assert_eq!(
        object_keys(&offline_json["proxy"]),
        object_keys(&live_json["proxy"]),
        "the CLI's offline fallback proxy drifted from the published schema"
    );
    assert_eq!(
        object_keys(&offline_json["runtime"]),
        object_keys(&live_json["runtime"]),
        "the CLI's offline fallback runtime drifted from the published schema"
    );
    let live_rows = live_json["providers"]
        .as_array()
        .expect("live providers are an array");
    let offline_rows = offline_json["providers"]
        .as_array()
        .expect("offline providers are an array");
    assert_eq!(offline_rows.len(), live_rows.len());
    for (offline_row, live_row) in offline_rows.iter().zip(live_rows) {
        assert_eq!(
            object_keys(offline_row),
            object_keys(live_row),
            "the CLI's offline fallback provider row drifted from the published schema"
        );
    }
}

#[test]
fn offline_fallback_reports_unavailable_instead_of_observed_health() {
    let offline = status_service::offline_snapshot(&offline_facts(), BASE_URL, VERSION);

    assert_eq!(
        offline.proxy.status,
        status_service::ProxyStatus::Unavailable
    );
    assert!(!offline.proxy.ready);
    assert!(!offline.proxy.available);
    assert!(
        offline.proxy.reason_code.is_some(),
        "an unavailable proxy must carry a reason code"
    );

    for provider in &offline.providers {
        assert_eq!(provider.status, status_service::ProviderStatus::Unknown);
        assert_eq!(
            provider.last_observation,
            status_service::ProviderObservation::Never
        );
        assert_eq!(provider.routable_accounts, 0);
        assert_eq!(provider.backoff_accounts, 0);
        assert_eq!(provider.unavailable_accounts, 0);
        assert!(provider.model_count.is_none());
        assert!(provider.last_probe_age_seconds.is_none());
        assert!(provider.last_probe_latency_ms.is_none());
        assert!(provider.last_probe_status_code.is_none());
    }

    assert!(offline.runtime.generation.is_none());
    assert_eq!(offline.runtime.reload, "unknown");
    assert_eq!(offline.runtime.tasks, "unknown");
    assert_eq!(offline.runtime.db, "unknown");
    assert_eq!(offline.runtime.retiring, 0);
}

#[tokio::test]
async fn offline_fallback_reports_unavailable_end_to_end() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_dir = private_runtime_dir(root.path());
    let config_path = root.path().join("config.toml");
    write_config(
        &config_path,
        &[
            ("opencode-go".to_owned(), 1, 1),
            ("minimax".to_owned(), 2, 1),
        ],
    );

    let offline_json = offline_status_json(&config_path, &runtime_dir, root.path()).await;

    assert_eq!(offline_json["proxy"]["status"], "unavailable");
    assert_eq!(offline_json["proxy"]["ready"], false);
    assert_eq!(offline_json["proxy"]["available"], false);
    assert!(
        offline_json["proxy"]["reason_code"].is_string(),
        "an unavailable proxy must carry a reason code"
    );
    for provider in offline_json["providers"]
        .as_array()
        .expect("providers are an array")
    {
        assert_eq!(provider["status"], "unknown");
        assert_eq!(provider["last_observation"], "never");
    }
    assert_eq!(offline_json["runtime"]["reload"], "unknown");
    assert_eq!(offline_json["runtime"]["tasks"], "unknown");
    assert_eq!(offline_json["runtime"]["db"], "unknown");
}

#[test]
fn offline_fallback_orders_and_bounds_provider_rows() {
    let facts: Vec<status_service::OfflineProviderFacts> = ["zeta", "alpha", "mid"]
        .iter()
        .map(|id| status_service::OfflineProviderFacts {
            provider_id: (*id).to_owned(),
            total_accounts: 1,
            enabled_accounts: 1,
        })
        .collect();
    let offline = status_service::offline_snapshot(&facts, BASE_URL, VERSION);
    let ids: Vec<&str> = offline
        .providers
        .iter()
        .map(|provider| provider.provider_id.as_str())
        .collect();
    assert_eq!(ids, vec!["alpha", "mid", "zeta"]);

    // The provider collection stays bounded by the published maximum.
    let many: Vec<status_service::OfflineProviderFacts> = (0..status_service::MAX_STATUS_PROVIDERS
        + 8)
        .map(|index| status_service::OfflineProviderFacts {
            provider_id: format!("provider-{index:04}"),
            total_accounts: 1,
            enabled_accounts: 1,
        })
        .collect();
    let bounded = status_service::offline_snapshot(&many, BASE_URL, VERSION);
    assert_eq!(
        bounded.providers.len(),
        status_service::MAX_STATUS_PROVIDERS
    );
    let bounded_ids: Vec<&str> = bounded
        .providers
        .iter()
        .map(|provider| provider.provider_id.as_str())
        .collect();
    let mut sorted = bounded_ids.clone();
    sorted.sort_unstable();
    assert_eq!(bounded_ids, sorted, "bounded provider rows stay sorted");
    assert_eq!(bounded_ids[0], "provider-0000");
    assert_eq!(
        bounded_ids[bounded_ids.len() - 1],
        format!("provider-{:04}", status_service::MAX_STATUS_PROVIDERS - 1)
    );
}

#[tokio::test]
async fn offline_fallback_orders_and_bounds_provider_rows_end_to_end() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_dir = private_runtime_dir(root.path());
    let config_path = root.path().join("config.toml");
    // One provider id longer than the published provider-id bound.
    let long_id = format!(
        "provider-{}",
        "x".repeat(status_service::MAX_PROVIDER_ID_CHARS)
    );
    write_config(
        &config_path,
        &[
            ("zeta".to_owned(), 1, 1),
            ("alpha".to_owned(), 1, 1),
            (long_id.clone(), 1, 1),
        ],
    );

    let offline_json = offline_status_json(&config_path, &runtime_dir, root.path()).await;
    let rows = offline_json["providers"]
        .as_array()
        .expect("providers are an array");
    let ids: Vec<&str> = rows
        .iter()
        .filter_map(|row| row["provider_id"].as_str())
        .collect();
    assert_eq!(ids.len(), 3);
    let mut sorted = ids.clone();
    sorted.sort_unstable();
    assert_eq!(ids, sorted, "provider rows stay sorted by provider id");
    assert_eq!(ids[0], "alpha");
    assert_eq!(ids[2], "zeta");
    assert_eq!(
        ids[1].chars().count(),
        status_service::MAX_PROVIDER_ID_CHARS,
        "the provider id is bounded like the published schema bounds it"
    );
    assert!(long_id.starts_with(ids[1]));
}

fn seeded_status_value() -> Value {
    json!({
        "schema_version": 1,
        "observed_at": "2026-10-22T12:00:00Z",
        "proxy": {
            "status": "ready",
            "ready": true,
            "available": true,
            "version": "0.8.2",
            "base_url": "http://127.0.0.1:11300",
            "uptime_seconds": 12.5,
            "model_count": 2,
            "routable_accounts": 1,
            "enabled_accounts": 1,
            "reason_code": null,
            "api_key": "sk-cli-status-secret-0001",
            "authorization": "Bearer CliStatusBearerToken0001"
        },
        "providers": [
            {
                "provider_id": "minimax",
                "status": "ready",
                "enabled_accounts": 1,
                "total_accounts": 1,
                "routable_accounts": 1,
                "backoff_accounts": 0,
                "unavailable_accounts": 0,
                "model_count": 2,
                "last_probe_age_seconds": 12,
                "last_probe_latency_ms": 84,
                "last_probe_status_code": 200,
                "last_observation": "verified",
                "reason_code": null
            }
        ],
        "runtime": {
            "generation": 7,
            "digest_prefix": "abc123",
            "reload": "idle",
            "tasks": "2/3",
            "db": "ok",
            "retiring": 0
        },
        "notes": "Bearer CliStatusBearerToken0001",
        "inline": "api_key=cli-status-secret-inline",
        "vendor": "sk-abcdefghijklmnop"
    })
}

const SECRET_SENTINELS: &[&str] = &[
    "sk-cli-status-secret-0001",
    "CliStatusBearerToken0001",
    "cli-status-secret-inline",
    "abcdefghijklmnop",
];

#[test]
fn redaction_removes_secret_shaped_material() {
    let seeded = seeded_status_value();
    let projected = status_service::redact_status_projection(&seeded);
    let rendered = projected.to_string();

    for sentinel in SECRET_SENTINELS {
        assert!(
            !rendered.contains(sentinel),
            "secret material survived the projection: {sentinel}"
        );
    }
    assert_eq!(
        projected["proxy"]["api_key"],
        json!(status_service::REDACTED_MARKER),
        "a secret-bearing member is replaced with the redaction marker"
    );
    assert_eq!(
        projected["proxy"]["authorization"],
        json!(status_service::REDACTED_MARKER),
        "a secret-bearing member is replaced with the redaction marker"
    );
    // Secret-shaped string values are replaced even outside a member whose
    // name is secret-bearing.
    assert_eq!(projected["notes"], json!(status_service::REDACTED_MARKER));
    assert_eq!(projected["inline"], json!(status_service::REDACTED_MARKER));
    assert_eq!(projected["vendor"], json!(status_service::REDACTED_MARKER));
    assert!(status_service::is_secret_bearing_key("api_key"));
    assert!(status_service::is_secret_bearing_key("authorization"));
    assert!(!status_service::is_secret_bearing_key("provider_id"));
}

#[tokio::test]
async fn redaction_removes_secret_shaped_material_end_to_end() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_dir = private_runtime_dir(root.path());
    let config_path = root.path().join("config.toml");
    write_config(&config_path, &[("minimax".to_owned(), 1, 1)]);

    let projected = cli_status_json_over_control(
        &config_path,
        &runtime_dir,
        root.path(),
        seeded_status_value(),
    )
    .await;

    for sentinel in SECRET_SENTINELS {
        assert!(
            !projected.to_string().contains(sentinel),
            "secret material survived the CLI projection: {sentinel}"
        );
    }
    assert_eq!(
        projected["proxy"]["api_key"],
        json!(status_service::REDACTED_MARKER)
    );
    assert_eq!(
        projected["proxy"]["authorization"],
        json!(status_service::REDACTED_MARKER)
    );
    assert_eq!(projected["notes"], json!(status_service::REDACTED_MARKER));
    assert_eq!(projected["inline"], json!(status_service::REDACTED_MARKER));
    assert_eq!(projected["vendor"], json!(status_service::REDACTED_MARKER));
}

#[tokio::test]
async fn redaction_preserves_non_sensitive_fields() {
    let seeded = seeded_status_value();
    let projected = status_service::redact_status_projection(&seeded);

    assert_ne!(
        projected, seeded,
        "the projection must actually remove the seeded secret material"
    );
    assert_eq!(projected["schema_version"], seeded["schema_version"]);
    assert_eq!(projected["observed_at"], seeded["observed_at"]);
    assert_eq!(projected["proxy"]["status"], seeded["proxy"]["status"]);
    assert_eq!(projected["proxy"]["ready"], seeded["proxy"]["ready"]);
    assert_eq!(projected["proxy"]["version"], seeded["proxy"]["version"]);
    assert_eq!(projected["proxy"]["base_url"], seeded["proxy"]["base_url"]);
    assert_eq!(
        projected["proxy"]["model_count"],
        seeded["proxy"]["model_count"]
    );
    assert_eq!(
        projected["proxy"]["enabled_accounts"],
        seeded["proxy"]["enabled_accounts"]
    );
    assert_eq!(projected["providers"][0]["provider_id"], "minimax");
    assert_eq!(projected["providers"][0]["status"], "ready");
    assert_eq!(projected["providers"][0]["enabled_accounts"], 1);
    assert_eq!(projected["providers"][0]["total_accounts"], 1);
    assert_eq!(projected["providers"][0]["last_probe_latency_ms"], 84);
    assert_eq!(projected["runtime"]["generation"], 7);
    assert_eq!(projected["runtime"]["reload"], "idle");
    assert_eq!(projected["runtime"]["tasks"], "2/3");
    assert_eq!(projected["runtime"]["db"], "ok");

    // The same holds for the projection the CLI actually publishes.
    let root = tempfile::tempdir().expect("temp root");
    let runtime_dir = private_runtime_dir(root.path());
    let config_path = root.path().join("config.toml");
    write_config(&config_path, &[("minimax".to_owned(), 1, 1)]);

    let cli_projected = cli_status_json_over_control(
        &config_path,
        &runtime_dir,
        root.path(),
        seeded_status_value(),
    )
    .await;

    assert_ne!(
        cli_projected, seeded,
        "the CLI projection must actually remove the seeded secret material"
    );
    assert_eq!(cli_projected["schema_version"], seeded["schema_version"]);
    assert_eq!(cli_projected["observed_at"], seeded["observed_at"]);
    assert_eq!(cli_projected["proxy"]["status"], "ready");
    assert_eq!(cli_projected["proxy"]["ready"], true);
    assert_eq!(cli_projected["proxy"]["version"], "0.8.2");
    assert_eq!(cli_projected["proxy"]["base_url"], "http://127.0.0.1:11300");
    assert_eq!(cli_projected["proxy"]["model_count"], 2);
    assert_eq!(cli_projected["proxy"]["enabled_accounts"], 1);
    assert_eq!(cli_projected["providers"][0]["provider_id"], "minimax");
    assert_eq!(cli_projected["providers"][0]["status"], "ready");
    assert_eq!(cli_projected["providers"][0]["enabled_accounts"], 1);
    assert_eq!(cli_projected["providers"][0]["total_accounts"], 1);
    assert_eq!(cli_projected["providers"][0]["last_probe_latency_ms"], 84);
    assert_eq!(cli_projected["runtime"]["generation"], 7);
    assert_eq!(cli_projected["runtime"]["reload"], "idle");
    assert_eq!(cli_projected["runtime"]["tasks"], "2/3");
    assert_eq!(cli_projected["runtime"]["db"], "ok");
}

fn nested_value(depth: usize) -> Value {
    let mut value = Value::String("leaf".to_owned());
    for _ in 0..depth {
        let mut members = Map::new();
        members.insert("level".to_owned(), value);
        value = Value::Object(members);
    }
    value
}

#[test]
fn redaction_is_safe_on_unusual_input() {
    // A large plain string stays intact and does not panic.
    let large = "x".repeat(1_000_000);
    let projected = status_service::redact_status_projection(&json!(large));
    assert_eq!(projected.as_str().map(str::len), Some(1_000_000));

    // A deeply nested object is walked without overflowing the stack.
    let deep = nested_value(64);
    let projected = status_service::redact_status_projection(&deep);
    let mut cursor = &projected;
    for _ in 0..64 {
        cursor = &cursor["level"];
    }
    assert_eq!(cursor, &json!("leaf"));

    // An empty object stays an empty object.
    let projected = status_service::redact_status_projection(&json!({}));
    assert_eq!(projected, json!({}));

    // Arrays keep their shape and their non-sensitive members.
    let projected =
        status_service::redact_status_projection(&json!([1, "two", {"three": 3}, null, true]));
    assert_eq!(projected, json!([1, "two", {"three": 3}, null, true]));

    // Non-string scalars pass through unchanged.
    for scalar in [json!(42), json!(true), json!(false), json!(null)] {
        assert_eq!(status_service::redact_status_projection(&scalar), scalar);
    }

    // Control characters inside a string are neither dropped nor fatal.
    let control = json!("a\u{0}b\u{7}c\u{1b}[31m");
    let projected = status_service::redact_status_projection(&control);
    assert_eq!(projected, control);
}
