//! M002 dependency-boundary guard, extended for the M003 crate cutover.
//!
//! The extractable wire kernel must not depend on EggPool routing, catalog,
//! config, request-runtime/resource, model-router, provider, database,
//! server, coordinator, Tokio, Axum, Hyper, TLS, or transport types.
//!
//! After the M003 cutover the single source of truth lives in
//! `rust/crates/eggpool-wire/src/`; the root `rust/src/wire/` kernel files
//! are facades. Both levels are scanned so the guard cannot pass vacuously,
//! and a second-implementation guard pins the root files to facades.

const KERNEL_SOURCES: &[(&str, &str)] = &[
    ("ir", include_str!("../src/wire/ir.rs")),
    ("adaptation", include_str!("../src/wire/adaptation.rs")),
    ("codec", include_str!("../src/wire/codec.rs")),
    ("codecs", include_str!("../src/wire/codecs.rs")),
    (
        "additional_codecs",
        include_str!("../src/wire/additional_codecs.rs"),
    ),
    ("decode", include_str!("../src/wire/decode.rs")),
    ("registry", include_str!("../src/wire/registry.rs")),
    ("stream", include_str!("../src/wire/stream.rs")),
    ("fidelity", include_str!("../src/wire/fidelity.rs")),
    ("provenance", include_str!("../src/wire/provenance.rs")),
    ("conformance", include_str!("../src/wire/conformance.rs")),
];

/// M003 single-source-of-truth locations. `registry.rs` moved to
/// `profile.rs`; every other module kept its file name.
const EXTRACTED_CRATE_SOURCES: &[(&str, &str)] = &[
    (
        "eggpool-wire/ir",
        include_str!("../crates/eggpool-wire/src/ir.rs"),
    ),
    (
        "eggpool-wire/adaptation",
        include_str!("../crates/eggpool-wire/src/adaptation.rs"),
    ),
    (
        "eggpool-wire/codec",
        include_str!("../crates/eggpool-wire/src/codec.rs"),
    ),
    (
        "eggpool-wire/codecs",
        include_str!("../crates/eggpool-wire/src/codecs.rs"),
    ),
    (
        "eggpool-wire/additional_codecs",
        include_str!("../crates/eggpool-wire/src/additional_codecs.rs"),
    ),
    (
        "eggpool-wire/decode",
        include_str!("../crates/eggpool-wire/src/decode.rs"),
    ),
    (
        "eggpool-wire/profile",
        include_str!("../crates/eggpool-wire/src/profile.rs"),
    ),
    (
        "eggpool-wire/stream",
        include_str!("../crates/eggpool-wire/src/stream.rs"),
    ),
    (
        "eggpool-wire/fidelity",
        include_str!("../crates/eggpool-wire/src/fidelity.rs"),
    ),
    (
        "eggpool-wire/provenance",
        include_str!("../crates/eggpool-wire/src/provenance.rs"),
    ),
    (
        "eggpool-wire/conformance",
        include_str!("../crates/eggpool-wire/src/conformance.rs"),
    ),
];

const FORBIDDEN: &[&str] = &[
    "crate::catalog",
    "crate::request",
    "crate::routing",
    "crate::config",
    "crate::model_router",
    "crate::providers",
    "crate::provider",
    "crate::db",
    "crate::server",
    "crate::coordinator",
    "crate::operations",
    "crate::health",
    "crate::accounts",
    "crate::quota",
    "tokio",
    "axum",
    "hyper",
    "reqwest",
    "eggress",
    "eggfetch",
    "rustls",
    "tokio-rustls",
    "sqlite",
    "rusqlite",
];

fn forbidden_use_violations(sources: &[(&str, &str)]) -> Vec<String> {
    let mut violations = Vec::new();
    for (module, source) in sources {
        // Only `use` statements count as dependencies; doc prose may name
        // EggPool owners to explain the seam.
        let uses: Vec<&str> = source
            .lines()
            .map(str::trim_start)
            .filter(|line| line.starts_with("use ") || line.starts_with("pub use "))
            .collect();
        for forbidden in FORBIDDEN {
            if uses.iter().any(|line| line.contains(forbidden)) {
                violations.push(format!("{module}.rs references {forbidden}"));
            }
        }
    }
    violations
}

