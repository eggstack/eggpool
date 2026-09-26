//! Bounded source-native provenance, separate from the semantic IR.
//!
//! The canonical request/response types carry provider-neutral meaning.
//! `WireProvenance` carries the bounded, source-native residue that has no
//! canonical projection: which surface and codec observed the payload, how
//! many native items/tools were seen (counts only, never content), and a
//! bounded list of extension field paths with shape metadata. It never
//! retains credentials, prompts, raw payloads, tool arguments, or complete
//! streams, and it has no persistence contract: it is an owned value with
//! same-request/response lifetime. Dropping it releases everything.
//!
//! # Completeness contract
//!
//! Every builder enforces the hard ceilings ([`MAX_PROVENANCE_FRAGMENTS`],
//! [`MAX_PROVENANCE_NAME_BYTES`], [`MAX_PROVENANCE_TOTAL_BYTES`],
//! [`MAX_PROVENANCE_DEPTH`]). On budget exhaustion the builder truncates and
//! records [`ProvenanceCompleteness::Truncated`] with a
//! [`TruncationReason`] — it never silently discards data while claiming
//! exactness. [`WireProvenance::is_exact_capable`] is false whenever the
//! provenance is incomplete or truncated.
//!
//! # Same-surface restore contract
//!
//! Exact or native reconstruction may consult provenance only when
//! [`may_restore_exact_for`] holds: provenance is complete **and** the target
//! surface equals the recorded source surface. Cross-surface encoding
//! operates from the semantic IR plus the fidelity plan/effects only and
//! must never inject source-native extras into another provider grammar.
//! [`may_restore_exact`] reports the target-independent half (completeness);
//! callers must additionally match the surface.
//!
//! # Redaction
//!
//! `Debug` reports counts, surfaces, completeness, and truncation state
//! only. Fragment names (structural field paths) are never rendered, so
//! `Debug` output is safe to log.

use crate::adaptation::NativeSummaryFacts;
use crate::codec::WireCodecId;
use crate::profile::WireSurface;

/// Maximum retained source fragments per provenance value.
pub const MAX_PROVENANCE_FRAGMENTS: usize = 32;
/// Maximum bytes per retained fragment name (truncated at a char boundary).
pub const MAX_PROVENANCE_NAME_BYTES: usize = 256;
/// Maximum total retained fragment-name bytes per provenance value.
pub const MAX_PROVENANCE_TOTAL_BYTES: usize = 16 * 1024;
/// Maximum representable nesting depth inferred from field-path separators.
pub const MAX_PROVENANCE_DEPTH: usize = 8;

/// Whether the provenance value observed its whole source.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ProvenanceCompleteness {
    /// Every observed source fragment is retained within budget.
    Complete,
    /// At least one fragment was dropped or shortened; see the reason.
    /// An incomplete value can never claim exact restoration.
    Truncated,
}

/// Why a provenance value stopped retaining source fragments.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TruncationReason {
    /// More fragments existed than [`MAX_PROVENANCE_FRAGMENTS`], a fragment
    /// name exceeded [`MAX_PROVENANCE_NAME_BYTES`], or the source already
    /// reported truncation upstream.
    FieldBudget,
    /// Retained names exceeded [`MAX_PROVENANCE_TOTAL_BYTES`].
    ByteBudget,
    /// A field path implied nesting deeper than [`MAX_PROVENANCE_DEPTH`].
    DepthBudget,
}

/// One bounded source-native fragment: structural identity only.
///
/// `kind` is a closed vocabulary string (`"extension_field"` today);
/// `name` is a structural field path, never content. `size_bytes` is the
/// retained name length in bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProvenanceFragment {
    pub kind: String,
    pub name: String,
    pub size_bytes: usize,
}

/// Bounded shape metadata for same-surface reconstruction decisions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProvenanceShape {
    /// Retained extension fragments plus observed native item/tool counts.
    /// Native items/tools contribute counts only, never content.
    pub field_count: usize,
    /// Maximum nesting depth inferred from field paths, saturated at
    /// [`MAX_PROVENANCE_DEPTH`].
    pub max_depth: usize,
    /// Total retained fragment-name bytes.
    pub total_bytes: usize,
}

