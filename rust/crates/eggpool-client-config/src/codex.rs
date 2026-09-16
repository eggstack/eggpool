//! Portable Codex rendering, catalog, validation, and TOML mutation.
//!
//! All renderers are secret-free: the generated TOML references
//! `EGGPOOL_API_KEY` by name and never embeds a resolved credential. The
//! catalog is built from the conservative provider-neutral projection with
//! deterministic ordering, bounded size, no secrets, and no provider-private
//! source metadata.

use serde_json::{json, Map, Value};

use crate::error::ClientConfigError;
use crate::projection::AgentModelProjection;
use crate::text::{
    find_root_key, find_table, remove_root_key, set_root_key, table_value, toml_string_value,
};

/// Environment-variable name referenced (never embedded) by Codex output.
pub const EGGPOOL_API_KEY_ENV: &str = "EGGPOOL_API_KEY";
/// Deterministic bound for generated catalog entries.
pub const MAX_CODEX_CATALOG_MODELS: usize = 1000;
/// Deterministic bound for rendered catalog bytes.
pub const MAX_CODEX_CATALOG_BYTES: usize = 1024 * 1024;

/// Render a Codex TOML snippet for `base_url` without a catalog pointer.
pub fn render_codex_toml(base_url: &str, model: Option<&str>) -> Result<String, ClientConfigError> {
    render_codex_toml_with_catalog(base_url, model, None)
}

/// Render a Codex TOML snippet, optionally pointing at a generated catalog.
pub fn render_codex_toml_with_catalog(
    base_url: &str,
    model: Option<&str>,
    catalog_path: Option<&str>,
) -> Result<String, ClientConfigError> {
    let mut lines = vec!["model_provider = \"eggpool\"".to_owned()];
    if let Some(model) = model {
        lines.push(format!("model = {}", toml_string(model)?));
    }
    if let Some(catalog_path) = catalog_path {
        lines.push(format!(
            "model_catalog_json = {}",
            toml_string(catalog_path)?
        ));
    }
    lines.extend([
        String::new(),
        "[model_providers.eggpool]".to_owned(),
        "name = \"EggPool\"".to_owned(),
        format!("base_url = {}", toml_string(base_url)?),
        "wire_api = \"responses\"".to_owned(),
        "supports_websockets = false".to_owned(),
        format!("env_key = {}", toml_string(EGGPOOL_API_KEY_ENV)?),
    ]);
    Ok(lines.join("\n"))
}

fn toml_string(value: &str) -> Result<String, ClientConfigError> {
    Ok(serde_json::to_string(value)?)
}

/// Render a deterministic, bounded, sanitized Codex catalog from projections.
///
/// Each entry carries the EggPool public ID as `slug`, display name,
/// conservative context facts, reasoning efforts where guaranteed,
/// `shell_type = "shell_command"`, `visibility = "list"`,
/// `supported_in_api = true`, `prefer_websockets = false`, and a 90%
/// `auto_compact_token_limit` derived from the guaranteed context window.
/// Every entry always emits `supported_reasoning_levels` (empty when no
/// reasoning is guaranteed) and `base_instructions = ""` (no EggPool
/// override); both are required by current Codex strict parsing.
pub fn build_codex_catalog_json(
    projections: &[AgentModelProjection],
) -> Result<String, ClientConfigError> {
    if projections.len() > MAX_CODEX_CATALOG_MODELS {
        return Err(ClientConfigError::CatalogTooLarge {
            count: projections.len(),
        });
    }
    let mut sorted = projections.to_vec();
    sorted.sort_by(|left, right| left.public_id.cmp(&right.public_id));
    let mut models = Vec::with_capacity(sorted.len());
    for projection in &sorted {
        models.push(codex_catalog_entry(projection));
    }
    let root = json!({ "models": models });
    let rendered = serde_json::to_string_pretty(&root)?;
    if rendered.len() > MAX_CODEX_CATALOG_BYTES {
        return Err(ClientConfigError::DocumentTooLarge);
    }
    Ok(rendered)
}

