//! O004 configuration/key/provider mutation contracts.

use std::{fs, thread};

use eggpool::{
    Config,
    config_reload_policy::{ReloadDisposition, disposition_for},
    operations::config_mutation::{self, ApplyMode, ApplyOutcome},
    provider_profile,
};
use tempfile::tempdir;

fn base_config() -> &'static str {
    "# keep this comment\n[server]\nhost = \"127.0.0.1\"\nport = 11300\n\n[database]\npath = \"usage.sqlite3\"\n"
}

#[test]
fn server_edits_preserve_unrelated_text_and_reject_invalid_candidates() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");

    config_mutation::set_server_value(&path, "host", "0.0.0.0").expect("host edit");
    let after_host = fs::read_to_string(&path).expect("updated config");
    assert!(after_host.starts_with("# keep this comment\n"));
    assert!(after_host.contains("host = \"0.0.0.0\""));
    assert!(after_host.contains("path = \"usage.sqlite3\""));

    let before_invalid = after_host.clone();
    assert!(config_mutation::set_server_value(&path, "port", "70000").is_err());
    assert_eq!(fs::read_to_string(&path).expect("config"), before_invalid);
}

#[test]
fn dashboard_edits_insert_only_the_owned_key() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");

    assert!(config_mutation::read_dashboard_public(&path).expect("default"));
    config_mutation::set_dashboard_public(&path, Some(false)).expect("dashboard edit");
    let text = fs::read_to_string(&path).expect("config");
    assert!(text.contains("[dashboard]\npublic = false"));
    assert!(!config_mutation::read_dashboard_public(&path).expect("updated"));
}

#[test]
fn key_rotation_is_random_and_env_owned_keys_are_not_replaced_inline() {
    let directory = tempdir().expect("temporary directory");
    let inline = directory.path().join("inline.toml");
    fs::write(&inline, base_config()).expect("config");
    let first = config_mutation::generate_key().expect("key");
    let second = config_mutation::generate_key().expect("key");
    assert_eq!(first.len(), 64);
    assert_ne!(first, second);
    assert!(config_mutation::write_server_key(&inline, &first).expect("write key"));
    assert_eq!(
        config_mutation::read_server_key(&inline).expect("read key"),
        Some(first)
    );

    let env_owned = directory.path().join("env.toml");
    fs::write(
        &env_owned,
        "[server]\nhost = \"127.0.0.1\"\nport = 11300\napi_key_env = \"O004_TEST_KEY\"\n",
    )
    .expect("config");
    assert!(!config_mutation::write_server_key(&env_owned, &second).expect("env-owned key"));
    assert!(
        !fs::read_to_string(&env_owned)
            .expect("config")
            .contains("api_key =")
    );
}

#[test]
fn independent_config_mutations_do_not_share_a_process_wide_busy_gate() {
    let directory = tempdir().expect("temporary directory");
    let paths = (0..8)
        .map(|index| {
            let path = directory.path().join(format!("config-{index}.toml"));
            fs::write(&path, base_config()).expect("config");
            path
        })
        .collect::<Vec<_>>();
    let workers = paths
        .into_iter()
        .map(|path| {
            thread::spawn(move || config_mutation::set_server_value(&path, "host", "0.0.0.0"))
        })
        .collect::<Vec<_>>();
    for worker in workers {
        assert!(worker.join().expect("mutation thread").expect("mutation"));
    }
}

#[test]
fn bundled_and_custom_templates_are_structurally_loaded() {
    let bundled = config_mutation::load_provider_templates(None).expect("bundled templates");
    assert!(bundled.contains_key("opencode-go"));
    assert!(bundled.contains_key("ollama-local"));

    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("providers.toml");
    fs::write(
        &path,
        "[providers.example]\nbase_url = \"https://example.invalid/v1\"\nprotocols = [\"openai\"]\nstatus = \"experimental\"\n",
    )
    .expect("template");
    let custom = config_mutation::load_provider_templates(Some(&path)).expect("custom template");
    assert!(custom.contains_key("example"));
    assert!(custom.contains_key("opencode-go"));
}

