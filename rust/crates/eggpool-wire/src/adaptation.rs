//! Shared, pure W006 semantic adaptation and loss policy.
//!
//! Surface codecs are responsible for their wire grammar.  This module owns
//! the decision about whether a grammar difference is exact, an explicitly
//! warned adaptation, or a local rejection.  It never selects a profile,
//! calls transport, mutates catalog/health state, or creates retry state.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::codec::{AdaptationNotice, CodecError, CodecOutput, CodecReasonCode};
use crate::ir::{
    CanonicalBlockKind, CanonicalRequest, CanonicalToolKind, ClientSurface, ReasoningMode,
};
use crate::profile::WireSurface;

/// Neutral capability status owned by the extractable wire kernel.
///
/// EggPool catalog adapters map `eggpool::catalog::CapabilityStatus` into this
/// type at the boundary (`wire::adapters`); the kernel never imports catalog
/// state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NeutralCapabilityStatus {
    Supported,
    Unsupported,
    Unknown,
    Mixed,
    Conflicting,
}

/// Neutral thinking-capability facts consumed by pure adaptation policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NeutralThinkingCapability {
    pub status: NeutralCapabilityStatus,
    pub toggle: NeutralCapabilityStatus,
    pub effort: NeutralCapabilityStatus,
    pub budget: NeutralCapabilityStatus,
}

/// Redaction-safe native summary facts owned by the wire boundary.
///
/// This mirrors the shape EggPool computes in
/// `eggpool::request::NativeFeatureSummary` without retaining the parsed
/// request value lifetime. Adapters map the EggPool summary into this type.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct NativeSummaryFacts {
    pub native_input_items: usize,
    pub native_tool_definitions: usize,
    pub extension_fields: Vec<String>,
    pub extensions_truncated: bool,
}

impl NativeSummaryFacts {
    pub fn has_cross_surface_blocker(&self) -> bool {
        self.native_input_items > 0 || self.native_tool_definitions > 0
    }
}

pub const MAX_ADAPTATION_NOTICES: usize = 32;

/// Configured handling for a semantic adaptation notice.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum LossPolicy {
    #[default]
    Warn,
    Reject,
}

impl TryFrom<&str> for LossPolicy {
    type Error = &'static str;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "warn" => Ok(Self::Warn),
            "reject" => Ok(Self::Reject),
            _ => Err("loss policy must be warn or reject"),
        }
    }
}

/// Immutable policy input for one pure codec operation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AdaptationPolicy {
    pub loss_policy: LossPolicy,
    pub max_notices: usize,
}

/// Pure capability-policy action supplied by M5/M7 to the codec boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CapabilityDisposition {
    Reject,
    WarnDrop,
    AllowWithWarning,
    Allow,
}

/// Capability outcomes are caller-owned facts, never inferred by codecs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReasoningCapabilityPolicy {
    pub unsupported: CapabilityDisposition,
    pub unknown: CapabilityDisposition,
    pub mixed: CapabilityDisposition,
}

impl Default for ReasoningCapabilityPolicy {
    fn default() -> Self {
        Self {
            unsupported: CapabilityDisposition::Reject,
            unknown: CapabilityDisposition::Reject,
            mixed: CapabilityDisposition::WarnDrop,
        }
    }
}

impl Default for AdaptationPolicy {
    fn default() -> Self {
        Self {
            loss_policy: LossPolicy::Warn,
            max_notices: MAX_ADAPTATION_NOTICES,
        }
    }
}

impl AdaptationPolicy {
    pub const fn warn() -> Self {
        Self {
            loss_policy: LossPolicy::Warn,
            max_notices: MAX_ADAPTATION_NOTICES,
        }
    }

    pub const fn reject() -> Self {
        Self {
            loss_policy: LossPolicy::Reject,
            max_notices: MAX_ADAPTATION_NOTICES,
        }
    }
}

/// Explicit outcome vocabulary used by tests and later facades.
#[derive(Debug, Clone, PartialEq)]
pub enum AdaptationOutcome<T> {
    Exact(T),
    Adapted {
        value: T,
        warnings: Vec<AdaptationNotice>,
    },
    Rejected {
        reason: CodecReasonCode,
        field: Option<String>,
    },
}