fn codex_catalog_entry(projection: &AgentModelProjection) -> Value {
    let capabilities = &projection.capabilities;
    let mut entry = Map::new();
    entry.insert(
        "slug".to_owned(),
        Value::String(projection.public_id.clone()),
    );
    entry.insert(
        "display_name".to_owned(),
        Value::String(projection.display_name.clone()),
    );
    entry.insert(
        "description".to_owned(),
        Value::String(format!(
            "EggPool model {} via EggPool Responses gateway",
            projection.public_id
        )),
    );
    entry.insert(
        "shell_type".to_owned(),
        Value::String("shell_command".to_owned()),
    );
    entry.insert("visibility".to_owned(), Value::String("list".to_owned()));
    entry.insert("supported_in_api".to_owned(), Value::Bool(true));
    entry.insert("priority".to_owned(), json!(100));
    entry.insert("prefer_websockets".to_owned(), Value::Bool(false));
    entry.insert(
        "supports_parallel_tool_calls".to_owned(),
        Value::Bool(capabilities.function_tools == Some(true)),
    );
    entry.insert(
        "experimental_supported_tools".to_owned(),
        Value::Array(Vec::new()),
    );
    let mut modalities = vec![Value::String("text".to_owned())];
    if capabilities.input_images == Some(true) {
        modalities.push(Value::String("image".to_owned()));
    }
    entry.insert("input_modalities".to_owned(), Value::Array(modalities));
    entry.insert(
        "supports_image_detail_original".to_owned(),
        Value::Bool(false),
    );
    if let Some(context_window) = capabilities.context_tokens {
        entry.insert("context_window".to_owned(), json!(context_window));
        entry.insert("max_context_window".to_owned(), json!(context_window));
        let auto_compact = context_window * 90 / 100;
        entry.insert("auto_compact_token_limit".to_owned(), json!(auto_compact));
    } else {
        entry.insert("auto_compact_token_limit".to_owned(), Value::Null);
    }
    entry.insert(
        "truncation_policy".to_owned(),
        json!({ "mode": "tokens", "limit": 10000 }),
    );
    if let Some(reasoning) = &capabilities.reasoning {
        entry.insert(
            "supports_reasoning_summaries".to_owned(),
            Value::Bool(false),
        );
        entry.insert(
            "default_reasoning_summary".to_owned(),
            Value::String("none".to_owned()),
        );
        if reasoning.efforts.is_empty() {
            entry.insert(
                "supported_reasoning_levels".to_owned(),
                Value::Array(Vec::new()),
            );
        } else {
            let levels: Vec<Value> = reasoning
                .efforts
                .iter()
                .map(|effort| {
                    json!({
                        "effort": effort,
                        "description": format!("EggPool reasoning effort {effort}"),
                    })
                })
                .collect();
            entry.insert(
                "supported_reasoning_levels".to_owned(),
                Value::Array(levels),
            );
            if let Some(default) = reasoning
                .default_effort
                .as_ref()
                .filter(|default| reasoning.efforts.contains(default))
            {
                entry.insert(
                    "default_reasoning_level".to_owned(),
                    Value::String(default.clone()),
                );
            } else if let Some(first) = reasoning.efforts.first() {
                entry.insert(
                    "default_reasoning_level".to_owned(),
                    Value::String(first.clone()),
                );
            }
        }
    } else {
        entry.insert(
            "supports_reasoning_summaries".to_owned(),
            Value::Bool(false),
        );
        entry.insert(
            "default_reasoning_summary".to_owned(),
            Value::String("none".to_owned()),
        );
        entry.insert(
            "supported_reasoning_levels".to_owned(),
            Value::Array(Vec::new()),
        );
    }
    entry.insert("support_verbosity".to_owned(), Value::Bool(false));
    entry.insert("base_instructions".to_owned(), Value::String(String::new()));
    Value::Object(entry)
}

