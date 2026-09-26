//! EggPool-owned adapters at the wire-kernel extraction seam.
//!
//! This module is explicitly *not* part of the extractable sans-I/O kernel.
//! It maps EggPool catalog, request-preservation, routing, and config facts
//! into the neutral types owned by `wire::{ir, adaptation, decode, registry}`
//! so the kernel modules never import EggPool runtime state.

use std::collections::BTreeMap;

use super::adaptation::{
    NativeSummaryFacts, NeutralCapabilityStatus, NeutralThinkingCapability,
    ReasoningCapabilityPolicy, native_summary_notices, reasoning_capability_notices_neutral,
};
use super::codec::{AdaptationNotice, CodecError};
use super::ir::{CanonicalRequest, ReasoningIntent};
use super::registry::{
    CompactionCapabilities, ConfiguredWireProfile, WireProfileRegistry, WireRegistryError,
    WireSurface,
};
use crate::catalog::{CapabilityStatus, ThinkingCapability};
use crate::request::{NativeFeatureSummary, NativeRequestPreservation};

/// Map one EggPool capability status into the neutral kernel status.
pub const fn neutral_capability_status(status: CapabilityStatus) -> NeutralCapabilityStatus {
    match status {
        CapabilityStatus::Supported => NeutralCapabilityStatus::Supported,
        CapabilityStatus::Unsupported => NeutralCapabilityStatus::Unsupported,
        CapabilityStatus::Unknown => NeutralCapabilityStatus::Unknown,
        CapabilityStatus::Mixed => NeutralCapabilityStatus::Mixed,
        CapabilityStatus::Conflicting => NeutralCapabilityStatus::Conflicting,
    }
}

/// Map EggPool catalog thinking capability into neutral kernel facts.
pub fn neutral_thinking_capability(capability: &ThinkingCapability) -> NeutralThinkingCapability {
    NeutralThinkingCapability {
        status: neutral_capability_status(capability.status),
        toggle: neutral_capability_status(capability.toggle),
        effort: neutral_capability_status(capability.effort),
        budget: neutral_capability_status(capability.budget),
    }
}

/// Map EggPool native summary into neutral kernel facts.
pub fn neutral_native_summary(summary: &NativeFeatureSummary) -> NativeSummaryFacts {
    NativeSummaryFacts {
        native_input_items: summary.native_input_items,
        native_tool_definitions: summary.native_tool_definitions,
        extension_fields: summary.extension_fields.clone(),
        extensions_truncated: summary.extensions_truncated,
    }
}

/// EggPool-owned wrapper preserving the pre-extraction
/// `reasoning_capability_notices` signature.
///
/// Converts catalog facts into neutral kernel facts, then delegates to the
/// pure kernel. Semantics are byte/value-equivalent to the M001 behavior.
pub fn reasoning_capability_notices(
    request: &CanonicalRequest,
    capability: &ThinkingCapability,
    policy: &ReasoningCapabilityPolicy,
    target: WireSurface,
) -> Result<Vec<AdaptationNotice>, CodecError> {
    reasoning_capability_notices_neutral(
        request,
        &neutral_thinking_capability(capability),
        policy,
        target,
    )
}

/// EggPool-owned wrapper preserving the pre-extraction
/// `native_preservation_notices` signature.
pub fn native_preservation_notices(
    preservation: &NativeRequestPreservation,
    target: WireSurface,
) -> Result<Vec<AdaptationNotice>, CodecError> {
    native_summary_notices(&neutral_native_summary(&preservation.summary), target)
}

/// EggPool-owned adapter from reasoning intent into routing state.
///
/// The single seam replacement for the removed
/// `ReasoningIntent::to_thinking_requirement`.
pub fn thinking_requirement_from_intent(
    intent: &ReasoningIntent,
) -> Option<crate::routing::ThinkingRequirement> {
    intent
        .to_thinking_facts()
        .map(|facts| crate::routing::ThinkingRequirement {
            requested: facts.requested,
            requested_toggle: facts.requested_toggle,
            effort: facts.effort,
            budget_tokens: facts.budget_tokens,
            explicit_disable: facts.explicit_disable,
        })
}

/// Neutral surface-config facts consumed by the pure profile join.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SurfaceConfigFacts {
    pub path_template: String,
    pub stream_path_template: Option<String>,
    pub priority: u32,
}