impl<T> AdaptationOutcome<T> {
    pub fn into_result(
        self,
        source_surface: Option<WireSurface>,
        target_surface: Option<WireSurface>,
    ) -> Result<CodecOutput<T>, CodecError> {
        match self {
            Self::Exact(value) => Ok(CodecOutput::new(value)),
            Self::Adapted { value, warnings } => Ok(CodecOutput {
                value,
                notices: warnings,
            }),
            Self::Rejected { reason, field } => Err(CodecError {
                reason,
                field,
                source_surface,
                target_surface,
            }),
        }
    }
}

/// Apply one bounded policy to every codec's notices.
pub fn apply_adaptation_policy<T>(
    output: CodecOutput<T>,
    policy: &AdaptationPolicy,
) -> Result<CodecOutput<T>, CodecError> {
    if output.notices.len() > policy.max_notices {
        return Err(CodecError {
            reason: CodecReasonCode::ResourceLimitViolation,
            field: Some("adaptation_notices".into()),
            source_surface: None,
            target_surface: None,
        });
    }
    if policy.loss_policy == LossPolicy::Reject && !output.notices.is_empty() {
        let first = &output.notices[0];
        return Err(CodecError {
            reason: CodecReasonCode::LossRejected,
            field: first.field.clone().or_else(|| Some(first.code.0.clone())),
            source_surface: first.source_surface,
            target_surface: first.target_surface,
        });
    }
    Ok(output)
}

/// Construct a bounded, redaction-safe notice for a structural difference.
pub fn notice(
    code: &str,
    field: &str,
    source: Option<WireSurface>,
    target: Option<WireSurface>,
) -> AdaptationNotice {
    AdaptationNotice::new(
        code,
        CodecReasonCode::UnsupportedSemanticFeature,
        Some(field),
        source,
        target,
    )
}

/// Validate the semantic subset shared by all request codecs and return the
/// target-specific differences that the encoder cannot express exactly.
pub fn request_notices(
    request: &CanonicalRequest,
    target: WireSurface,
) -> Result<Vec<AdaptationNotice>, CodecError> {
    validate_reasoning(request)?;
    validate_structured_output(request)?;
    validate_tools(request)?;

    let source = Some(client_wire_surface(request.client_surface));
    let mut notices = Vec::new();

    reasoning_notices(request, target, source, &mut notices);
    structured_notices(request, target, source, &mut notices);
    tool_notices(request, target, source, &mut notices);
    media_notices(request, target, source, &mut notices);
    cache_notices(request, target, source, &mut notices);

    if request.metadata.is_empty() {
        // no-op
    } else if matches!(
        target,
        WireSurface::AnthropicMessages
            | WireSurface::GeminiInteractions
            | WireSurface::GeminiGenerateContent
    ) {
        notices.push(notice(
            "metadata_not_representable",
            "metadata",
            source,
            Some(target),
        ));
    }
    if request.parallel_tool_calls.is_some() && target == WireSurface::AnthropicMessages {
        notices.push(notice(
            "parallel_tool_calls_not_representable",
            "parallel_tool_calls",
            source,
            Some(target),
        ));
    }
    Ok(notices)
}

