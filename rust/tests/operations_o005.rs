//! O005 agent integration and configsetup generation contracts.

use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

use eggpool::operations::integrations::{
    IntegrationContext, IntegrationModel, LifecycleAction, LifecycleOptions, ModelLimits,
    SnippetOptions, Target, aggregate_projections, apply_overrides, build_codex_catalog_json,
    build_integration_context, build_remote_context, codex_lifecycle, deliver, opencode_lifecycle,
    parse_remote_target, paste_hint, project_context_models, project_model,
    remote_connection_token, render_remote_json, render_target, resolve_advertised_base_url,
    resolve_model, validate_codex_catalog_json,
};
use serde_json::{Map, Value, json};
use tempfile::tempdir;

fn context() -> IntegrationContext {
    IntegrationContext {
        config_path: PathBuf::from("/dev/null"),
        api_key: "ep_test_key_123".into(),
        base_url: "http://192.168.1.100:11300/v1".into(),
        base_url_root: "http://192.168.1.100:11300".into(),
        host: "192.168.1.100".into(),
        port: 11300,
        models: vec![IntegrationModel {
            model_id: "gpt-4o/openai".into(),
            base_model_id: "gpt-4o".into(),
            provider_id: Some("openai".into()),
            display_name: "GPT-4o".into(),
            capabilities: Value::Object(Map::new()),
            source_metadata: Value::Object(Map::new()),
            limits: ModelLimits {
                context_tokens: Some(128_000),
                ..Default::default()
            },
        }],
        collapse_models: false,
        config_mutated: false,
        transcoder_mutated: false,
    }
}

#[test]
fn every_configsetup_target_has_a_real_renderer() {
    let context = context();
    for target in Target::ALL {
        let model = resolve_model(target, None, &context, false)
            .unwrap_or_else(|_| panic!("{}", target.name()));
        let rendered = render_target(target, &context, model.as_deref())
            .unwrap_or_else(|_| panic!("{}", target.name()));
        assert!(!rendered.is_empty(), "{}", target.name());
    }
}

#[test]
fn renderers_preserve_structured_and_shell_escaping() {
    let mut context = context();
    context.api_key = "ep_'secret\\tail".into();
    context.base_url = "https://example.invalid/\"v1".into();
    let aider = render_target(Target::Aider, &context, Some("model with space")).expect("aider");
    assert!(aider.contains("OPENAI_API_KEY="));
    assert!(aider.contains("secret"));
    let codex = render_target(Target::Codex, &context, Some("model\"\\x")).expect("codex");
    assert!(codex.contains("env_key = \"EGGPOOL_API_KEY\""));
    assert!(codex.contains("model = \"model\\\"\\\\x\""));
}

#[test]
fn base_url_and_host_overrides_are_normalized() {
    let context =
        apply_overrides(context(), None, Some("https://example.invalid/v1/")).expect("base URL");
    assert_eq!(context.base_url, "https://example.invalid/v1");
    assert_eq!(context.base_url_root, "https://example.invalid");
    let context = apply_overrides(context, Some("router.local"), None).expect("host");
    assert_eq!(context.base_url, "http://router.local:11300/v1");
}

#[test]
fn context_debug_redacts_the_server_key() {
    let context = context();
    let debug = format!("{context:?}");
    assert!(!debug.contains(&context.api_key));
    assert!(debug.contains("..."));
}

#[tokio::test]
async fn delivery_is_secret_safe_and_writes_atomically() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("nested").join("config.json");
    let snippet = "{\"api_key\":\"ep_test_key_123\"}";
    let options = SnippetOptions {
        print_secret: false,
        no_clipboard: true,
        force: false,
        output: Some(path.clone()),
        write: false,
    };
    let result = deliver(snippet, Target::QwenCode, &options, None, None)
        .await
        .expect("write");
    assert!(result.stdout.is_none());
    assert_eq!(
        fs::read_to_string(&path).expect("content"),
        format!("{snippet}\n")
    );
    assert_eq!(
        fs::metadata(&path).expect("metadata").permissions().mode() & 0o777,
        0o600
    );

    let error = deliver(snippet, Target::QwenCode, &options, None, None)
        .await
        .expect_err("overwrite must require force");
    assert!(error.to_string().contains("config.json"));

    let changed = "{\"new\":true}";
    let forced = SnippetOptions {
        force: true,
        ..options
    };
    let result = deliver(changed, Target::QwenCode, &forced, None, None)
        .await
        .expect("forced write");
    assert!(
        result
            .messages
            .iter()
            .any(|message| message.starts_with("Backup created:"))
    );
    assert_eq!(
        fs::read_to_string(&path).expect("content"),
        format!("{changed}\n")
    );
}