#[test]
fn kernel_modules_have_no_forbidden_runtime_imports() {
    let mut violations = forbidden_use_violations(KERNEL_SOURCES);
    violations.extend(forbidden_use_violations(EXTRACTED_CRATE_SOURCES));
    assert!(
        violations.is_empty(),
        "forbidden kernel dependencies:\n{}",
        violations.join("\n")
    );
}

#[test]
fn kernel_seam_modules_exist() {
    // The seam is explicit: neutral kernel + EggPool adapters + decode limits.
    let adapters = include_str!("../src/wire/adapters.rs");
    assert!(adapters.contains("neutral_thinking_capability"));
    assert!(adapters.contains("neutral_native_summary"));
    assert!(adapters.contains("configured_profiles_from_facts"));
    // The implementation now lives in the extracted crate; the root decode
    // facade only re-exports it.
    let decode = include_str!("../crates/eggpool-wire/src/decode.rs");
    assert!(decode.contains("DecodeLimits::current") || decode.contains("pub const fn current"));
    assert!(decode.contains("canonical_request_from_object_with_limits"));
}

/// M003 guard against a second codec implementation: the root kernel files
/// must be thin facades over `eggpool-wire`, never duplicate definitions.
#[test]
fn root_kernel_modules_are_facades_without_duplicate_implementations() {
    const FACADE_CRATE: &[(&str, &str)] = &[
        ("ir", "eggpool_wire::ir"),
        ("adaptation", "eggpool_wire::adaptation"),
        ("codec", "eggpool_wire::codec"),
        ("codecs", "eggpool_wire::codecs"),
        ("additional_codecs", "eggpool_wire::additional_codecs"),
        ("decode", "eggpool_wire::decode"),
        ("registry", "eggpool_wire::profile"),
        ("stream", "eggpool_wire::stream"),
        ("fidelity", "eggpool_wire::fidelity"),
        ("provenance", "eggpool_wire::provenance"),
        ("conformance", "eggpool_wire::conformance"),
    ];
    // Implementation markers that must live only in `eggpool-wire`. If any
    // root facade grows one of these, the single-source-of-truth invariant
    // is broken.
    const IMPLEMENTATION_MARKERS: &[&str] = &[
        "pub struct CanonicalRequest",
        "pub enum CanonicalRole",
        "pub fn apply_adaptation_policy",
        "pub fn stable_tool_call_id",
        "pub trait WireCodec",
        "pub enum WireCodecId",
        "pub struct OpenAiChatCodec",
        "pub struct AnthropicMessagesCodec",
        "pub struct OpenAiResponsesCodec",
        "pub struct GeminiInteractionsCodec",
        "pub struct GeminiGenerateContentCodec",
        "pub fn builtin_codec_instance",
        "pub struct DecodeLimits",
        "pub enum DecodeError",
        "fn canonical_request_from_object",
        "fn canonical_request_from_value",
        "pub struct WireProfileRegistry",
        "pub enum WireSurface",
        "pub struct SseDecoder",
        "pub struct StreamEventDecoder",
        "pub fn decode_stream_event",
        "pub fn encode_client_event",
        "fn decode_request",
        "fn encode_request",
        "fn decode_response",
        "pub enum Fidelity",
        "pub struct TranslationPlan",
        "pub fn plan_request_translation",
        "pub struct WireProvenance",
        "pub fn may_restore_exact_for",
        "pub struct StreamConformanceVector",
        "pub fn stream_conformance_vectors",
        "pub fn sse_split_points",
    ];
    for ((module, source), (_, facade_path)) in KERNEL_SOURCES.iter().zip(FACADE_CRATE.iter()) {
        assert!(
            source.contains(&format!("pub use {facade_path}::")),
            "wire/{module}.rs must re-export {facade_path}"
        );
        for marker in IMPLEMENTATION_MARKERS {
            assert!(
                !source.contains(marker),
                "wire/{module}.rs must stay a facade but contains {marker:?}"
            );
        }
        // Facades stay small; anything larger is a reintroduced implementation.
        let lines = source.lines().count();
        assert!(
            lines <= 30,
            "wire/{module}.rs facade grew to {lines} lines (limit 30)"
        );
    }
    // The extracted crate must actually own every implementation marker.
    let crate_sources: Vec<&str> = EXTRACTED_CRATE_SOURCES
        .iter()
        .map(|(_, source)| *source)
        .collect();
    for marker in IMPLEMENTATION_MARKERS {
        assert!(
            crate_sources.iter().any(|source| source.contains(marker)),
            "no eggpool-wire source owns {marker:?}"
        );
    }
}