/// Validate that a generated catalog meets the strict-parser contract.
///
/// Every entry must carry a slug/display name, must not advertise websockets,
/// and must not contain credentials or raw source blobs.
pub fn validate_codex_catalog_json(
    catalog_json: &str,
    api_key: &str,
) -> Result<usize, ClientConfigError> {
    let value: Value = serde_json::from_str(catalog_json)?;
    let models = value
        .get("models")
        .and_then(Value::as_array)
        .ok_or_else(|| ClientConfigError::Drift {
            detail: "Codex catalog is missing the models array".to_owned(),
        })?;
    for model in models {
        let object = model.as_object().ok_or_else(|| ClientConfigError::Drift {
            detail: "Codex catalog entry is not an object".to_owned(),
        })?;
        if object
            .get("slug")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .is_none()
        {
            return Err(ClientConfigError::Drift {
                detail: "Codex catalog entry is missing slug".to_owned(),
            });
        }
        if object
            .get("display_name")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .is_none()
        {
            return Err(ClientConfigError::Drift {
                detail: "Codex catalog entry is missing display_name".to_owned(),
            });
        }
        if object.get("prefer_websockets").and_then(Value::as_bool) == Some(true) {
            return Err(ClientConfigError::Drift {
                detail: "Codex catalog must not advertise websockets".to_owned(),
            });
        }
        if object
            .get("supported_reasoning_levels")
            .and_then(Value::as_array)
            .is_none()
        {
            return Err(ClientConfigError::Drift {
                detail: "Codex catalog entry is missing supported_reasoning_levels".to_owned(),
            });
        }
        let has_base_instructions = object
            .get("base_instructions")
            .and_then(Value::as_str)
            .is_some();
        let has_template_instructions = object
            .get("model_messages")
            .and_then(|messages| messages.get("instructions_template"))
            .and_then(Value::as_str)
            .is_some();
        if !has_base_instructions && !has_template_instructions {
            return Err(ClientConfigError::Drift {
                detail: "Codex catalog entry is missing base_instructions".to_owned(),
            });
        }
    }
    if !api_key.is_empty() && catalog_json.contains(api_key) {
        return Err(ClientConfigError::Drift {
            detail: "Codex catalog must not contain the server key".to_owned(),
        });
    }
    Ok(models.len())
}

/// Previous EggPool-owned Codex values captured before mutation.
#[derive(Debug, Clone, Default)]
pub struct CodexPrevious {
    pub model_provider: Option<String>,
    pub model_catalog_json: Option<String>,
    pub model: Option<String>,
    pub provider_table: Option<Vec<String>>,
}

