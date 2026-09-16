//! Provider-neutral coding-agent projection.
//!
//! This is the single authoritative implementation of the conservative
//! projection used by the Codex catalog and OpenCode renderers. Rules:
//!
//! - derive only from validated facts; unknown stays `None`, never optimistic;
//! - never infer capability from model-ID substrings;
//! - `websockets` remains false until a real EggPool Responses WebSocket path
//!   exists;
//! - remote compaction is not advertised here;
//! - provider-private source metadata never leaks into the projection.
//!
//! The crate owns [`IntegrationModel`] as portable source facts (plain data,
//! no database or config access). The EggPool application converts its
//! catalog/database rows into this type and retains ownership of loading.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Portable model limits used by the projection.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelLimits {
    pub context_tokens: Option<u64>,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
}

/// Portable source facts for one public model entry.
///
/// All fields are plain data. `capabilities` carries validated capability
/// JSON (for example `supports_tools`, `supports_vision`, `thinking`);
/// `source_metadata` is provider-private and never leaks into the projection.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IntegrationModel {
    pub model_id: String,
    pub base_model_id: String,
    pub provider_id: Option<String>,
    pub display_name: String,
    pub capabilities: Value,
    pub source_metadata: Value,
    pub limits: ModelLimits,
}

/// Normalized reasoning facts for coding-agent renderers.
///
/// Derived only from validated catalog/model-info facts. Unknown stays absent
/// rather than optimistic.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentReasoningCapabilities {
    pub efforts: Vec<String>,
    pub default_effort: Option<String>,
    pub summaries: bool,
}

/// Provider-neutral coding-agent capabilities.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentModelCapabilities {
    pub context_tokens: Option<u64>,
    pub max_output_tokens: Option<u64>,
    pub input_text: bool,
    pub input_images: Option<bool>,
    pub reasoning: Option<AgentReasoningCapabilities>,
    pub function_tools: Option<bool>,
    pub freeform_tools: Option<bool>,
    pub deferred_tool_search: Option<bool>,
    pub responses: bool,
    pub websockets: bool,
}

/// Client-neutral projected model used by Codex/OpenCode renderers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentModelProjection {
    pub public_id: String,
    pub display_name: String,
    pub capabilities: AgentModelCapabilities,
}

impl AgentModelCapabilities {
    /// Conservative unknown baseline: text input and Responses only.
    pub fn conservative() -> Self {
        Self {
            context_tokens: None,
            max_output_tokens: None,
            input_text: true,
            input_images: None,
            reasoning: None,
            function_tools: None,
            freeform_tools: None,
            deferred_tool_search: None,
            responses: true,
            websockets: false,
        }
    }
}

/// Derive a provider-neutral projection from one validated source model.
///
/// Rules: unknown stays unknown (None), never inferred from the model ID,
/// `websockets` stays false, remote compaction is not advertised here.
pub fn project_model(model: &IntegrationModel) -> AgentModelProjection {
    let mut capabilities = AgentModelCapabilities::conservative();
    capabilities.context_tokens = model.limits.context_tokens.filter(|value| *value > 0);
    capabilities.max_output_tokens = model.limits.output_tokens.filter(|value| *value > 0);

    if let Some(object) = model.capabilities.as_object() {
        if let Some(vision) = object.get("supports_vision").and_then(Value::as_bool) {
            capabilities.input_images = Some(vision);
        }
        if let Some(tools) = object.get("supports_tools").and_then(Value::as_bool) {
            capabilities.function_tools = Some(tools);
        }
        // Freeform (`custom`) and deferred (`tool_search`) stay unknown
        // unless the catalog explicitly proves them. Unknown is conservative:
        // renderers must not advertise them optimistically.
        if let Some(freeform) = object
            .get("supports_freeform_tools")
            .and_then(Value::as_bool)
        {
            capabilities.freeform_tools = Some(freeform);
        }
        if let Some(deferred) = object
            .get("supports_deferred_tool_search")
            .and_then(Value::as_bool)
        {
            capabilities.deferred_tool_search = Some(deferred);
        }
        let mut reasoning_supported = false;
        let mut efforts: Vec<String> = Vec::new();
        if let Some(thinking) = object.get("thinking").and_then(Value::as_object) {
            let status_supported = thinking
                .get("status")
                .and_then(Value::as_str)
                .is_some_and(|status| status == "supported");
            if status_supported {
                reasoning_supported = true;
                if let Some(values) = thinking.get("supported_efforts").and_then(Value::as_array) {
                    let mut ordered: BTreeSet<String> = BTreeSet::new();
                    for value in values {
                        if let Some(effort) = value.as_str() {
                            ordered.insert(effort.to_owned());
                        }
                    }
                    efforts = ordered.into_iter().collect();
                    efforts.sort();
                }
            }
        }
        if reasoning_supported {
            // `supported_efforts` is advisory; an empty list still means
            // reasoning is supported without enumerated efforts.
            capabilities.reasoning = Some(AgentReasoningCapabilities {
                efforts,
                default_effort: None,
                summaries: false,
            });
        }
    }

    AgentModelProjection {
        public_id: model.model_id.clone(),
        display_name: model.display_name.clone(),
        capabilities,
    }
}