#[test]
fn init_config_uses_the_repository_canonical_example() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");

    assert!(config_mutation::init_config(&path, false).expect("initialize config"));
    let generated = fs::read_to_string(&path).expect("generated config");
    assert_eq!(generated, include_str!("../config.example.toml"));
    Config::from_toml_bytes(&path, generated.as_bytes()).expect("canonical config parses");
}

#[test]
fn canonical_lan_and_dashboard_defaults_agree_across_helpers() {
    assert_eq!(Config::default().server.host, "0.0.0.0");
    assert!(Config::default().dashboard.public);

    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");
    // A missing `[dashboard].public` means the public read-only dashboard in
    // both the typed config and the text mutation helper.
    assert!(config_mutation::read_dashboard_public(&path).expect("default"));
    let typed = Config::from_toml_bytes(&path, fs::read(&path).expect("config").as_slice())
        .expect("typed config");
    assert!(typed.dashboard.public);

    // An explicit opt-out survives and is never rewritten unexpectedly.
    config_mutation::set_dashboard_public(&path, Some(false)).expect("dashboard edit");
    assert!(!config_mutation::read_dashboard_public(&path).expect("updated"));
    let typed = Config::from_toml_bytes(&path, fs::read(&path).expect("config").as_slice())
        .expect("typed config");
    assert!(!typed.dashboard.public);
}

#[test]
fn dashboard_public_toggle_keeps_restart_required_semantics() {
    use eggpool::config_reload_policy::ReloadDisposition;

    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");

    let mutation = config_mutation::set_dashboard_public_with_transition(&path, Some(false))
        .expect("dashboard edit");
    assert!(!mutation.value || mutation.transition.has_restart_required());
    assert_eq!(
        disposition_for("dashboard.public"),
        ReloadDisposition::RestartRequired
    );
    assert!(
        mutation.transition.has_restart_required(),
        "dashboard visibility changes apply on restart, like before"
    );
    let back = config_mutation::set_dashboard_public_with_transition(&path, Some(true))
        .expect("dashboard edit");
    assert!(back.transition.has_restart_required());
    assert!(config_mutation::read_dashboard_public(&path).expect("restored"));
}

#[test]
fn init_config_writes_the_canonical_lan_and_public_dashboard_values() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    assert!(config_mutation::init_config(&path, false).expect("initialize config"));
    let generated = fs::read_to_string(&path).expect("generated config");
    assert!(generated.contains("host = \"0.0.0.0\""));
    let typed = Config::from_toml_bytes(&path, generated.as_bytes()).expect("parses");
    assert_eq!(typed.server.host, "0.0.0.0");
    assert!(typed.dashboard.public);
    assert!(config_mutation::read_dashboard_public(&path).expect("public"));
}

#[test]
fn server_threads_is_a_restart_required_compatibility_field() {
    assert_eq!(Config::default().server.threads, 1);
    assert_eq!(
        disposition_for("server.threads"),
        ReloadDisposition::RestartRequired
    );
}

#[test]
fn mutation_carries_canonical_disposition_before_apply() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");

    let restart = config_mutation::set_server_value_with_transition(&path, "host", "0.0.0.0")
        .expect("host edit");
    assert!(restart.value);
    assert_eq!(
        restart.transition.restart_required_paths(),
        vec!["server.host".to_owned()]
    );

    let noop = config_mutation::set_server_value_with_transition(&path, "host", "0.0.0.0")
        .expect("same host edit");
    assert!(noop.transition.is_noop());
}

#[test]
fn mutation_transition_debug_never_contains_secret_values() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");
    let secret = "o004-transition-secret";

    let mutation =
        config_mutation::write_server_key_with_transition(&path, secret).expect("key edit");
    let debug = format!("{:?}", mutation.transition);
    assert!(!debug.contains(secret));
    assert_eq!(
        mutation.transition.restart_required_paths(),
        vec!["server.api_key".to_owned()]
    );
}

