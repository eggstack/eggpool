//! Portable client-configuration core for EggPool.
//!
//! This crate is the small reusable Rust boundary for portable connection
//! profiles and client configuration policy. It owns typed connection/profile
//! schemas, bounded token encode/decode, client-neutral projected model
//! types, target identification, pure rendering/mutation helpers,
//! ownership-state types, hashing, and validation that does not require
//! EggPool runtime state.
//!
//! The EggPool application continues to own loading `Config` and
//! database/catalog/model-info facts, producing the conservative projection
//! from authoritative server state, resolving server API-key policy, choosing
//! the advertised endpoint, CLI presentation/clipboard behavior, HTTP serving,
//! and runtime paths specific to the proxy process.
//!
//! The crate never depends on Axum, Tokio process/runtime features, SQLite,
//! Eggress, provider transport, routing, quota, health, or EggPool database
//! repositories. Transport encoding is `epc1.<base64url(canonical JSON)>`
//! without compression; the `epc1` codec version unambiguously fixes that
//! algorithm.

#![forbid(unsafe_code)]

pub mod adapter;
pub mod codex;
pub mod error;
pub mod hash;
pub mod integration_profile;
pub mod opencode;
pub mod ownership;
pub mod profile;
pub mod projection;
pub mod text;
pub mod token;

pub use adapter::{
    ClientAdapter, ClientDetection, ClientSchemaVariant, ClientTarget, ClientVersion, CodexAdapter,
    MutationPlan, OpencodeAdapter, VerificationPlan,
};
pub use codex::{
    apply_codex_text_mutation, build_codex_catalog_json, check_codex_state,
    remove_codex_owned_text, render_codex_toml, render_codex_toml_with_catalog,
    validate_codex_catalog_json, CodexPrevious, CodexStateCheck, EGGPOOL_API_KEY_ENV,
    MAX_CODEX_CATALOG_BYTES, MAX_CODEX_CATALOG_MODELS,
};
pub use error::ClientConfigError;
pub use hash::{hex_bytes, sha256_hex};
pub use integration_profile::{
    AgentIntegrationProfileV1, IntegrationCapabilities, INTEGRATION_PROFILE_SCHEMA_VERSION,
    MAX_INTEGRATION_MODELS, MAX_INTEGRATION_PROFILE_BYTES,
};
pub use opencode::{expected_opencode_provider, render_opencode_config, OPENCODE_RESPONSES_NPM};
pub use ownership::{
    OwnershipManifest, OWNED_CODEX_FIELDS, OWNED_OPENCODE_FIELDS, OWNERSHIP_SCHEMA_VERSION,
};
pub use profile::{
    AuthMode, AuthReference, ConnectionProfileV1, IntegrationProfileReference, IssuerMetadata,
    ProxyReference, WireProtocol, MAX_BASE_URL_BYTES, MAX_ENDPOINT_BYTES, MAX_ENV_BYTES,
    MAX_PROFILE_TARGETS, PROFILE_SCHEMA_ID, PROFILE_SCHEMA_MAJOR,
};
pub use projection::{
    aggregate_projections, project_model, project_models, AgentModelCapabilities,
    AgentModelProjection, AgentReasoningCapabilities, IntegrationModel, ModelLimits,
};
pub use text::{has_jsonc_comments, table_value};
pub use token::{decode_profile, encode_profile, MAX_DECODED_BYTES, MAX_TOKEN_CHARS, TOKEN_PREFIX};