#[tokio::test]
async fn default_delivery_hides_secrets_and_explicit_print_reveals_them() {
    let snippet = "{\"api_key\":\"ep_test_key_123\"}";
    let hidden = deliver(
        snippet,
        Target::QwenCode,
        &SnippetOptions {
            print_secret: false,
            no_clipboard: true,
            force: false,
            output: None,
            write: false,
        },
        None,
        None,
    )
    .await
    .expect("hidden delivery");
    assert!(hidden.stdout.is_none());
    assert!(
        hidden
            .messages
            .iter()
            .any(|message| message.starts_with("Secret not printed"))
    );

    let printed = deliver(
        snippet,
        Target::QwenCode,
        &SnippetOptions {
            print_secret: true,
            no_clipboard: true,
            force: false,
            output: None,
            write: false,
        },
        None,
        None,
    )
    .await
    .expect("explicit delivery");
    assert_eq!(printed.stdout.as_deref(), Some(snippet));

    let codex = deliver(
        "env_key = \"EGGPOOL_API_KEY\"",
        Target::Codex,
        &SnippetOptions {
            print_secret: false,
            no_clipboard: true,
            force: false,
            output: None,
            write: false,
        },
        None,
        paste_hint(Target::Codex),
    )
    .await
    .expect("codex delivery");
    assert_eq!(
        codex.stdout.as_deref(),
        Some("env_key = \"EGGPOOL_API_KEY\"")
    );
    assert!(
        codex.messages.iter().any(
            |message| message.contains("EGGPOOL_API_KEY") && message.contains("eggpool getkey")
        )
    );

    let codex_print_secret = deliver(
        "env_key = \"EGGPOOL_API_KEY\"",
        Target::Codex,
        &SnippetOptions {
            print_secret: true,
            no_clipboard: true,
            force: false,
            output: None,
            write: false,
        },
        None,
        paste_hint(Target::Codex),
    )
    .await
    .expect("codex explicit print delivery");
    assert_eq!(
        codex_print_secret.stdout.as_deref(),
        Some("env_key = \"EGGPOOL_API_KEY\"")
    );
}

#[test]
fn codex_does_not_fabricate_a_model_when_catalog_has_multiple_models() {
    let mut context = context();
    context.models.push(IntegrationModel {
        model_id: "claude-sonnet/anthropic".into(),
        base_model_id: "claude-sonnet".into(),
        provider_id: Some("anthropic".into()),
        display_name: "Claude Sonnet".into(),
        capabilities: Value::Object(Map::new()),
        source_metadata: Value::Object(Map::new()),
        limits: ModelLimits::default(),
    });

    assert_eq!(
        resolve_model(Target::Codex, None, &context, false).expect("model resolution"),
        None
    );
}

#[tokio::test]
async fn context_uses_static_models_and_owns_transcoder_mutation() {
    let directory = tempdir().expect("temporary directory");
    let config_path = directory.path().join("config.toml");
    let database_path = directory.path().join("missing").join("usage.sqlite3");
    fs::write(
        &config_path,
        format!(
            "[server]\nport = 8080\n\n[database]\npath = \"{}\"\n\n[transcoder]\nenabled = false\n\n[providers.minimax]\nid = \"minimax\"\nbase_url = \"https://example.invalid/v1\"\nprotocols = [\"anthropic\"]\n[[providers.minimax.accounts]]\nname = \"account\"\napi_key = \"provider-secret\"\n[[providers.minimax.static_models]]\nid = \"MiniMax-M3\"\nprotocol = \"anthropic\"\nmax_context_tokens = 1000000\n",
            database_path.display()
        ),
    )
    .expect("config");

    let context = build_integration_context(&config_path)
        .await
        .expect("context");
    assert!(context.base_url.ends_with(":8080/v1"));
    assert!(context.config_mutated);
    assert!(context.transcoder_mutated);
    assert_eq!(context.models[0].model_id, "MiniMax-M3/minimax");
    assert_eq!(context.models[0].limits.context_tokens, Some(1_000_000));
    let config = fs::read_to_string(&config_path).expect("updated config");
    assert!(config.contains("api_key = \""));
    assert!(config.contains("[transcoder]\nenabled = true"));
}