#[test]
fn account_matching_is_secret_safe_and_apply_outcome_is_typed() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(
        &path,
        concat!(
            "[providers.example]\n",
            "id = \"example\"\n",
            "base_url = \"https://example.invalid/v1\"\n",
            "protocols = [\"openai\"]\n",
            "[[providers.example.accounts]]\n",
            "name = \"example-0001\"\n",
            "api_key = \"synthetic-secret\"\n"
        ),
    )
    .expect("config");
    let matches = config_mutation::matching_accounts(&path, Some("example")).expect("matches");
    assert_eq!(matches.len(), 1);
    assert_eq!(matches[0].name, "example-0001");
    assert_eq!(
        config_mutation::redact_key("synthetic-secret"),
        "synt...cret"
    );
    let debug = format!("{:?}", matches[0]);
    assert!(!debug.contains("synthetic-secret"));
    assert!(debug.contains("synt...cret"));
    assert_eq!(ApplyMode::LiveOrReport, ApplyMode::LiveOrReport);
    assert_ne!(
        ApplyOutcome::ServerNotRunning,
        ApplyOutcome::ControlUnavailable
    );
}

// Provider-profile metadata M001 (reviewed 2026-10-02): bundled templates are
// bootstrap facts whose authority is current first-party provider
// documentation, never a sibling repository. Together stays on the canonical
// `https://api.together.ai/v1` (`docs.together.ai`); `api.together.xyz/v1`
// is a legacy alias from the old migration page. OpenCode Go stays on
// `https://opencode.ai/zen/go/v1`.

const BUNDLED_TEMPLATES_TEXT: &str = provider_profile::BUNDLED_PROVIDER_PROFILES;

fn bundled_provider_ids() -> Vec<String> {
    config_mutation::load_provider_templates(None)
        .expect("bundled templates")
        .into_keys()
        .collect()
}

fn bundled_provider_table(id: &str) -> toml::map::Map<String, toml::Value> {
    let root: toml::Value = BUNDLED_TEMPLATES_TEXT.parse().expect("templates parse");
    root.get("providers")
        .and_then(toml::Value::as_table)
        .and_then(|providers| providers.get(id))
        .and_then(toml::Value::as_table)
        .unwrap_or_else(|| panic!("bundled template present for {id}"))
        .clone()
}

fn template_str(table: &toml::map::Map<String, toml::Value>, key: &str) -> String {
    table
        .get(key)
        .and_then(toml::Value::as_str)
        .unwrap_or_else(|| panic!("template key present: {key}"))
        .to_owned()
}

/// Join a configured base URL and path the way provider dispatch does: one
/// separator, no version-segment duplication, no missing prefix.
fn compose_url(base: &str, path: &str) -> String {
    format!(
        "{}/{}",
        base.trim_end_matches('/'),
        path.trim_start_matches('/')
    )
}

