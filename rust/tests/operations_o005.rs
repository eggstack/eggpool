//! O005 agent integration and configsetup generation contracts.

use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

use eggpool::operations::integrations::{
    IntegrationContext, IntegrationModel, ModelLimits, SnippetOptions, Target, apply_overrides,
    build_integration_context, deliver, render_target, resolve_model,
};
use serde_json::{Map, Value};
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
        None,
    )
    .await
    .expect("codex delivery");
    assert!(codex.stdout.is_none());
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