#[test]
fn agent_projection_is_conservative_and_never_infers_from_names() {
    let tricky = IntegrationModel {
        model_id: "gpt-vision-tool-9000".into(),
        base_model_id: "gpt-vision-tool-9000".into(),
        provider_id: None,
        display_name: "Tricky".into(),
        capabilities: Value::Object(Map::new()),
        source_metadata: Value::Object(Map::new()),
        limits: ModelLimits::default(),
    };
    let projection = project_model(&tricky);
    assert_eq!(projection.capabilities.input_images, None);
    assert_eq!(projection.capabilities.function_tools, None);
    assert_eq!(projection.capabilities.reasoning, None);
    assert!(!projection.capabilities.websockets);

    let rich = IntegrationModel {
        model_id: "alias".into(),
        base_model_id: "alias".into(),
        provider_id: None,
        display_name: "Alias".into(),
        capabilities: json!({
            "supports_tools": true,
            "supports_vision": true,
            "thinking": {"status": "supported", "supported_efforts": ["low", "high"]},
        }),
        source_metadata: Value::Object(Map::new()),
        limits: ModelLimits {
            context_tokens: Some(100_000),
            output_tokens: Some(8_000),
            ..Default::default()
        },
    };
    let projections = project_context_models(&IntegrationContext {
        config_path: PathBuf::from("/dev/null"),
        api_key: "ep_test_key_123".into(),
        base_url: "http://127.0.0.1:11300/v1".into(),
        base_url_root: "http://127.0.0.1:11300".into(),
        host: "127.0.0.1".into(),
        port: 11300,
        models: vec![rich],
        collapse_models: false,
        config_mutated: false,
        transcoder_mutated: false,
    });
    assert_eq!(projections.len(), 1);
    assert_eq!(projections[0].capabilities.context_tokens, Some(100_000));

    // Aliases advertise the intersection, not the union.
    let left = aggregate_projections("alias", "Alias", &[projections[0].capabilities.clone()]);
    assert_eq!(left.context_tokens, Some(100_000));
}

#[test]
fn codex_catalog_parses_with_limits_reasoning_and_no_websocket_advertisement() {
    let mut ctx = context();
    ctx.models.push(IntegrationModel {
        model_id: "reasoner/test".into(),
        base_model_id: "reasoner".into(),
        provider_id: Some("test".into()),
        display_name: "Reasoner".into(),
        capabilities: json!({
            "supports_tools": true,
            "supports_vision": false,
            "thinking": {"status": "supported", "supported_efforts": ["low", "high"]},
        }),
        source_metadata: Value::Object(Map::new()),
        limits: ModelLimits {
            context_tokens: Some(200_000),
            output_tokens: Some(16_000),
            ..Default::default()
        },
    });
    let catalog = build_codex_catalog_json(&ctx).expect("catalog");
    let count = validate_codex_catalog_json(&catalog, &ctx.api_key).expect("valid");
    assert_eq!(count, 2);
    let value: Value = serde_json::from_str(&catalog).expect("json");
    let models = value
        .get("models")
        .and_then(Value::as_array)
        .expect("models");
    let reasoner = models
        .iter()
        .find(|model| model.get("slug").and_then(Value::as_str) == Some("reasoner/test"))
        .expect("reasoner entry");
    assert_eq!(
        reasoner.get("context_window").and_then(Value::as_u64),
        Some(200_000)
    );
    assert_eq!(
        reasoner
            .get("auto_compact_token_limit")
            .and_then(Value::as_u64),
        Some(180_000)
    );
    assert!(
        reasoner
            .get("supported_reasoning_levels")
            .and_then(Value::as_array)
            .is_some_and(|levels| levels.len() == 2)
    );
    assert!(!catalog.contains(&ctx.api_key));
}

#[test]
fn opencode_provider_uses_responses_runtime_with_limits_and_env_key() {
    let rendered = render_target(Target::Opencode, &context(), None).expect("opencode");
    assert!(rendered.contains("@ai-sdk/openai"));
    assert!(!rendered.contains("@ai-sdk/openai-compatible\""));
    assert!(rendered.contains("{env:EGGPOOL_API_KEY}"));
    assert!(!rendered.contains("ep_test_key_123"));
    let value: Value = serde_json::from_str(&rendered).expect("json");
    let provider = value
        .get("provider")
        .and_then(|provider| provider.get("eggpool"))
        .expect("eggpool");
    let models = provider
        .get("models")
        .and_then(Value::as_object)
        .expect("models");
    let entry = models.get("gpt-4o/openai").expect("entry");
    assert_eq!(
        entry
            .get("limit")
            .and_then(|limit| limit.get("context"))
            .and_then(Value::as_u64),
        Some(128_000)
    );
}