/// Collapse multiple projections for one public ID into conservative
/// guaranteed capabilities.
///
/// Aggregation: context/output are the minimum known guaranteed values,
/// boolean required features and input modalities are intersections,
/// reasoning efforts are set intersections, and unknown on any required
/// candidate remains unknown/conservative. Never publishes the union of
/// heterogeneous targets.
pub fn aggregate_projections(
    public_id: &str,
    display_name: &str,
    projections: &[AgentModelCapabilities],
) -> AgentModelCapabilities {
    if projections.is_empty() {
        return AgentModelCapabilities::conservative();
    }
    if projections.len() == 1 {
        return projections[0].clone();
    }
    let context_tokens = if projections
        .iter()
        .all(|capability| capability.context_tokens.is_some())
    {
        projections
            .iter()
            .filter_map(|capability| capability.context_tokens)
            .min()
    } else {
        None
    };
    let max_output_tokens = if projections
        .iter()
        .all(|capability| capability.max_output_tokens.is_some())
    {
        projections
            .iter()
            .filter_map(|capability| capability.max_output_tokens)
            .min()
    } else {
        None
    };
    let intersect_optional_bool =
        |select: fn(&AgentModelCapabilities) -> Option<bool>| -> Option<bool> {
            let mut values = BTreeSet::new();
            for capability in projections {
                match select(capability) {
                    Some(value) => {
                        values.insert(value);
                    }
                    None => {
                        values.insert(true);
                        values.insert(false);
                    }
                }
            }
            if values.len() == 1 {
                Some(*values.iter().next().expect("single value"))
            } else {
                None
            }
        };
    let input_images = intersect_optional_bool(|capability| capability.input_images);
    let function_tools = intersect_optional_bool(|capability| capability.function_tools);
    let freeform_tools = intersect_optional_bool(|capability| capability.freeform_tools);
    let deferred_tool_search =
        intersect_optional_bool(|capability| capability.deferred_tool_search);
    let reasoning = {
        if projections
            .iter()
            .all(|capability| capability.reasoning.is_some())
        {
            let mut intersection: Option<BTreeSet<String>> = None;
            for capability in projections {
                let efforts = capability
                    .reasoning
                    .as_ref()
                    .map(|reasoning| reasoning.efforts.iter().cloned().collect::<BTreeSet<_>>())
                    .unwrap_or_default();
                intersection = Some(match intersection {
                    Some(current) => current.intersection(&efforts).cloned().collect(),
                    None => efforts,
                });
            }
            let mut efforts: Vec<String> = intersection.unwrap_or_default().into_iter().collect();
            efforts.sort();
            Some(AgentReasoningCapabilities {
                efforts,
                default_effort: None,
                summaries: false,
            })
        } else {
            None
        }
    };
    let _ = (public_id, display_name);
    AgentModelCapabilities {
        context_tokens,
        max_output_tokens,
        input_text: true,
        input_images,
        reasoning,
        function_tools,
        freeform_tools,
        deferred_tool_search,
        responses: true,
        websockets: false,
    }
}

