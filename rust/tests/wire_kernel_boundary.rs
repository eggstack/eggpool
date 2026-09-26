//! M002 dependency-boundary guard.
//!
//! The extractable wire kernel must not depend on EggPool routing, catalog,
//! config, request-runtime/resource, model-router, provider, database,
//! server, coordinator, Tokio, Axum, Hyper, TLS, or transport types.

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

#[test]
fn kernel_modules_have_no_forbidden_runtime_imports() {
    let mut violations = Vec::new();
    for (module, source) in KERNEL_SOURCES {
        // Only `use` statements count as dependencies; doc prose may name
        // EggPool owners to explain the seam.
        let uses: Vec<&str> = source
            .lines()
            .map(str::trim_start)
            .filter(|line| line.starts_with("use "))
            .collect();
        for forbidden in FORBIDDEN {
            if uses.iter().any(|line| line.contains(forbidden)) {
                violations.push(format!("wire/{module}.rs references {forbidden}"));
            }
        }
    }
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
    let decode = include_str!("../src/wire/decode.rs");
    assert!(decode.contains("DecodeLimits::current") || decode.contains("pub const fn current"));
    assert!(decode.contains("canonical_request_from_object_with_limits"));
}