#[test]
fn codex_managed_lifecycle_is_idempotent_and_refuses_drift() {
    let directory = tempdir().expect("temporary directory");
    let state_dir = directory.path().join("state");
    let config_path = directory.path().join("codex-config.toml");
    let ctx = context();

    // Empty/missing config converges on apply.
    let apply = LifecycleOptions {
        action: LifecycleAction::Apply,
        dry_run: false,
        force: false,
        model: None,
    };
    let first = codex_lifecycle(&ctx, &apply, Some(&state_dir), Some(&config_path)).expect("apply");
    assert!(first.changed);
    let second =
        codex_lifecycle(&ctx, &apply, Some(&state_dir), Some(&config_path)).expect("re-apply");
    assert!(!second.changed);

    // User config with comments and unrelated tables is preserved.
    let raw = fs::read_to_string(&config_path).expect("config");
    assert!(raw.contains("[model_providers.eggpool]"));

    // --sync after catalog change converges owned state.
    let mut updated = ctx.clone();
    updated.models.push(IntegrationModel {
        model_id: "new-model/test".into(),
        base_model_id: "new-model".into(),
        provider_id: Some("test".into()),
        display_name: "New Model".into(),
        capabilities: Value::Object(Map::new()),
        source_metadata: Value::Object(Map::new()),
        limits: ModelLimits {
            context_tokens: Some(50_000),
            ..Default::default()
        },
    });
    let sync = LifecycleOptions {
        action: LifecycleAction::Sync,
        dry_run: false,
        force: false,
        model: None,
    };
    let synced =
        codex_lifecycle(&updated, &sync, Some(&state_dir), Some(&config_path)).expect("sync");
    assert!(synced.changed);

    // Unrelated external edits are preserved, not refused.
    let mut tampered = fs::read_to_string(&config_path).expect("config");
    tampered.push_str("# operator edit\n");
    fs::write(&config_path, &tampered).expect("tamper");
    codex_lifecycle(&updated, &sync, Some(&state_dir), Some(&config_path)).expect("sync");
    let converged = fs::read_to_string(&config_path).expect("config");
    assert!(converged.contains("# operator edit"));

    // Owned-field drift still refuses without --force.
    let drifted = converged.replace("wire_api = \"responses\"", "wire_api = \"chat\"");
    fs::write(&config_path, &drifted).expect("drift");
    assert!(codex_lifecycle(&updated, &sync, Some(&state_dir), Some(&config_path)).is_err());

    // --remove restores only owned fields and deletes the catalog.
    let forced_sync = LifecycleOptions {
        action: LifecycleAction::Sync,
        dry_run: false,
        force: true,
        model: None,
    };
    codex_lifecycle(&updated, &forced_sync, Some(&state_dir), Some(&config_path)).expect("forced");
    let remove = LifecycleOptions {
        action: LifecycleAction::Remove,
        dry_run: false,
        force: false,
        model: None,
    };
    let removed =
        codex_lifecycle(&updated, &remove, Some(&state_dir), Some(&config_path)).expect("remove");
    assert!(removed.changed);
    let final_raw = fs::read_to_string(&config_path).unwrap_or_default();
    assert!(!final_raw.contains("model_catalog_json"));
    assert!(!final_raw.contains(&ctx.api_key));
}