/// Bounded, redaction-safe source-native provenance for one request/response.
///
/// Separate from the semantic IR by construction: this type cannot carry
/// prompts, raw payloads, credentials, or stream bytes — only the source
/// identity, counts, and bounded structural field paths described above.
#[derive(Clone, PartialEq, Eq)]
pub struct WireProvenance {
    pub source_surface: WireSurface,
    pub source_codec: WireCodecId,
    pub completeness: ProvenanceCompleteness,
    pub truncation_reason: Option<TruncationReason>,
    pub fragments: Vec<ProvenanceFragment>,
    pub shape: ProvenanceShape,
    native_items: usize,
    native_tools: usize,
}

impl std::fmt::Debug for WireProvenance {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("WireProvenance")
            .field("source_surface", &self.source_surface)
            .field("source_codec", &self.source_codec)
            .field("completeness", &self.completeness)
            .field("truncation_reason", &self.truncation_reason)
            .field("fragment_count", &self.fragments.len())
            .field("shape", &self.shape)
            .field("native_items", &self.native_items)
            .field("native_tools", &self.native_tools)
            .finish()
        // Deliberately omits fragment names: Debug is safe to log.
    }
}

impl WireProvenance {
    /// Empty provenance: no source extras observed.
    ///
    /// Complete by construction — there is nothing to restore, so nothing
    /// precludes exactness on its own (fidelity notices still apply).
    /// Non-Responses surfaces begin here rather than pretending round-trip
    /// fidelity they do not have.
    pub fn empty(source_surface: WireSurface, source_codec: WireCodecId) -> Self {
        Self {
            source_surface,
            source_codec,
            completeness: ProvenanceCompleteness::Complete,
            truncation_reason: None,
            fragments: Vec::new(),
            shape: ProvenanceShape {
                field_count: 0,
                max_depth: 0,
                total_bytes: 0,
            },
            native_items: 0,
            native_tools: 0,
        }
    }

    /// Build provenance from native summary facts (e.g. Responses preservation).
    ///
    /// Extension field names become bounded `"extension_field"` fragments;
    /// native input items and tool definitions contribute to
    /// [`ProvenanceShape::field_count`] as counts only — their content is
    /// never retained. Over-ceiling input truncates (fragments dropped,
    /// names shortened at char boundaries, depth saturated) and records
    /// [`ProvenanceCompleteness::Truncated`] with the first applicable
    /// [`TruncationReason`]; an upstream `extensions_truncated` flag maps to
    /// [`TruncationReason::FieldBudget`].
    pub fn from_native_summary(
        summary: &NativeSummaryFacts,
        source_surface: WireSurface,
        source_codec: WireCodecId,
    ) -> Self {
        let mut provenance = Self::empty(source_surface, source_codec);
        provenance.native_items = summary.native_input_items;
        provenance.native_tools = summary.native_tool_definitions;
        if summary.extensions_truncated {
            provenance.mark_truncated(TruncationReason::FieldBudget);
        }
        let mut max_depth = 0usize;
        for field in &summary.extension_fields {
            if provenance.fragments.len() >= MAX_PROVENANCE_FRAGMENTS {
                provenance.mark_truncated(TruncationReason::FieldBudget);
                break;
            }
            let name = truncate_name_bytes(field, MAX_PROVENANCE_NAME_BYTES);
            if name.len() < field.len() {
                provenance.mark_truncated(TruncationReason::FieldBudget);
            }
            let depth = estimate_depth(&name);
            if depth > MAX_PROVENANCE_DEPTH {
                provenance.mark_truncated(TruncationReason::DepthBudget);
            }
            max_depth = max_depth.max(depth.min(MAX_PROVENANCE_DEPTH));
            let size_bytes = name.len();
            if provenance.shape.total_bytes.saturating_add(size_bytes) > MAX_PROVENANCE_TOTAL_BYTES
            {
                provenance.mark_truncated(TruncationReason::ByteBudget);
                break;
            }
            provenance.shape.total_bytes += size_bytes;
            provenance.fragments.push(ProvenanceFragment {
                kind: "extension_field".into(),
                name,
                size_bytes,
            });
        }
        provenance.shape.max_depth = max_depth;
        provenance.shape.field_count =
            provenance.fragments.len() + provenance.native_items + provenance.native_tools;
        provenance
    }