/// Apply the narrow EggPool-owned Codex mutation to TOML text.
///
/// Owns only root `model_provider`, root `model_catalog_json`, optional root
/// `model` (when explicitly requested), and the `[model_providers.eggpool]`
/// table. All other content, comments, and formatting are preserved. Root
/// keys are placed before the first table so they remain root assignments.
pub fn apply_codex_text_mutation(
    existing: &str,
    base_url: &str,
    catalog_path: &str,
    model: Option<&str>,
    manage_model: bool,
) -> (String, CodexPrevious) {
    let mut lines: Vec<String> = if existing.is_empty() {
        Vec::new()
    } else {
        existing.lines().map(str::to_owned).collect()
    };
    let mut previous = CodexPrevious::default();
    if let Some(index) = find_root_key(&lines, "model_provider") {
        previous.model_provider = toml_string_value(&lines[index]);
    }
    if let Some(index) = find_root_key(&lines, "model_catalog_json") {
        previous.model_catalog_json = toml_string_value(&lines[index]);
    }
    if let Some(index) = find_root_key(&lines, "model") {
        previous.model = toml_string_value(&lines[index]);
    }
    if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
        previous.provider_table = Some(lines[start..end].to_vec());
    }

    let catalog_rendered =
        serde_json::to_string(catalog_path).unwrap_or_else(|_| "\"\"".to_owned());
    set_root_key(&mut lines, "model_provider", "\"eggpool\"");
    set_root_key(&mut lines, "model_catalog_json", &catalog_rendered);
    if manage_model {
        if let Some(model) = model {
            let rendered = serde_json::to_string(model).unwrap_or_else(|_| "\"\"".to_owned());
            set_root_key(&mut lines, "model", &rendered);
        } else if find_root_key(&lines, "model").is_some() {
            remove_root_key(&mut lines, "model");
        }
    }

    let base_rendered = serde_json::to_string(base_url).unwrap_or_else(|_| "\"\"".to_owned());
    let table_lines = vec![
        "[model_providers.eggpool]".to_owned(),
        "name = \"EggPool\"".to_owned(),
        format!("base_url = {base_rendered}"),
        "wire_api = \"responses\"".to_owned(),
        "supports_websockets = false".to_owned(),
        format!(
            "env_key = {}",
            serde_json::to_string(EGGPOOL_API_KEY_ENV).expect("env key")
        ),
    ];
    if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
        lines.splice(start..end, table_lines);
    } else {
        if !lines.is_empty() && !lines.last().is_some_and(String::is_empty) {
            lines.push(String::new());
        }
        lines.extend(table_lines);
    }

    let mut text = lines.join("\n");
    if !text.is_empty() && !text.ends_with('\n') {
        text.push('\n');
    }
    (text, previous)
}

/// Remove EggPool-owned Codex fields, restoring previous values where known.
pub fn remove_codex_owned_text(
    existing: &str,
    previous: &CodexPrevious,
    owned_model: bool,
) -> String {
    let mut lines: Vec<String> = if existing.is_empty() {
        Vec::new()
    } else {
        existing.lines().map(str::to_owned).collect()
    };
    match &previous.model_provider {
        Some(value) if !value.is_empty() && value != "eggpool" => {
            set_root_key(
                &mut lines,
                "model_provider",
                &serde_json::to_string(value).expect("toml"),
            );
        }
        _ => {
            remove_root_key(&mut lines, "model_provider");
        }
    }
    match &previous.model_catalog_json {
        Some(value) if !value.is_empty() => {
            set_root_key(
                &mut lines,
                "model_catalog_json",
                &serde_json::to_string(value).expect("toml"),
            );
        }
        _ => {
            remove_root_key(&mut lines, "model_catalog_json");
        }
    }
    if owned_model {
        match &previous.model {
            Some(value) if !value.is_empty() => {
                set_root_key(
                    &mut lines,
                    "model",
                    &serde_json::to_string(value).expect("toml"),
                );
            }
            _ => {
                remove_root_key(&mut lines, "model");
            }
        }
    }
    if let Some(table) = &previous.provider_table {
        if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
            lines.splice(start..end, table.clone());
        }
    } else if let Some((start, end)) = find_table(&lines, "model_providers.eggpool") {
        lines.drain(start..end);
        if start < lines.len()
            && lines[start].trim().is_empty()
            && start > 0
            && lines[start - 1].trim().is_empty()
        {
            lines.remove(start);
        }
    }
    let mut text = lines.join("\n");
    if !text.is_empty() && !text.ends_with('\n') {
        text.push('\n');
    }
    text
}

/// Inputs for [`check_codex_state`] without filesystem access.
#[derive(Debug, Clone, Copy)]
pub struct CodexStateCheck<'a> {
    pub config_text: &'a str,
    pub base_url: &'a str,
    pub api_key: &'a str,
    pub model: Option<&'a str>,
    pub manage_model: bool,
    pub catalog_path: &'a str,
    pub catalog_exists: bool,
    pub catalog_current: bool,
}

