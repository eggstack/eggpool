//! EggPool-owned adapter over the shared `eggpool-provider-profile` contract.
//!
//! The bundled provider-profile document is owned by
//! [`eggpool_provider_profile`]. This module is the only place EggPool reads
//! it: the connect bootstrap, catalog refresh, and the parity tests all reach
//! the same canonical bytes through the shared crate rather than through a
//! second copy of provider facts.
//!
//! Projection is deliberately narrow. The shared contract carries provider
//! metadata only; accounts, credentials, keys, and every runtime policy stay in
//! [`crate::config`] and [`crate::operations`].

use std::collections::BTreeMap;
use std::sync::OnceLock;

use eggpool_provider_profile::{ProviderProfile, ProviderProfileRegistry, WireSurface};
use toml::{Table, Value};

/// The canonical embedded provider-profile document.
pub const BUNDLED_PROVIDER_PROFILES: &str =
    eggpool_provider_profile::EMBEDDED_PROVIDER_PROFILES_TOML;

/// Presentation-only keys that never reach the runtime configuration.
const PRESENTATION_KEYS: [&str; 6] = [
    "display_name",
    "status",
    "category",
    "region",
    "recommended",
    "notes",
];

/// Shared-contract verification fields with no runtime configuration
/// counterpart. They exist so a consumer can tell discovery from credential
/// proof; the runtime configuration schema is unchanged by that.
const CONTRACT_ONLY_VERIFY_KEYS: [&str; 1] = ["models_require_authentication"];

/// Parsed bundled profiles, validated once per process.
pub fn bundled_profiles()
-> Result<&'static ProviderProfileRegistry, eggpool_provider_profile::ProviderProfileError> {
    static REGISTRY: OnceLock<
        Result<ProviderProfileRegistry, eggpool_provider_profile::ProviderProfileError>,
    > = OnceLock::new();
    REGISTRY
        .get_or_init(ProviderProfileRegistry::embedded)
        .as_ref()
        .map_err(Clone::clone)
}

/// One bundled provider profile.
pub fn bundled_profile(
    id: &str,
) -> Result<&'static ProviderProfile, eggpool_provider_profile::ProviderProfileError> {
    bundled_profiles()?.require(id)
}