/// Apply the cross-surface policy to Responses syntax that has no canonical
/// representation. Native same-surface forwarding never calls this helper.
/// Unknown extension fields are safe to omit only with an explicit notice;
/// native input items and tool definitions are semantic blockers.
///
/// This is the pure kernel entry point over [`NativeSummaryFacts`].
/// EggPool callers use `eggpool::wire::adapters::native_preservation_notices`
/// which maps `NativeRequestPreservation` into these facts.
pub fn native_summary_notices(
    summary: &NativeSummaryFacts,
    target: WireSurface,
) -> Result<Vec<AdaptationNotice>, CodecError> {
    if target == WireSurface::OpenaiResponses {
        return Ok(Vec::new());
    }
    let source = Some(WireSurface::OpenaiResponses);
    if summary.has_cross_surface_blocker() && summary.native_input_items > 0 {
        return Err(CodecError {
            reason: CodecReasonCode::UnsupportedSemanticFeature,
            field: Some("input.native_item".into()),
            source_surface: source,
            target_surface: Some(target),
        });
    }
    if summary.native_tool_definitions > 0 {
        return Err(CodecError {
            reason: CodecReasonCode::UnsupportedSemanticFeature,
            field: Some("tools.native_definition".into()),
            source_surface: source,
            target_surface: Some(target),
        });
    }
    let mut notices: Vec<_> = summary
        .extension_fields
        .iter()
        .map(|field| {
            notice(
                "native_extension_not_representable",
                field,
                source,
                Some(target),
            )
        })
        .collect();
    if summary.extensions_truncated {
        notices.push(notice(
            "native_extensions_truncated",
            "extensions",
            source,
            Some(target),
        ));
    }
    Ok(notices)
}

fn media_notices(
    request: &CanonicalRequest,
    target: WireSurface,
    source: Option<WireSurface>,
    notices: &mut Vec<AdaptationNotice>,
) {
    for message in &request.messages {
        for block in &message.content {
            let code = match (block.kind, target) {
                (CanonicalBlockKind::Audio, _) => Some("audio_not_representable"),
                (CanonicalBlockKind::Document, WireSurface::GeminiInteractions) => {
                    Some("document_not_representable")
                }
                _ => None,
            };
            if let Some(code) = code {
                notices.push(notice(code, "content", source, Some(target)));
            }
            if block.kind == CanonicalBlockKind::Image
                && block
                    .media
                    .as_ref()
                    .is_some_and(|media| media.detail.is_some())
                && !matches!(
                    target,
                    WireSurface::OpenaiChatCompletions | WireSurface::OpenaiResponses
                )
            {
                notices.push(notice(
                    "image_detail_not_representable",
                    "content.image.detail",
                    source,
                    Some(target),
                ));
            }
        }
    }
}

fn cache_notices(
    request: &CanonicalRequest,
    target: WireSurface,
    source: Option<WireSurface>,
    notices: &mut Vec<AdaptationNotice>,
) {
    if request.cache_control.is_some() {
        notices.push(notice(
            "cache_control_unsupported_placement",
            "cache_control",
            source,
            Some(target),
        ));
        if !valid_cache_control(request.cache_control.as_ref()) {
            notices.push(notice(
                "cache_control_invalid_shape",
                "cache_control",
                source,
                Some(target),
            ));
        }
    }
    for message in &request.messages {
        for block in &message.content {
            let has_anthropic = block.cache_control.is_some();
            let has_openai = block.prompt_cache_breakpoint.is_some();
            if has_anthropic && !valid_cache_control(block.cache_control.as_ref()) {
                notices.push(notice(
                    "cache_control_invalid_shape",
                    "content.cache_control",
                    source,
                    Some(target),
                ));
            }
            if has_openai && !valid_prompt_breakpoint(block.prompt_cache_breakpoint.as_ref()) {
                notices.push(notice(
                    "cache_breakpoint_invalid_shape",
                    "content.prompt_cache_breakpoint",
                    source,
                    Some(target),
                ));
            }
            if (has_anthropic && target != WireSurface::AnthropicMessages)
                || (has_openai && target == WireSurface::GeminiInteractions)
                || (has_openai && target == WireSurface::GeminiGenerateContent)
            {
                notices.push(notice(
                    "cache_boundary_not_representable",
                    "content.cache_control",
                    source,
                    Some(target),
                ));
            }
        }
    }
    for tool in &request.tools {
        if tool.cache_control.is_some() && target != WireSurface::AnthropicMessages {
            notices.push(notice(
                "cache_control_unsupported_by_target",
                "tools.cache_control",
                source,
                Some(target),
            ));
        }
        if tool.cache_control.is_some() && !valid_cache_control(tool.cache_control.as_ref()) {
            notices.push(notice(
                "cache_control_invalid_shape",
                "tools.cache_control",
                source,
                Some(target),
            ));
        }
        if tool.defer_loading.is_some() && target != WireSurface::AnthropicMessages {
            notices.push(notice(
                "provider_extension_not_representable",
                "tools.defer_loading",
                source,
                Some(target),
            ));
        }
    }
}

