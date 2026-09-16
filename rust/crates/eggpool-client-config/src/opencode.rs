//! Portable OpenCode rendering and validation.
//!
//! The renderer uses the Responses-capable `@ai-sdk/openai` runtime with
//! `{env:EGGPOOL_API_KEY}` interpolation, per-model limits from the same
//! conservative projection, and no resolved secret.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::codex::EGGPOOL_API_KEY_ENV;
use crate::error::ClientConfigError;
use crate::projection::AgentModelProjection;

/// NPM runtime for the Responses-capable OpenCode provider.
pub const OPENCODE_RESPONSES_NPM: &str = "@ai-sdk/openai";

/// Render an OpenCode JSON config from a base URL and projections.
pub fn render_opencode_config(
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<String, ClientConfigError> {
    let mut sorted = projections.to_vec();
    sorted.sort_by(|left, right| left.public_id.cmp(&right.public_id));
    let mut models = BTreeMap::new();
    for projection in &sorted {
        let mut entry = Map::new();
        if projection.display_name != projection.public_id {
            entry.insert(
                "name".to_owned(),
                Value::String(projection.display_name.clone()),
            );
        }
        let mut limit = Map::new();
        if let Some(value) = projection
            .capabilities
            .context_tokens
            .filter(|value| *value > 0)
        {
            limit.insert("context".to_owned(), serde_json::json!(value));
        }
        if let Some(value) = projection
            .capabilities
            .max_output_tokens
            .filter(|value| *value > 0)
        {
            limit.insert("output".to_owned(), serde_json::json!(value));
        }
        if !limit.is_empty() {
            entry.insert("limit".to_owned(), Value::Object(limit));
        }
        let mut input_modalities = vec![Value::String("text".to_owned())];
        if projection.capabilities.input_images == Some(true) {
            input_modalities.push(Value::String("image".to_owned()));
        }
        entry.insert(
            "modalities".to_owned(),
            serde_json::json!({
                "input": input_modalities,
                "output": ["text"],
            }),
        );
        if let Some(reasoning) = &projection.capabilities.reasoning {
            entry.insert("reasoning".to_owned(), Value::Bool(true));
            if !reasoning.efforts.is_empty() {
                let mut variants = Map::new();
                for effort in &reasoning.efforts {
                    variants.insert(
                        effort.to_owned(),
                        serde_json::json!({"reasoningEffort": effort}),
                    );
                }
                entry.insert("variants".to_owned(), Value::Object(variants));
            }
        }
        models.insert(projection.public_id.clone(), Value::Object(entry));
    }
    let options = Map::from_iter([
        ("baseURL".to_owned(), Value::String(base_url.to_owned())),
        (
            "apiKey".to_owned(),
            Value::String(format!("{{env:{EGGPOOL_API_KEY_ENV}}}")),
        ),
    ]);
    let eggpool = Map::from_iter([
        (
            "npm".to_owned(),
            Value::String(OPENCODE_RESPONSES_NPM.to_owned()),
        ),
        ("name".to_owned(), Value::String("EggPool".to_owned())),
        ("options".to_owned(), Value::Object(options)),
        ("models".to_owned(), serde_json::to_value(models)?),
    ]);
    let provider = Map::from_iter([("eggpool".to_owned(), Value::Object(eggpool))]);
    let root = Map::from_iter([
        (
            "$schema".to_owned(),
            Value::String("https://opencode.ai/config.json".to_owned()),
        ),
        ("provider".to_owned(), Value::Object(provider)),
    ]);
    Ok(serde_json::to_string_pretty(&root)?)
}

/// Extract the expected `provider.eggpool` value for drift checks.
pub fn expected_opencode_provider(
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<Value, ClientConfigError> {
    let rendered = render_opencode_config(base_url, projections)?;
    let value: Value = serde_json::from_str(&rendered)?;
    value
        .get("provider")
        .and_then(|provider| provider.get("eggpool"))
        .cloned()
        .ok_or_else(|| ClientConfigError::Drift {
            detail: "OpenCode renderer is missing the eggpool provider".to_owned(),
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::projection::{project_models, IntegrationModel, ModelLimits};
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

    #[test]
    fn opencode_renderer_uses_responses_runtime_and_env_key() {
        let rendered = render_opencode_config("http://192.168.1.100:11300/v1", &projections())
            .expect("opencode");
        assert!(rendered.contains(OPENCODE_RESPONSES_NPM));
        assert!(!rendered.contains("@ai-sdk/openai-compatible"));
        assert!(rendered.contains("{env:EGGPOOL_API_KEY}"));
        let value: Value = serde_json::from_str(&rendered).expect("json");
        let provider = value
            .get("provider")
            .and_then(|provider| provider.get("eggpool"))
            .expect("eggpool provider");
        assert_eq!(
            provider.get("npm").and_then(Value::as_str),
            Some(OPENCODE_RESPONSES_NPM)
        );
        let models = provider
            .get("models")
            .and_then(Value::as_object)
            .expect("models");
        let entry = models.get("gpt-4o/openai").expect("model entry");
        assert_eq!(
            entry
                .get("limit")
                .and_then(|limit| limit.get("context"))
                .and_then(Value::as_u64),
            Some(128_000)
        );
    }

    #[test]
    fn opencode_renderer_is_secret_free_and_deterministic() {
        let first =
            render_opencode_config("http://127.0.0.1:11300/v1", &projections()).expect("first");
        let second =
            render_opencode_config("http://127.0.0.1:11300/v1", &projections()).expect("second");
        assert_eq!(first, second);
        assert!(!first.contains("ep_test_key"));
    }
}