/// Project a shared profile onto the configuration shape EggPool loads.
///
/// This is the only projection path for bundled data: the connect bootstrap
/// renders it into a provider block, and the parity tests compare it against
/// the canonical document parsed directly.
pub fn provider_config_table(profile: &ProviderProfile) -> Table {
    let mut table = Table::new();
    table.insert("id".into(), Value::String(profile.id.clone()));
    table.insert("base_url".into(), Value::String(profile.base_url.clone()));
    table.insert(
        "protocols".into(),
        Value::Array(
            profile
                .protocols
                .iter()
                .map(|protocol| Value::String(protocol.clone()))
                .collect(),
        ),
    );

    table.insert("auth".into(), auth_table(&profile.auth));
    if !profile.wire_surfaces.is_empty() {
        let mut surfaces = Table::new();
        for (surface, entry) in &profile.wire_surfaces {
            let mut value = Table::new();
            value.insert(
                "path_template".into(),
                Value::String(entry.path_template.clone()),
            );
            if let Some(path) = &entry.stream_path_template {
                value.insert("stream_path_template".into(), Value::String(path.clone()));
            }
            value.insert("priority".into(), Value::Integer(i64::from(entry.priority)));
            if let Some(auth) = &entry.auth {
                value.insert("auth".into(), auth_table(auth));
            }
            if !entry.headers.is_empty() {
                value.insert("headers".into(), static_headers_value(&entry.headers));
            }
            if entry.supports_remote_compaction_v1 {
                value.insert("supports_remote_compaction_v1".into(), Value::Boolean(true));
            }
            if let Some(path) = &entry.compact_path_template {
                value.insert("compact_path_template".into(), Value::String(path.clone()));
            }
            if entry.supports_remote_compaction_v2 {
                value.insert("supports_remote_compaction_v2".into(), Value::Boolean(true));
            }
            surfaces.insert(surface.as_str().to_owned(), Value::Table(value));
        }
        table.insert("wire_surfaces".into(), Value::Table(surfaces));
    }
    if !profile.headers.is_empty() {
        table.insert("headers".into(), static_headers_value(&profile.headers));
    }
    if let Some(endpoint) = &profile.models_endpoint {
        let mut value = Table::new();
        value.insert("method".into(), Value::String(endpoint.method.clone()));
        value.insert("path".into(), Value::String(endpoint.path.clone()));
        if let Some(body) = &endpoint.body {
            value.insert("body".into(), body.clone());
        }
        if !endpoint.query.is_empty() {
            let mut query = Table::new();
            for (key, item) in &endpoint.query {
                query.insert(key.clone(), Value::String(item.clone()));
            }
            value.insert("query".into(), Value::Table(query));
        }
        value.insert("required".into(), Value::Boolean(endpoint.required));
        table.insert("models_endpoint".into(), Value::Table(value));
    }
    let mut verify = Table::new();
    if let Some(model) = &profile.verify.probe_model {
        verify.insert("probe_model".into(), Value::String(model.clone()));
    }
    verify.insert(
        "probe_protocol".into(),
        Value::String(
            profile
                .verify
                .probe_protocol
                .clone()
                .unwrap_or_else(|| "openai".into()),
        ),
    );
    verify.insert(
        "require_models".into(),
        Value::Boolean(profile.verify.require_models),
    );
    table.insert("verify".into(), Value::Table(verify));

    if !profile.model_wire.is_empty() {
        let mut model_wire = Table::new();
        for (model, preference) in &profile.model_wire {
            let mut value = Table::new();
            value.insert(
                "preferred_surface".into(),
                Value::String(preference.preferred_surface.as_str().to_owned()),
            );
            value.insert("fixed".into(), Value::Boolean(preference.fixed));
            model_wire.insert(model.clone(), Value::Table(value));
        }
        table.insert("model_wire".into(), Value::Table(model_wire));
    }
    if !profile.static_models.is_empty() {
        table.insert(
            "static_models".into(),
            Value::Array(
                profile
                    .static_models
                    .iter()
                    .map(|model| {
                        let mut value = Table::new();
                        value.insert("id".into(), Value::String(model.id.clone()));
                        if let Some(display) = &model.display_name {
                            value.insert("display_name".into(), Value::String(display.clone()));
                        }
                        if let Some(protocol) = &model.protocol {
                            value.insert("protocol".into(), Value::String(protocol.clone()));
                        }
                        for (key, limit) in [
                            ("max_context_tokens", model.max_context_tokens),
                            ("max_input_tokens", model.max_input_tokens),
                            ("max_output_tokens", model.max_output_tokens),
                        ] {
                            if let Some(limit) = limit {
                                value.insert(key.into(), Value::Integer(limit as i64));
                            }
                        }
                        for (key, flag) in [
                            ("supports_tools", model.supports_tools),
                            ("supports_vision", model.supports_vision),
                        ] {
                            if let Some(flag) = flag {
                                value.insert(key.into(), Value::Boolean(flag));
                            }
                        }
                        if !model.source_metadata.is_empty() {
                            let mut metadata = Table::new();
                            for (key, item) in &model.source_metadata {
                                metadata.insert(key.clone(), item.clone());
                            }
                            value.insert("source_metadata".into(), Value::Table(metadata));
                        }
                        Value::Table(value)
                    })
                    .collect(),
            ),
        );
    }
    if !profile.runtime_capabilities.is_empty() {
        let mut capabilities = Table::new();
        for (key, item) in &profile.runtime_capabilities {
            capabilities.insert(key.clone(), item.clone());
        }
        table.insert("model_capabilities".into(), Value::Table(capabilities));
    }

    for (key, value) in [
        ("openai_path", &profile.openai_path),
        ("anthropic_path", &profile.anthropic_path),
        ("responses_path", &profile.responses_path),
        ("models_method", &profile.models_method),
        ("models_path", &profile.models_path),
    ] {
        if let Some(value) = value {
            table.insert(key.into(), Value::String(value.clone()));
        }
    }
    table
}