impl SurfaceConfigFacts {
    pub fn from_config(config: &crate::config::ProviderWireSurfaceConfig) -> Self {
        Self {
            path_template: config.path_template.clone(),
            stream_path_template: config.stream_path_template.clone(),
            priority: config.priority,
        }
    }
}

/// Pure neutral join of static registry definitions to provider-owned path
/// metadata. The kernel owns this logic; EggPool only maps config types.
pub fn configured_profiles_from_facts(
    registry: &WireProfileRegistry,
    surfaces: &BTreeMap<String, SurfaceConfigFacts>,
) -> Result<Vec<ConfiguredWireProfile>, WireRegistryError> {
    let mut result = Vec::with_capacity(surfaces.len());
    for (surface_id, config) in surfaces {
        let surface = WireSurface::try_from(surface_id.as_str())
            .map_err(|_| WireRegistryError::ProviderUnknownProfile(surface_id.clone()))?;
        let definition = registry
            .get(surface)
            .ok_or_else(|| WireRegistryError::ProviderUnknownProfile(surface_id.clone()))?;
        result.push(ConfiguredWireProfile {
            definition: definition.clone(),
            path_template: config.path_template.clone(),
            stream_path_template: config.stream_path_template.clone(),
            priority: config.priority,
        });
    }
    result.sort_by_key(|profile| (profile.priority, profile.definition.surface));
    Ok(result)
}

/// EggPool-owned wrapper preserving `configured_profiles` over config types.
pub fn configured_profiles(
    registry: &WireProfileRegistry,
    surfaces: &BTreeMap<String, crate::config::ProviderWireSurfaceConfig>,
) -> Result<Vec<ConfiguredWireProfile>, WireRegistryError> {
    let facts: BTreeMap<String, SurfaceConfigFacts> = surfaces
        .iter()
        .map(|(key, config)| (key.clone(), SurfaceConfigFacts::from_config(config)))
        .collect();
    configured_profiles_from_facts(registry, &facts)
}

/// Pure neutral validation of provider references against neutral surface ids.
pub fn validate_provider_references_neutral(
    registry: &WireProfileRegistry,
    surface_ids: &[String],
    model_preferences: &BTreeMap<String, String>,
) -> Result<(), WireRegistryError> {
    for surface_id in surface_ids {
        if !registry.supports(
            WireSurface::try_from(surface_id.as_str())
                .map_err(|_| WireRegistryError::ProviderUnknownProfile(surface_id.clone()))?,
        ) {
            return Err(WireRegistryError::ProviderUnknownProfile(
                surface_id.clone(),
            ));
        }
    }
    for preferred in model_preferences.values() {
        let surface = WireSurface::try_from(preferred.as_str())
            .map_err(|_| WireRegistryError::ModelPreferenceUnavailable(preferred.clone()))?;
        if !surface_ids.iter().any(|id| id == surface.as_str()) {
            return Err(WireRegistryError::ModelPreferenceUnavailable(
                preferred.clone(),
            ));
        }
    }
    Ok(())
}

/// EggPool-owned wrapper preserving `validate_provider_references` over config.
pub fn validate_provider_references(
    registry: &WireProfileRegistry,
    surfaces: &BTreeMap<String, crate::config::ProviderWireSurfaceConfig>,
    model_preferences: &BTreeMap<String, crate::config::ModelWirePreference>,
) -> Result<(), WireRegistryError> {
    let surface_ids: Vec<String> = surfaces.keys().cloned().collect();
    let prefs: BTreeMap<String, String> = model_preferences
        .iter()
        .map(|(key, pref)| (key.clone(), pref.preferred_surface.clone()))
        .collect();
    validate_provider_references_neutral(registry, &surface_ids, &prefs)
}

/// EggPool-owned constructor for compaction capabilities from surface config.
pub fn compaction_capabilities_from_surface_config(
    config: &crate::config::ProviderWireSurfaceConfig,
) -> CompactionCapabilities {
    CompactionCapabilities {
        supports_remote_compaction_v1: config.supports_remote_compaction_v1,
        compact_path_template: config.compact_path_template.clone(),
        supports_remote_compaction_v2: config.supports_remote_compaction_v2,
    }
}