    /// True only when every observed source fragment is retained in budget.
    pub fn is_complete(&self) -> bool {
        self.completeness == ProvenanceCompleteness::Complete
    }

    /// False when incomplete or truncated: such a value can never support an
    /// exact-restoration claim for fields outside the semantic model.
    /// Completeness is necessary but not sufficient — fidelity notices still
    /// apply, and the target surface must match (see
    /// [`may_restore_exact_for`]).
    pub fn is_exact_capable(&self) -> bool {
        self.is_complete()
    }

    fn mark_truncated(&mut self, reason: TruncationReason) {
        self.completeness = ProvenanceCompleteness::Truncated;
        if self.truncation_reason.is_none() {
            self.truncation_reason = Some(reason);
        }
    }
}

fn truncate_name_bytes(name: &str, limit: usize) -> String {
    if name.len() <= limit {
        return name.to_owned();
    }
    let mut end = limit;
    while end > 0 && !name.is_char_boundary(end) {
        end -= 1;
    }
    name[..end].to_owned()
}

/// Heuristic nesting depth from structural separators (`.` and `[`).
/// Field paths are structural, never content; the estimate only bounds
/// retention, it never gates semantic decisions.
fn estimate_depth(name: &str) -> usize {
    name.bytes()
        .filter(|byte| *byte == b'.' || *byte == b'[')
        .count()
}

/// Target-independent restore eligibility: true only for complete
/// provenance. Callers must additionally require the target surface to equal
/// [`WireProvenance::source_surface`]; see [`may_restore_exact_for`].
/// Cross-surface encoding must never consult fragments.
pub fn may_restore_exact(provenance: &WireProvenance) -> bool {
    provenance.is_exact_capable()
}