#[test]
fn bundled_templates_parse_with_stable_ids_and_well_formed_endpoints() {
    let templates = config_mutation::load_provider_templates(None).expect("bundled templates");
    for id in [
        "opencode-go",
        "minimax",
        "minimax-cn",
        "openrouter",
        "ollama-local",
        "lmstudio-local",
        "llamacpp-local",
        "vllm-local",
        "localai-local",
        "custom-compatible",
        "openai",
        "anthropic",
        "groq",
        "deepinfra",
        "gemini",
        "gemini-native",
        "xai",
        "mistral",
        "siliconflow",
        "deepseek",
        "together",
        "fireworks",
        "alibaba",
    ] {
        let template = templates
            .get(id)
            .unwrap_or_else(|| panic!("bundled template present for {id}"));
        assert_eq!(template.id, id);
        assert!(
            template.url.starts_with("http://") || template.url.starts_with("https://"),
            "{id} carries an absolute http(s) base URL"
        );
        assert!(
            !template.url.ends_with('/'),
            "{id} base URL carries no trailing slash"
        );
    }

    // Every bundled entry declares a known auth shape; unknown modes fail
    // closed here instead of at first provider contact.
    let root: toml::Value = BUNDLED_TEMPLATES_TEXT.parse().expect("templates parse");
    let providers = root
        .get("providers")
        .and_then(toml::Value::as_table)
        .expect("providers table");
    for (id, raw) in providers {
        let table = raw.as_table().unwrap_or_else(|| panic!("{id} is a table"));
        let mode = table
            .get("auth")
            .and_then(toml::Value::as_table)
            .and_then(|auth| auth.get("mode"))
            .and_then(toml::Value::as_str)
            .unwrap_or("bearer");
        assert!(
            matches!(mode, "bearer" | "api_key" | "none"),
            "{id} declares a known auth mode"
        );
        assert!(
            table
                .get("protocols")
                .and_then(toml::Value::as_array)
                .is_some_and(|protocols| !protocols.is_empty()),
            "{id} declares at least one protocol"
        );
    }
}

#[test]
fn together_template_retains_first_party_ai_endpoint() {
    let templates = config_mutation::load_provider_templates(None).expect("bundled templates");
    let template = templates.get("together").expect("together template");
    assert_eq!(template.id, "together");
    // Canonical first-party value per docs.together.ai (reviewed 2026-10-02).
    // The api.together.xyz host is a legacy alias, not the correction target.
    assert_eq!(template.url, "https://api.together.ai/v1");

    let table = bundled_provider_table("together");
    assert_eq!(
        template_str(&table, "base_url"),
        "https://api.together.ai/v1"
    );
    let auth = table
        .get("auth")
        .and_then(toml::Value::as_table)
        .expect("together auth");
    assert_eq!(
        auth.get("mode").and_then(toml::Value::as_str),
        Some("bearer")
    );

    // Default OpenAI-compatible surfaces compose without duplicating `/v1`.
    let chat = compose_url(&template.url, "/chat/completions");
    let models = compose_url(&template.url, "/models");
    assert_eq!(chat, "https://api.together.ai/v1/chat/completions");
    assert_eq!(models, "https://api.together.ai/v1/models");
    assert!(!chat.contains("/v1/v1"));
    assert!(!models.contains("/v1/v1"));
    assert!(!template.url.contains("together.xyz"));
}

#[test]
fn opencode_go_template_retains_zen_go_prefix_and_wire_surfaces() {
    let templates = config_mutation::load_provider_templates(None).expect("bundled templates");
    let template = templates.get("opencode-go").expect("opencode-go template");
    // Current first-party value per opencode.ai/docs/go (reviewed 2026-10-02).
    assert_eq!(template.url, "https://opencode.ai/zen/go/v1");

    let table = bundled_provider_table("opencode-go");
    let surfaces = table
        .get("wire_surfaces")
        .and_then(toml::Value::as_table)
        .expect("opencode-go wire surfaces");
    let surface_path = |surface: &str| {
        surfaces
            .get(surface)
            .and_then(toml::Value::as_table)
            .and_then(|entry| entry.get("path_template"))
            .and_then(toml::Value::as_str)
            .unwrap_or_else(|| panic!("{surface} path template"))
            .to_owned()
    };
    assert_eq!(surface_path("openai_chat_completions"), "/chat/completions");
    assert_eq!(surface_path("openai_responses"), "/responses");
    assert_eq!(surface_path("anthropic_messages"), "/messages");

    // Every configured surface composes under the /zen/go/v1 prefix.
    for (surface, expected) in [
        (
            "openai_chat_completions",
            "https://opencode.ai/zen/go/v1/chat/completions",
        ),
        (
            "openai_responses",
            "https://opencode.ai/zen/go/v1/responses",
        ),
        (
            "anthropic_messages",
            "https://opencode.ai/zen/go/v1/messages",
        ),
    ] {
        let composed = compose_url(&template.url, &surface_path(surface));
        assert_eq!(composed, expected);
        assert!(composed.contains("/zen/go/v1/"));
    }
    // Model discovery uses the default OpenAI-shape path under the prefix.
    assert_eq!(
        compose_url(&template.url, "/models"),
        "https://opencode.ai/zen/go/v1/models"
    );
}