fn valid_cache_control(value: Option<&serde_json::Value>) -> bool {
    value
        .and_then(serde_json::Value::as_object)
        .and_then(|object| object.get("type"))
        .and_then(serde_json::Value::as_str)
        == Some("ephemeral")
}

fn valid_prompt_breakpoint(value: Option<&serde_json::Value>) -> bool {
    value
        .and_then(serde_json::Value::as_object)
        .and_then(|object| object.get("mode"))
        .and_then(serde_json::Value::as_str)
        == Some("explicit")
}

/// Apply caller/M5 reasoning capability facts without mutating those facts.
///
/// This is deliberately separate from [`request_notices`]: codecs can encode
/// a request without a catalog, while M7 can supply verified capability facts
/// when it wants capability-aware rejection or downgrade behavior.
///
/// Pure kernel entry point over [`NeutralThinkingCapability`]. EggPool
/// callers use `eggpool::wire::adapters::reasoning_capability_notices`.
pub fn reasoning_capability_notices_neutral(
    request: &CanonicalRequest,
    capability: &NeutralThinkingCapability,
    policy: &ReasoningCapabilityPolicy,
    target: WireSurface,
) -> Result<Vec<AdaptationNotice>, CodecError> {
    let intent = &request.reasoning;
    if intent.requested != Some(true) {
        return Ok(Vec::new());
    }
    let dimension_status = match intent.mode {
        ReasoningMode::Effort => capability.effort,
        ReasoningMode::FixedBudget => capability.budget,
        ReasoningMode::Toggle | ReasoningMode::Adaptive | ReasoningMode::Unspecified => {
            capability.toggle
        }
    };
    let status = match capability.status {
        NeutralCapabilityStatus::Supported => dimension_status,
        other => other,
    };
    let disposition = match status {
        NeutralCapabilityStatus::Supported => return Ok(Vec::new()),
        NeutralCapabilityStatus::Unsupported => policy.unsupported,
        NeutralCapabilityStatus::Unknown | NeutralCapabilityStatus::Conflicting => policy.unknown,
        NeutralCapabilityStatus::Mixed => policy.mixed,
    };
    let source = Some(client_wire_surface(request.client_surface));
    let field = match intent.mode {
        ReasoningMode::Effort => "reasoning.effort",
        ReasoningMode::FixedBudget => "reasoning.budget_tokens",
        ReasoningMode::Toggle | ReasoningMode::Adaptive | ReasoningMode::Unspecified => "reasoning",
    };
    match disposition {
        CapabilityDisposition::Reject => Err(CodecError {
            reason: CodecReasonCode::UnsupportedSemanticFeature,
            field: Some(field.into()),
            source_surface: source,
            target_surface: Some(target),
        }),
        CapabilityDisposition::WarnDrop => Ok(vec![notice(
            "reasoning_control_dropped_by_capability",
            field,
            source,
            Some(target),
        )]),
        CapabilityDisposition::AllowWithWarning => Ok(vec![notice(
            "reasoning_capability_uncertain",
            field,
            source,
            Some(target),
        )]),
        CapabilityDisposition::Allow => Ok(Vec::new()),
    }
}

fn validate_reasoning(request: &CanonicalRequest) -> Result<(), CodecError> {
    if request.reasoning.requested == Some(false)
        && (request.reasoning.effort.is_some() || request.reasoning.budget_tokens.is_some())
    {
        return Err(source_error("reasoning", request));
    }
    if request.reasoning.budget_tokens == Some(0)
        || request
            .reasoning
            .effort
            .as_deref()
            .is_some_and(str::is_empty)
    {
        return Err(source_error("reasoning", request));
    }
    Ok(())
}