/// Full same-surface restore contract: complete provenance **and** the target
/// surface equals the recorded source surface. Incomplete or truncated
/// provenance, or any cross-surface target, returns false.
pub fn may_restore_exact_for(provenance: &WireProvenance, target: WireSurface) -> bool {
    provenance.is_exact_capable() && provenance.source_surface == target
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::codec::WireCodecId;
    use crate::profile::WireSurface;

    fn summary_with_extensions(count: usize) -> NativeSummaryFacts {
        NativeSummaryFacts {
            native_input_items: 0,
            native_tool_definitions: 0,
            extension_fields: (0..count).map(|index| format!("extra_{index}")).collect(),
            extensions_truncated: false,
        }
    }

    #[test]
    fn empty_provenance_is_complete_and_exact_capable() {
        let provenance = WireProvenance::empty(
            WireSurface::AnthropicMessages,
            WireCodecId::AnthropicMessages,
        );
        assert!(provenance.is_complete());
        assert!(provenance.is_exact_capable());
        assert!(may_restore_exact(&provenance));
        assert!(may_restore_exact_for(
            &provenance,
            WireSurface::AnthropicMessages
        ));
        assert!(!may_restore_exact_for(
            &provenance,
            WireSurface::OpenaiResponses
        ));
    }

    #[test]
    fn from_native_summary_maps_counts_not_content() {
        let summary = NativeSummaryFacts {
            native_input_items: 2,
            native_tool_definitions: 1,
            extension_fields: vec!["input[0].odd".into(), "top".into()],
            extensions_truncated: false,
        };
        let provenance = WireProvenance::from_native_summary(
            &summary,
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert!(provenance.is_complete());
        assert!(provenance.is_exact_capable());
        assert_eq!(provenance.fragments.len(), 2);
        assert!(provenance.fragments.iter().all(|fragment| {
            fragment.kind == "extension_field" && fragment.size_bytes == fragment.name.len()
        }));
        // Counts recorded; no content retained.
        assert_eq!(provenance.shape.field_count, 2 + 2 + 1);
        assert_eq!(provenance.shape.max_depth, 2);
        assert!(may_restore_exact_for(
            &provenance,
            WireSurface::OpenaiResponses
        ));
        assert!(!may_restore_exact_for(
            &provenance,
            WireSurface::OpenaiChatCompletions
        ));
    }

    #[test]
    fn fragment_budget_truncates_with_reason_and_blocks_exact() {
        let summary = summary_with_extensions(MAX_PROVENANCE_FRAGMENTS + 5);
        let provenance = WireProvenance::from_native_summary(
            &summary,
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert_eq!(provenance.fragments.len(), MAX_PROVENANCE_FRAGMENTS);
        assert!(!provenance.is_complete());
        assert!(!provenance.is_exact_capable());
        assert!(!may_restore_exact(&provenance));
        assert!(!may_restore_exact_for(
            &provenance,
            WireSurface::OpenaiResponses
        ));
        assert_eq!(
            provenance.truncation_reason,
            Some(TruncationReason::FieldBudget)
        );
    }

    #[test]
    fn byte_budget_holds_at_full_fragment_and_name_budgets() {
        // The fragment (32) and per-name (256 B) ceilings compose to at most
        // 8 KiB retained, so a fully loaded provenance value stays within
        // the 16 KiB total budget with headroom. The byte-budget arm remains
        // as defense-in-depth if ceilings ever change.
        let summary = NativeSummaryFacts {
            native_input_items: 0,
            native_tool_definitions: 0,
            extension_fields: (0..MAX_PROVENANCE_FRAGMENTS)
                .map(|index| format!("f{index:02}_{}", "x".repeat(200)))
                .collect(),
            extensions_truncated: false,
        };
        for field in &summary.extension_fields {
            assert!(field.len() <= MAX_PROVENANCE_NAME_BYTES);
        }
        let provenance = WireProvenance::from_native_summary(
            &summary,
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert_eq!(provenance.fragments.len(), MAX_PROVENANCE_FRAGMENTS);
        assert!(provenance.is_complete());
        assert!(provenance.is_exact_capable());
        assert!(provenance.shape.total_bytes <= MAX_PROVENANCE_TOTAL_BYTES);
        assert!(provenance.truncation_reason.is_none());
    }

    #[test]
    fn overlong_names_shorten_at_char_boundaries_and_mark_truncated() {
        let long_name = format!("{}{}", "é".repeat(200), "x".repeat(200));
        let summary = NativeSummaryFacts {
            native_input_items: 0,
            native_tool_definitions: 0,
            extension_fields: vec![long_name],
            extensions_truncated: false,
        };
        let provenance = WireProvenance::from_native_summary(
            &summary,
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert_eq!(provenance.fragments.len(), 1);
        assert!(provenance.fragments[0].name.len() <= MAX_PROVENANCE_NAME_BYTES);
        assert!(
            provenance.fragments[0]
                .name
                .is_char_boundary(provenance.fragments[0].name.len())
        );
        assert!(!provenance.is_exact_capable());
        assert_eq!(
            provenance.truncation_reason,
            Some(TruncationReason::FieldBudget)
        );
    }

    #[test]
    fn deep_paths_saturate_depth_and_mark_truncated() {
        let deep = (0..20)
            .map(|index| format!("f{index}"))
            .collect::<Vec<_>>()
            .join(".");
        let summary = NativeSummaryFacts {
            native_input_items: 0,
            native_tool_definitions: 0,
            extension_fields: vec![deep],
            extensions_truncated: false,
        };
        let provenance = WireProvenance::from_native_summary(
            &summary,
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert_eq!(provenance.shape.max_depth, MAX_PROVENANCE_DEPTH);
        assert!(!provenance.is_exact_capable());
        assert_eq!(
            provenance.truncation_reason,
            Some(TruncationReason::DepthBudget)
        );
    }

    #[test]
    fn upstream_truncation_flag_maps_to_field_budget() {
        let summary = NativeSummaryFacts {
            native_input_items: 0,
            native_tool_definitions: 0,
            extension_fields: vec!["a".into()],
            extensions_truncated: true,
        };
        let provenance = WireProvenance::from_native_summary(
            &summary,
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert!(!provenance.is_complete());
        assert!(!provenance.is_exact_capable());
        assert_eq!(
            provenance.truncation_reason,
            Some(TruncationReason::FieldBudget)
        );
    }

    #[test]
    fn debug_is_redaction_safe_at_full_budget() {
        let summary = summary_with_extensions(MAX_PROVENANCE_FRAGMENTS);
        let provenance = WireProvenance::from_native_summary(
            &summary,
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert_eq!(provenance.fragments.len(), MAX_PROVENANCE_FRAGMENTS);
        let rendered = format!("{provenance:?}");
        for index in 0..MAX_PROVENANCE_FRAGMENTS {
            assert!(
                !rendered.contains(&format!("extra_{index}")),
                "Debug must not contain fragment names"
            );
        }
        assert!(rendered.contains("fragment_count"));
        assert!(rendered.contains("Complete"));
        assert!(rendered.contains("OpenaiResponses"));
    }

    #[test]
    fn incomplete_provenance_cannot_claim_exact() {
        // Every truncation path must agree: no exact capability, no restore.
        let truncated: Vec<WireProvenance> = vec![
            WireProvenance::from_native_summary(
                &summary_with_extensions(MAX_PROVENANCE_FRAGMENTS + 1),
                WireSurface::OpenaiResponses,
                WireCodecId::OpenaiResponses,
            ),
            WireProvenance::from_native_summary(
                &NativeSummaryFacts {
                    native_input_items: 3,
                    native_tool_definitions: 0,
                    extension_fields: Vec::new(),
                    extensions_truncated: true,
                },
                WireSurface::OpenaiResponses,
                WireCodecId::OpenaiResponses,
            ),
        ];
        for provenance in &truncated {
            assert!(!provenance.is_exact_capable());
            assert!(!may_restore_exact(provenance));
            for target in WireSurface::ALL {
                assert!(!may_restore_exact_for(provenance, target));
            }
        }
    }

    #[test]
    fn cross_surface_encode_never_injects_provenance_extras() {
        // Provenance is not an encode input at the type level; this pins the
        // contract end to end: a provenance value loaded with extension
        // fragments cannot leak marker strings into cross-surface output.
        use crate::codec::WireCodec;
        use crate::decode::{DecodeLimits, canonical_request_from_value_with_limits};
        use crate::ir::ClientSurface;
        use crate::profile::{ConfiguredWireProfile, WireProfileDefinition};
        use serde_json::json;

        const MARKER: &str = "zz_provenance_marker_not_in_ir";
        let request = canonical_request_from_value_with_limits(
            &json!({
                "model": "model-a",
                "messages": [{"role": "user", "content": "hello"}],
            }),
            ClientSurface::ChatCompletions,
            DecodeLimits::current(),
        )
        .expect("fixture decodes");
        let provenance = WireProvenance::from_native_summary(
            &NativeSummaryFacts {
                native_input_items: 0,
                native_tool_definitions: 0,
                extension_fields: vec![MARKER.into()],
                extensions_truncated: false,
            },
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
        );
        assert!(
            provenance
                .fragments
                .iter()
                .any(|fragment| fragment.name == MARKER)
        );
        assert!(!may_restore_exact_for(
            &provenance,
            WireSurface::AnthropicMessages
        ));
        let codec = crate::codecs::AnthropicMessagesCodec;
        let profile = ConfiguredWireProfile {
            definition: WireProfileDefinition {
                surface: WireSurface::AnthropicMessages,
                request_codec: crate::codec::WireCodecId::AnthropicMessages,
                response_codec: crate::codec::WireCodecId::AnthropicMessages,
                stream_codec: crate::codec::WireCodecId::AnthropicMessages,
            },
            path_template: "/m004".into(),
            stream_path_template: None,
            priority: 0,
        };
        let output = codec
            .encode_request(&request, &profile)
            .expect("cross-surface encode succeeds");
        let rendered = serde_json::to_string(&output.value).expect("output serializes");
        assert!(
            !rendered.contains(MARKER),
            "cross-surface output must not contain provenance extras"
        );
    }
}