/// Project every public model/alias in deterministic order.
///
/// Models sharing one `model_id` are collapsed with
/// [`aggregate_projections`]. Output is sorted by `public_id`.
pub fn project_models(models: &[IntegrationModel]) -> Vec<AgentModelProjection> {
    let mut grouped: BTreeMap<&str, Vec<&IntegrationModel>> = BTreeMap::new();
    for model in models {
        grouped
            .entry(model.model_id.as_str())
            .or_default()
            .push(model);
    }
    let mut projections = Vec::new();
    for (public_id, models) in grouped {
        let display_name = models
            .first()
            .map(|model| model.display_name.as_str())
            .unwrap_or(public_id);
        let capabilities: Vec<AgentModelCapabilities> = models
            .iter()
            .map(|model| project_model(model).capabilities)
            .collect();
        let merged = aggregate_projections(public_id, display_name, &capabilities);
        projections.push(AgentModelProjection {
            public_id: public_id.to_owned(),
            display_name: display_name.to_owned(),
            capabilities: merged,
        });
    }
    projections.sort_by(|left, right| left.public_id.cmp(&right.public_id));
    projections
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Map};

    fn source_model(id: &str) -> IntegrationModel {
        IntegrationModel {
            model_id: id.to_owned(),
            base_model_id: id.to_owned(),
            provider_id: None,
            display_name: id.to_owned(),
            capabilities: Value::Object(Map::new()),
            source_metadata: Value::Object(Map::new()),
            limits: ModelLimits::default(),
        }
    }

    #[test]
    fn projection_derives_only_from_validated_facts() {
        let model = IntegrationModel {
            capabilities: json!({
                "supports_tools": true,
                "supports_vision": false,
                "thinking": {
                    "status": "supported",
                    "supported_efforts": ["high", "low", "high"],
                },
            }),
            source_metadata: json!({"secret": "must-not-leak"}),
            limits: ModelLimits {
                context_tokens: Some(200_000),
                output_tokens: Some(16_000),
                ..Default::default()
            },
            ..source_model("alias")
        };
        let projection = project_model(&model);
        assert_eq!(projection.public_id, "alias");
        assert_eq!(projection.capabilities.context_tokens, Some(200_000));
        assert_eq!(projection.capabilities.max_output_tokens, Some(16_000));
        assert_eq!(projection.capabilities.input_images, Some(false));
        assert_eq!(projection.capabilities.function_tools, Some(true));
        assert!(projection.capabilities.responses);
        assert!(!projection.capabilities.websockets);
        let reasoning = projection
            .capabilities
            .reasoning
            .as_ref()
            .expect("reasoning");
        assert_eq!(reasoning.efforts, vec!["high".to_owned(), "low".to_owned()]);
        // Serialized projection must not leak provider-private metadata.
        let rendered = serde_json::to_string(&projection).expect("json");
        assert!(!rendered.contains("must-not-leak"));
    }

    #[test]
    fn projection_leaves_unknown_absent_and_never_infers_from_name() {
        let projection = project_model(&source_model("gpt-4o-super-vision-tool-9000"));
        assert_eq!(projection.capabilities.context_tokens, None);
        assert_eq!(projection.capabilities.input_images, None);
        assert_eq!(projection.capabilities.function_tools, None);
        assert_eq!(projection.capabilities.reasoning, None);
        assert!(!projection.capabilities.websockets);
    }

    #[test]
    fn aggregation_uses_conservative_intersection_not_union() {
        let left = AgentModelCapabilities {
            context_tokens: Some(200_000),
            max_output_tokens: Some(16_000),
            input_text: true,
            input_images: Some(true),
            reasoning: Some(AgentReasoningCapabilities {
                efforts: vec!["high".to_owned(), "low".to_owned(), "medium".to_owned()],
                default_effort: None,
                summaries: false,
            }),
            function_tools: Some(true),
            freeform_tools: None,
            deferred_tool_search: None,
            responses: true,
            websockets: false,
        };
        let right = AgentModelCapabilities {
            context_tokens: Some(100_000),
            max_output_tokens: None,
            input_text: true,
            input_images: Some(false),
            reasoning: Some(AgentReasoningCapabilities {
                efforts: vec!["low".to_owned(), "medium".to_owned()],
                default_effort: None,
                summaries: false,
            }),
            function_tools: Some(true),
            freeform_tools: None,
            deferred_tool_search: None,
            responses: true,
            websockets: false,
        };
        let merged = aggregate_projections("alias", "Alias", &[left, right]);
        assert_eq!(merged.context_tokens, Some(100_000));
        assert_eq!(merged.max_output_tokens, None);
        assert_eq!(merged.input_images, None);
        assert_eq!(merged.function_tools, Some(true));
        let reasoning = merged.reasoning.expect("reasoning");
        assert_eq!(
            reasoning.efforts,
            vec!["low".to_owned(), "medium".to_owned()]
        );
        assert!(!merged.websockets);
    }

    #[test]
    fn project_models_is_deterministic_and_groups_aliases() {
        let first = IntegrationModel {
            display_name: "Alias".to_owned(),
            limits: ModelLimits {
                context_tokens: Some(200_000),
                ..Default::default()
            },
            ..source_model("alias")
        };
        let second = IntegrationModel {
            display_name: "Alias".to_owned(),
            limits: ModelLimits {
                context_tokens: Some(100_000),
                ..Default::default()
            },
            ..source_model("alias")
        };
        let projections = project_models(&[second.clone(), first.clone()]);
        assert_eq!(projections.len(), 1);
        assert_eq!(projections[0].capabilities.context_tokens, Some(100_000));
    }
}