fn reasoning_notices(
    request: &CanonicalRequest,
    target: WireSurface,
    source: Option<WireSurface>,
    notices: &mut Vec<AdaptationNotice>,
) {
    let reasoning = &request.reasoning;
    if reasoning.requested.is_none() {
        return;
    }
    let supported = match target {
        WireSurface::OpenaiChatCompletions | WireSurface::OpenaiResponses => {
            (reasoning.mode == ReasoningMode::Effort
                || (reasoning.mode == ReasoningMode::Toggle && reasoning.explicit_disable))
                && reasoning.budget_tokens.is_none()
        }
        WireSurface::AnthropicMessages => {
            reasoning.budget_tokens.is_some()
                || reasoning.mode == ReasoningMode::Adaptive
                || (reasoning.mode == ReasoningMode::Toggle && reasoning.explicit_disable)
        }
        WireSurface::GeminiInteractions => {
            reasoning.requested == Some(false) || reasoning.mode == ReasoningMode::Effort
        }
        WireSurface::GeminiGenerateContent => {
            reasoning.budget_tokens.is_some() || reasoning.requested == Some(false)
        }
    };
    if !supported {
        let code = if reasoning.effort.is_some() {
            "reasoning_effort_not_representable"
        } else if reasoning.budget_tokens.is_some() {
            "reasoning_budget_not_representable"
        } else {
            "reasoning_control_not_representable"
        };
        notices.push(notice(code, "reasoning", source, Some(target)));
    }
}

fn validate_structured_output(request: &CanonicalRequest) -> Result<(), CodecError> {
    let Some(format) = request.response_format.as_ref() else {
        return Ok(());
    };
    let Some(kind) = format.get("type").and_then(serde_json::Value::as_str) else {
        return Err(source_error("response_format.type", request));
    };
    match kind {
        "json_object" => Ok(()),
        "json_schema" => {
            if let Some(json_schema) = format.get("json_schema") {
                let schema = json_schema
                    .as_object()
                    .and_then(|schema| schema.get("schema"))
                    .and_then(serde_json::Value::as_object);
                if schema.is_none() {
                    return Err(source_error("response_format.json_schema.schema", request));
                }
            }
            Ok(())
        }
        _ => Err(source_error("response_format.type", request)),
    }
}

fn structured_notices(
    request: &CanonicalRequest,
    target: WireSurface,
    source: Option<WireSurface>,
    notices: &mut Vec<AdaptationNotice>,
) {
    let Some(format) = request.response_format.as_ref() else {
        return;
    };
    let kind = format
        .get("type")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if matches!(target, WireSurface::AnthropicMessages) {
        notices.push(notice(
            "structured_output_not_representable",
            "response_format",
            source,
            Some(target),
        ));
    } else if target == WireSurface::GeminiInteractions && kind == "json_schema" {
        // Interactions currently accepts the JSON object wrapper but has no
        // frozen schema field in the W005 contract.
        notices.push(notice(
            "structured_schema_not_representable",
            "response_format.json_schema",
            source,
            Some(target),
        ));
    }
}

fn validate_tools(request: &CanonicalRequest) -> Result<(), CodecError> {
    let mut names = BTreeSet::new();
    for tool in &request.tools {
        if tool.name.trim().is_empty() || !names.insert(tool.name.clone()) {
            return Err(source_error("tools.name", request));
        }
    }
    let mut calls = BTreeSet::new();
    for message in &request.messages {
        for block in &message.content {
            match block.kind {
                CanonicalBlockKind::ToolCall => {
                    let Some(call_id) = block.call_id.as_deref() else {
                        return Err(source_error("tool_call.id", request));
                    };
                    if call_id.trim().is_empty() || !calls.insert(call_id.to_owned()) {
                        return Err(source_error("tool_call.id", request));
                    }
                    if block.name.as_deref().is_none_or(str::is_empty) {
                        return Err(source_error("tool_call.name", request));
                    }
                }
                CanonicalBlockKind::ToolResult => {
                    let Some(call_id) = block.call_id.as_deref() else {
                        return Err(source_error("tool_result.call_id", request));
                    };
                    if call_id.trim().is_empty() {
                        return Err(source_error("tool_result.call_id", request));
                    }
                }
                _ => {}
            }
        }
    }
    Ok(())
}