#[test]
fn touched_provider_configs_construct_and_validate() {
    use std::path::Path;
    // Build minimal configs from the reviewed template facts so template
    // drift breaks here before it can reach provider dispatch.
    let together = concat!(
        "[providers.together]\n",
        "id = \"together\"\n",
        "base_url = \"https://api.together.ai/v1\"\n",
        "protocols = [\"openai\"]\n",
        "[providers.together.auth]\n",
        "mode = \"bearer\"\n",
        "[providers.together.verify]\n",
        "probe_model = \"meta-llama/Llama-3.3-70B-Instruct-Turbo\"\n",
        "probe_protocol = \"openai\"\n",
        "require_models = true\n",
    );
    let config = Config::from_toml_bytes(Path::new("together.toml"), together.as_bytes())
        .expect("together config parses");
    let provider = config.providers.get("together").expect("together provider");
    assert_eq!(provider.base_url, "https://api.together.ai/v1");
    assert_eq!(provider.auth.mode, "bearer");

    let opencode_go = concat!(
        "[providers.opencode-go]\n",
        "id = \"opencode-go\"\n",
        "base_url = \"https://opencode.ai/zen/go/v1\"\n",
        "protocols = [\"openai\", \"anthropic\"]\n",
        "[providers.opencode-go.auth]\n",
        "mode = \"bearer\"\n",
        "[providers.opencode-go.wire_surfaces.openai_chat_completions]\n",
        "path_template = \"/chat/completions\"\n",
        "priority = 100\n",
        "[providers.opencode-go.wire_surfaces.openai_responses]\n",
        "path_template = \"/responses\"\n",
        "priority = 90\n",
        "[providers.opencode-go.wire_surfaces.anthropic_messages]\n",
        "path_template = \"/messages\"\n",
        "priority = 100\n",
        "[providers.opencode-go.verify]\n",
        "probe_model = \"gpt-5.5-mini\"\n",
        "probe_protocol = \"openai\"\n",
        "require_models = true\n",
    );
    let config = Config::from_toml_bytes(Path::new("opencode-go.toml"), opencode_go.as_bytes())
        .expect("opencode-go config parses");
    let provider = config
        .providers
        .get("opencode-go")
        .expect("opencode-go provider");
    assert_eq!(provider.base_url, "https://opencode.ai/zen/go/v1");
    assert_eq!(
        provider
            .wire_surfaces
            .get("openai_chat_completions")
            .expect("chat surface")
            .path_template,
        "/chat/completions"
    );
    assert_eq!(
        provider
            .wire_surfaces
            .get("openai_responses")
            .expect("responses surface")
            .path_template,
        "/responses"
    );
    assert_eq!(
        provider
            .wire_surfaces
            .get("anthropic_messages")
            .expect("messages surface")
            .path_template,
        "/messages"
    );
}