#[test]
fn opencode_managed_lifecycle_preserves_existing_config_safely() {
    let directory = tempdir().expect("temporary directory");
    let state_dir = directory.path().join("state");
    let config_path = directory.path().join("opencode.json");
    let ctx = context();

    let apply = LifecycleOptions {
        action: LifecycleAction::Apply,
        dry_run: false,
        force: false,
        model: None,
    };
    opencode_lifecycle(&ctx, &apply, Some(&state_dir), Some(&config_path)).expect("apply");
    let raw = fs::read_to_string(&config_path).expect("config");
    assert!(!raw.contains(&ctx.api_key));

    // Existing non-EggPool content is preserved across merges.
    let mut value: Value = serde_json::from_str(&raw).expect("json");
    value
        .as_object_mut()
        .expect("object")
        .insert("custom".to_owned(), json!({"kept": true}));
    fs::write(
        &config_path,
        serde_json::to_string_pretty(&value).expect("json"),
    )
    .expect("write");
    // Unrelated edits no longer refuse: sync converges owned state only.
    let sync = LifecycleOptions {
        action: LifecycleAction::Sync,
        dry_run: false,
        force: false,
        model: None,
    };
    opencode_lifecycle(&ctx, &sync, Some(&state_dir), Some(&config_path)).expect("sync");
    let merged: Value =
        serde_json::from_str(&fs::read_to_string(&config_path).expect("config")).expect("json");
    assert_eq!(
        merged
            .get("custom")
            .and_then(|custom| custom.get("kept"))
            .and_then(Value::as_bool),
        Some(true)
    );

    // Owned-entry drift still refuses without --force.
    let mut drifted: Value =
        serde_json::from_str(&fs::read_to_string(&config_path).expect("config")).expect("json");
    drifted["provider"]["eggpool"]["npm"] = json!("@evil/pkg");
    fs::write(
        &config_path,
        serde_json::to_string_pretty(&drifted).expect("json"),
    )
    .expect("drift");
    assert!(opencode_lifecycle(&ctx, &sync, Some(&state_dir), Some(&config_path)).is_err());
    let forced = LifecycleOptions {
        action: LifecycleAction::Sync,
        dry_run: false,
        force: true,
        model: None,
    };
    opencode_lifecycle(&ctx, &forced, Some(&state_dir), Some(&config_path)).expect("forced");

    // JSONC with comments is preserved through managed mutation.
    fs::write(
        &config_path,
        "{\n// operator comment\n\"provider\": {},\n}\n",
    )
    .expect("jsonc");
    opencode_lifecycle(&ctx, &apply, Some(&state_dir), Some(&config_path)).expect("jsonc apply");
    let preserved = fs::read_to_string(&config_path).expect("config");
    assert!(preserved.contains("// operator comment"));
    assert!(preserved.contains("\"eggpool\""));
    let dry = LifecycleOptions {
        action: LifecycleAction::DryRun,
        dry_run: true,
        force: false,
        model: None,
    };
    let report =
        opencode_lifecycle(&ctx, &dry, Some(&state_dir), Some(&config_path)).expect("dry-run");
    assert!(report.diff.is_some());
}

#[tokio::test]
async fn configremote_is_read_only_deterministic_and_secret_free() {
    let directory = tempdir().expect("temporary directory");
    let config_path = directory.path().join("config.toml");
    let db_path = directory.path().join("usage.sqlite3");
    fs::write(
        &config_path,
        format!(
            "[server]\nhost = \"0.0.0.0\"\nport = 11300\napi_key = \"test-server-key-12345678\"\n\n[database]\npath = \"{}\"\n\n[integrations]\nadvertise_base_url = \"https://pool.example.internal/v1/\"\n",
            db_path.display()
        ),
    )
    .expect("config");
    let before = fs::read_to_string(&config_path).expect("before");

    // Explicit --base-url wins over configured advertisement.
    let config = eggpool::Config::from_toml(&config_path).expect("config parses");
    let resolved = resolve_advertised_base_url(&config, Some("https://override.example/v1/"))
        .expect("override wins");
    assert_eq!(resolved, "https://override.example/v1");

    let remote = build_remote_context(&config_path, None)
        .await
        .expect("remote context");
    assert_eq!(remote.base_url, "https://pool.example.internal/v1");
    assert!(remote.auth_configured);

    // Unknown targets are rejected without side effects.
    assert!(parse_remote_target("vscode").is_err());

    for target_name in ["codex", "opencode"] {
        let target = parse_remote_target(target_name).expect("target");
        let (_profile, first) = remote_connection_token(&remote, target).expect("token");
        let (_profile, second) = remote_connection_token(&remote, target).expect("token");
        assert_eq!(first, second);
        assert!(first.starts_with("epc1."));
        let json = render_remote_json(target_name, &remote, &first).expect("json");
        let value: Value = serde_json::from_str(&json).expect("json parses");
        assert_eq!(value["target"], target_name);
        assert_eq!(value["base_url"], "https://pool.example.internal/v1");
        assert_eq!(value["profile"], first);
        assert!(!json.contains("test-server-key"));
    }

    // Read-only: config file unchanged, no database file created.
    let after = fs::read_to_string(&config_path).expect("after");
    assert_eq!(before, after);
    assert!(!db_path.exists());

    // Loopback-only without advertisement fails closed.
    let loopback_path = directory.path().join("loopback.toml");
    fs::write(
        &loopback_path,
        "[server]\nhost = \"127.0.0.1\"\nport = 11300\napi_key = \"test-server-key-12345678\"\n",
    )
    .expect("loopback config");
    assert!(build_remote_context(&loopback_path, None).await.is_err());
}