/// Check managed Codex state without touching the filesystem.
///
/// Returns human-readable drift issues; an empty vector means current.
/// `catalog_exists` and `catalog_current` describe the generated catalog
/// artifact so the pure policy can report stale/missing catalogs.
pub fn check_codex_state(check: CodexStateCheck<'_>) -> Vec<String> {
    let config_text = check.config_text;
    let base_url = check.base_url;
    let api_key = check.api_key;
    let model = check.model;
    let manage_model = check.manage_model;
    let catalog_path = check.catalog_path;
    let catalog_exists = check.catalog_exists;
    let catalog_current = check.catalog_current;
    let mut issues = Vec::new();
    let lines: Vec<String> = if config_text.is_empty() {
        Vec::new()
    } else {
        config_text.lines().map(str::to_owned).collect()
    };
    let provider_is_eggpool = find_root_key(&lines, "model_provider")
        .and_then(|index| toml_string_value(&lines[index]))
        .as_deref()
        == Some("eggpool");
    if !provider_is_eggpool {
        issues.push("root model_provider is not \"eggpool\"".to_owned());
    }
    let catalog_matches = find_root_key(&lines, "model_catalog_json")
        .and_then(|index| toml_string_value(&lines[index]))
        .as_deref()
        == Some(catalog_path);
    if !catalog_matches {
        issues.push(format!(
            "root model_catalog_json does not point at {catalog_path}"
        ));
    }
    if manage_model {
        let current =
            find_root_key(&lines, "model").and_then(|index| toml_string_value(&lines[index]));
        if current.as_deref() != model {
            issues.push("root model selection differs from requested model".to_owned());
        }
    }
    let table = "model_providers.eggpool";
    if table_value(&lines, table, "name").as_deref() != Some("EggPool") {
        issues.push("[model_providers.eggpool] name differs".to_owned());
    }
    if table_value(&lines, table, "base_url").as_deref() != Some(base_url) {
        issues.push("[model_providers.eggpool] base_url differs".to_owned());
    }
    if table_value(&lines, table, "wire_api").as_deref() != Some("responses") {
        issues.push("[model_providers.eggpool] wire_api differs".to_owned());
    }
    if table_value(&lines, table, "supports_websockets").as_deref() != Some("false") {
        issues.push("[model_providers.eggpool] supports_websockets differs".to_owned());
    }
    if table_value(&lines, table, "env_key").as_deref() != Some(EGGPOOL_API_KEY_ENV) {
        issues.push("[model_providers.eggpool] env_key differs".to_owned());
    }
    if !catalog_exists {
        issues.push(format!("generated catalog {catalog_path} is missing"));
    } else if !catalog_current {
        issues.push("generated catalog content is stale".to_owned());
    }
    if !api_key.is_empty() && config_text.contains(api_key) {
        issues.push("client config embeds the resolved server key".to_owned());
    }
    issues
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::projection::{
        project_models, AgentModelCapabilities, IntegrationModel, ModelLimits,
    };
    use crate::text::{find_root_key, table_value, toml_string_value};
    use serde_json::{Map, Value};

    fn projections() -> Vec<AgentModelProjection> {
        let models = vec![IntegrationModel {
            model_id: "gpt-4o/openai".to_owned(),
            base_model_id: "gpt-4o".to_owned(),
            provider_id: Some("openai".to_owned()),
            display_name: "GPT-4o".to_owned(),
            capabilities: Value::Object(Map::new()),
            source_metadata: Value::Object(Map::new()),
            limits: ModelLimits {
                context_tokens: Some(128_000),
                ..Default::default()
            },
        }];
        project_models(&models)
    }

    fn root_value(snippet: &str, key: &str) -> Option<String> {
        let lines: Vec<String> = snippet.lines().map(str::to_owned).collect();
        find_root_key(&lines, key).and_then(|index| toml_string_value(&lines[index]))
    }

    fn table_lines(snippet: &str) -> Vec<String> {
        snippet.lines().map(str::to_owned).collect()
    }

    #[test]
    fn codex_renderer_matches_responses_contract_and_is_secret_free() {
        let snippet =
            render_codex_toml("http://192.168.1.100:11300/v1", Some("router-alias")).expect("toml");
        assert_eq!(
            root_value(&snippet, "model_provider").as_deref(),
            Some("eggpool")
        );
        assert_eq!(
            root_value(&snippet, "model").as_deref(),
            Some("router-alias")
        );
        let lines = table_lines(&snippet);
        assert_eq!(
            table_value(&lines, "model_providers.eggpool", "name").as_deref(),
            Some("EggPool")
        );
        assert_eq!(
            table_value(&lines, "model_providers.eggpool", "base_url").as_deref(),
            Some("http://192.168.1.100:11300/v1")
        );
        assert_eq!(
            table_value(&lines, "model_providers.eggpool", "wire_api").as_deref(),
            Some("responses")
        );
        assert_eq!(
            table_value(&lines, "model_providers.eggpool", "supports_websockets").as_deref(),
            Some("false")
        );
        assert_eq!(
            table_value(&lines, "model_providers.eggpool", "env_key").as_deref(),
            Some("EGGPOOL_API_KEY")
        );
        assert!(!snippet.contains("chat_completions"));
        // Secret-free by construction: no key input, env name only.
        assert!(snippet.contains("EGGPOOL_API_KEY"));
    }

    #[test]
    fn codex_catalog_is_deterministic_sanitized_and_bounded() {
        let first = build_codex_catalog_json(&projections()).expect("catalog");
        let second = build_codex_catalog_json(&projections()).expect("catalog");
        assert_eq!(first, second);
        let count = validate_codex_catalog_json(&first, "ep_test_key_123").expect("valid");
        assert_eq!(count, 1);
        assert!(!first.contains("ep_test_key_123"));
        let value: Value = serde_json::from_str(&first).expect("json");
        let models = value
            .get("models")
            .and_then(Value::as_array)
            .expect("models");
        assert_eq!(models.len(), 1);
        assert_eq!(
            models[0].get("prefer_websockets").and_then(Value::as_bool),
            Some(false)
        );
    }

    #[test]
    fn codex_mutation_preserves_comments_and_unrelated_tables() {
        let existing = "# user comment\nmodel_provider = \"other\"\n\n[other_table]\nkey = 1\n";
        let (proposed, previous) = apply_codex_text_mutation(
            existing,
            "http://127.0.0.1:11300/v1",
            "/tmp/catalog.json",
            None,
            false,
        );
        assert!(proposed.contains("# user comment"));
        assert!(proposed.contains("[other_table]"));
        assert!(proposed.contains("model_provider = \"eggpool\""));
        assert_eq!(previous.model_provider.as_deref(), Some("other"));
    }

    #[test]
    fn check_reports_drift_secret_free() {
        let issues = check_codex_state(CodexStateCheck {
            config_text: "",
            base_url: "http://127.0.0.1:11300/v1",
            api_key: "secret",
            model: None,
            manage_model: false,
            catalog_path: "/tmp/catalog.json",
            catalog_exists: false,
            catalog_current: false,
        });
        assert!(!issues.is_empty());
        for issue in &issues {
            assert!(!issue.contains("secret"));
        }
    }

    #[test]
    fn catalog_validation_rejects_websocket_advertisement() {
        let catalog = build_codex_catalog_json(&projections()).expect("catalog");
        let mut value: Value = serde_json::from_str(&catalog).expect("json");
        for model in value
            .get_mut("models")
            .and_then(Value::as_array_mut)
            .expect("models")
        {
            model
                .as_object_mut()
                .expect("object")
                .insert("prefer_websockets".to_owned(), Value::Bool(true));
        }
        assert!(validate_codex_catalog_json(&value.to_string(), "").is_err());
    }

    #[test]
    fn debug_placeholder() {
        let _ = AgentModelCapabilities::conservative();
    }
}