#[test]
fn representative_template_path_compositions_stay_exact() {
    // Guards the composition shapes most likely to hide drift: DeepSeek's
    // `/v1`-less base, MiniMax's Anthropic subpath base, and Alibaba's
    // compatible-mode prefix.
    let cases = [
        (
            "deepseek",
            "https://api.deepseek.com",
            "/chat/completions",
            "https://api.deepseek.com/chat/completions",
        ),
        (
            "deepseek",
            "https://api.deepseek.com",
            "/models",
            "https://api.deepseek.com/models",
        ),
        (
            "minimax",
            "https://api.minimax.io/anthropic",
            "/v1/messages",
            "https://api.minimax.io/anthropic/v1/messages",
        ),
        (
            "minimax",
            "https://api.minimax.io/anthropic",
            "/v1/models",
            "https://api.minimax.io/anthropic/v1/models",
        ),
        (
            "alibaba",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "/chat/completions",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
    ];
    for (id, base, path, expected) in cases {
        let table = bundled_provider_table(id);
        assert_eq!(template_str(&table, "base_url"), base);
        assert_eq!(compose_url(base, path), expected);
    }
}

/// Provider-profile contract cutover parity. The bundled document is owned by
/// `eggpool-provider-profile`; EggPool consumes it through `provider_profile`
/// and projects it back onto the runtime configuration shape. These tests
/// compare that projection against the canonical document parsed directly, so
/// the extraction cannot silently change effective provider configuration.
mod shared_profile_parity {
    use super::{BUNDLED_TEMPLATES_TEXT, provider_profile};
    use eggpool::{Config, operations::config_mutation};
    use std::path::Path;

    fn load(document: &toml::Table) -> Config {
        let text = toml::to_string(document).expect("document renders");
        Config::from_toml_bytes(Path::new("config.toml"), text.as_bytes())
            .expect("document loads as configuration")
    }

    fn normalized(document: &toml::Table) -> std::collections::BTreeMap<String, String> {
        load(document)
            .providers
            .into_iter()
            .map(|(id, provider)| {
                (
                    id,
                    toml::to_string(&provider).expect("provider config renders"),
                )
            })
            .collect()
    }

    #[test]
    fn shared_profile_projection_matches_the_canonical_document() {
        let canonical = normalized(
            &provider_profile::canonical_config_document().expect("bundled document parses"),
        );
        let projected = normalized(
            &provider_profile::projected_config_document().expect("shared profiles project"),
        );
        assert!(!canonical.is_empty(), "canonical document has providers");
        assert_eq!(
            projected.keys().collect::<std::collections::BTreeSet<_>>(),
            canonical.keys().collect::<std::collections::BTreeSet<_>>(),
            "the extraction neither adds nor drops a bundled provider"
        );
        for (id, expected) in &canonical {
            assert_eq!(
                projected.get(id).map(String::as_str),
                Some(expected.as_str()),
                "{id} projects onto an identical runtime provider configuration"
            );
        }
    }

    #[test]
    fn the_bundled_document_comes_from_the_shared_contract() {
        assert_eq!(
            provider_profile::BUNDLED_PROVIDER_PROFILES,
            eggpool_provider_profile::EMBEDDED_PROVIDER_PROFILES_TOML
        );
        assert_eq!(
            BUNDLED_TEMPLATES_TEXT,
            eggpool_provider_profile::EMBEDDED_PROVIDER_PROFILES_TOML
        );
        assert_eq!(
            provider_profile::bundled_profiles()
                .expect("shared profiles parse")
                .len(),
            super::bundled_provider_ids().len()
        );
    }

    #[test]
    fn bundled_provider_data_carries_no_credential_material() {
        let document: toml::Value = BUNDLED_TEMPLATES_TEXT
            .parse()
            .expect("bundled document is valid TOML");
        let providers = document
            .get("providers")
            .and_then(toml::Value::as_table)
            .expect("bundled document declares providers");
        for (id, raw) in providers {
            let table = raw.as_table().expect("provider entry is a table");
            for key in ["api_key", "api_key_env", "value_env", "secret", "password"] {
                assert!(!table.contains_key(key), "{id} declares no {key} field");
            }
            for (scope, table) in [
                ("provider", table.clone()),
                (
                    "verify",
                    table
                        .get("verify")
                        .and_then(toml::Value::as_table)
                        .cloned()
                        .unwrap_or_default(),
                ),
            ] {
                assert!(
                    !table.contains_key("models_require_authentication") || scope == "verify",
                    "{id} keeps contract-only verification fields inside [verify]"
                );
            }
        }
        // The runtime-facing loader exposes no credential field either.
        assert_eq!(
            config_mutation::load_provider_templates(None)
                .expect("bundled templates")
                .len(),
            providers.len()
        );
        let profiles = provider_profile::bundled_profiles().expect("shared profiles parse");
        for profile in profiles.profiles() {
            assert!(
                eggpool_provider_profile::valid_header_name(&profile.auth.header),
                "{} declares a valid credential header name",
                profile.id
            );
            assert!(
                !profile.auth.scheme.contains(char::is_whitespace),
                "{} auth scheme is a single token",
                profile.id
            );
            for surface in profile
                .surface_auth(eggpool_wire::profile::WireSurface::AnthropicMessages)
                .into_iter()
                .chain(
                    profile.surface_auth(eggpool_wire::profile::WireSurface::OpenaiChatCompletions),
                )
            {
                assert!(
                    eggpool_provider_profile::valid_header_name(&surface.header),
                    "{} surface declares a valid credential header name",
                    profile.id
                );
            }
        }
    }

    #[test]
    fn opencode_go_hints_reach_the_runtime_configuration_as_preferences() {
        let document =
            provider_profile::canonical_config_document().expect("bundled document parses");
        let config = load(&document);
        let provider = config
            .providers
            .get("opencode-go")
            .expect("OpenCode Go is bundled");
        assert_eq!(
            provider.model_wire.len(),
            30,
            "every reviewed OpenCode Go model carries an exact hint"
        );
        for (model, preference) in &provider.model_wire {
            assert!(
                !preference.fixed,
                "{model} stays advisory so the runtime can renegotiate"
            );
            assert!(
                provider
                    .wire_surfaces
                    .contains_key(&preference.preferred_surface),
                "{model} names a surface the provider serves"
            );
        }
        assert_eq!(
            provider
                .model_wire
                .get("gpt-6-luna")
                .map(|preference| preference.preferred_surface.as_str()),
            Some("openai_responses")
        );
        assert_eq!(
            provider
                .model_wire
                .get("minimax-m3")
                .map(|preference| preference.preferred_surface.as_str()),
            Some("anthropic_messages")
        );
    }

    #[test]
    fn explicit_operator_configuration_overrides_bundled_defaults() {
        // Bundled profiles are bootstrap facts. An operator block that differs
        // from the shared profile must still win under current semantics.
        let mut document =
            provider_profile::canonical_config_document().expect("bundled document parses");
        let providers = document
            .get_mut("providers")
            .and_then(toml::Value::as_table_mut)
            .expect("providers table");
        let opencode = providers
            .get_mut("opencode-go")
            .and_then(toml::Value::as_table_mut)
            .expect("opencode-go entry");
        opencode.insert(
            "base_url".into(),
            toml::Value::String("https://operator.example/v1".into()),
        );
        let config = load(&document);
        let provider = config
            .providers
            .get("opencode-go")
            .expect("opencode-go is configured");
        assert_eq!(provider.base_url, "https://operator.example/v1");
        assert_eq!(
            provider
                .model_wire
                .get("gpt-6-luna")
                .map(|preference| preference.preferred_surface.as_str()),
            Some("openai_responses"),
            "operator configuration keeps its own preferences"
        );
        assert_eq!(
            provider.anthropic_path, "/messages",
            "the operator keeps the derived surface path and the new base URL"
        );
        assert_eq!(
            format!(
                "{}/{}",
                provider.base_url.trim_end_matches('/'),
                provider.anthropic_path.trim_start_matches('/')
            ),
            "https://operator.example/v1/messages",
            "an overridden base URL is what dispatch composes against"
        );
    }

    #[test]
    fn a_profile_without_a_hint_leaves_the_runtime_preference_map_empty() {
        let document =
            provider_profile::canonical_config_document().expect("bundled document parses");
        let config = load(&document);
        let provider = config
            .providers
            .get("together")
            .expect("together is bundled");
        assert!(
            provider.model_wire.is_empty(),
            "a profile with no reviewed hint contributes no preference"
        );
    }
}