fn tool_notices(
    request: &CanonicalRequest,
    target: WireSurface,
    source: Option<WireSurface>,
    notices: &mut Vec<AdaptationNotice>,
) {
    if target != WireSurface::OpenaiResponses
        && request
            .tools
            .iter()
            .any(|tool| tool.kind == CanonicalToolKind::Freeform)
    {
        notices.push(notice(
            "freeform_tool_wrapped_as_function",
            "tools.custom",
            source,
            Some(target),
        ));
    }
    if target != WireSurface::OpenaiResponses
        && request
            .tools
            .iter()
            .any(|tool| tool.kind == CanonicalToolKind::DeferredSearch)
    {
        notices.push(notice(
            "deferred_tool_search_wrapped_as_function",
            "tools.tool_search",
            source,
            Some(target),
        ));
    }
    if matches!(target, WireSurface::GeminiGenerateContent) {
        let has_ids = request.messages.iter().any(|message| {
            message.content.iter().any(|block| {
                matches!(
                    block.kind,
                    CanonicalBlockKind::ToolCall | CanonicalBlockKind::ToolResult
                ) && block.call_id.is_some()
            })
        });
        if has_ids {
            notices.push(notice(
                "tool_call_id_not_representable",
                "tool_call.id",
                source,
                Some(target),
            ));
        }
    }

    if target == WireSurface::OpenaiChatCompletions {
        let mixed = request.messages.iter().any(|message| {
            let has_text = message
                .content
                .iter()
                .any(|block| block.kind == CanonicalBlockKind::Text);
            let has_call = message
                .content
                .iter()
                .any(|block| block.kind == CanonicalBlockKind::ToolCall);
            has_text && has_call
        });
        if mixed {
            notices.push(notice(
                "tool_order_collapsed",
                "messages.content",
                source,
                Some(target),
            ));
        }
    }
}

fn source_error(field: &str, request: &CanonicalRequest) -> CodecError {
    CodecError {
        reason: CodecReasonCode::MalformedSourceRequest,
        field: Some(field.into()),
        source_surface: Some(client_wire_surface(request.client_surface)),
        target_surface: None,
    }
}

pub const fn client_wire_surface(surface: ClientSurface) -> WireSurface {
    match surface {
        ClientSurface::ChatCompletions => WireSurface::OpenaiChatCompletions,
        ClientSurface::Responses => WireSurface::OpenaiResponses,
        ClientSurface::Messages => WireSurface::AnthropicMessages,
    }
}

/// Reusable capability fact for deferred `tool_search`.
///
/// Native Responses forwarding preserves the declaration/call/output
/// lifecycle byte-for-byte. Every other built-in surface carries ordinary
/// function tools, so a client-executed search bridges deterministically as
/// a `tool_search` wrapper function. Hosted/server search stays native-only.
/// Unknown future surfaces must opt in explicitly rather than silently
/// dropping the tool.
pub const fn supports_deferred_tool_search(target: WireSurface) -> bool {
    matches!(
        target,
        WireSurface::OpenaiResponses
            | WireSurface::OpenaiChatCompletions
            | WireSurface::AnthropicMessages
            | WireSurface::GeminiInteractions
            | WireSurface::GeminiGenerateContent
    )
}

/// Derive a repeatable ID for provider responses that have no native call ID.
/// This is a compatibility identity, never a random identifier or a log value.
pub fn stable_tool_call_id(name: &str, arguments: &str, ordinal: usize) -> String {
    use sha2::{Digest, Sha256};

    let mut digest = Sha256::new();
    digest.update(name.as_bytes());
    digest.update([0]);
    digest.update(arguments.as_bytes());
    digest.update([0]);
    digest.update(ordinal.to_be_bytes());
    let bytes = digest.finalize();
    let mut id = String::from("call_");
    for byte in bytes.iter().take(12) {
        use std::fmt::Write as _;
        let _ = write!(id, "{byte:02x}");
    }
    id
}