/// The canonical document reduced to runtime configuration tables.
///
/// Presentation keys and contract-only verification keys are dropped, so the
/// result loads as a configuration document. This is the pre-extraction parse
/// path, kept as the comparison baseline.
pub fn canonical_config_document() -> Table {
    let parsed: Value = BUNDLED_PROVIDER_PROFILES
        .parse()
        .expect("embedded provider-profile document is valid TOML");
    let providers = parsed
        .get("providers")
        .and_then(Value::as_table)
        .expect("embedded document declares providers");
    let mut document = Table::new();
    let mut rendered = Table::new();
    for (id, raw) in providers {
        let Some(table) = raw.as_table() else {
            continue;
        };
        let mut value = table.clone();
        for key in PRESENTATION_KEYS {
            value.remove(key);
        }
        // `api_key_env` was a stripped placeholder in the bundled data; the
        // credential environment variable is derived from the provider id.
        value.remove("api_key_env");
        if let Some(verify) = value.get_mut("verify").and_then(Value::as_table_mut) {
            for key in CONTRACT_ONLY_VERIFY_KEYS {
                verify.remove(key);
            }
        }
        rendered.insert(id.clone(), Value::Table(value));
    }
    document.insert("providers".into(), Value::Table(rendered));
    document
}

/// Every bundled provider projected onto the runtime configuration shape.
pub fn projected_config_document() -> Result<Table, eggpool_provider_profile::ProviderProfileError>
{
    let registry = bundled_profiles()?;
    let mut providers = Table::new();
    for profile in registry.profiles() {
        providers.insert(
            profile.id.clone(),
            Value::Table(provider_config_table(profile)),
        );
    }
    let mut document = Table::new();
    document.insert("providers".into(), Value::Table(providers));
    Ok(document)
}

fn auth_table(auth: &eggpool_provider_profile::ProviderAuthProfile) -> Value {
    let mut table = Table::new();
    table.insert("mode".into(), Value::String(auth.mode.as_str().to_owned()));
    table.insert("header".into(), Value::String(auth.header.clone()));
    table.insert("scheme".into(), Value::String(auth.scheme.clone()));
    if !auth.additional.is_empty() {
        table.insert(
            "additional".into(),
            Value::Array(
                auth.additional
                    .iter()
                    .map(|item| {
                        let mut value = Table::new();
                        value.insert("mode".into(), Value::String(item.mode.as_str().to_owned()));
                        value.insert("header".into(), Value::String(item.header.clone()));
                        value.insert("scheme".into(), Value::String(item.scheme.clone()));
                        Value::Table(value)
                    })
                    .collect(),
            ),
        );
    }
    Value::Table(table)
}

fn static_headers_value(
    headers: &[eggpool_provider_profile::ProviderStaticHeaderProfile],
) -> Value {
    Value::Array(
        headers
            .iter()
            .map(|header| {
                let mut value = Table::new();
                value.insert("name".into(), Value::String(header.name.clone()));
                if let Some(header_value) = &header.value {
                    value.insert("value".into(), Value::String(header_value.clone()));
                }
                Value::Table(value)
            })
            .collect(),
    )
}

/// Exact advisory surface hints declared by bundled profiles.
pub fn bundled_model_wire_hints() -> BTreeMap<(String, String), WireSurface> {
    bundled_profiles()
        .map(|registry| {
            registry
                .profiles()
                .flat_map(|profile| {
                    profile.model_wire.iter().map(move |(model, preference)| {
                        (
                            (profile.id.clone(), model.clone()),
                            preference.preferred_surface,
                        )
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}
