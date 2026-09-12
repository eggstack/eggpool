//! Neutral deterministic semantic model-routing policy primitives.
//!
//! This crate deliberately does not know about HTTP, providers, catalogs,
//! configuration files, or application routing. Consumers adapt their local
//! configuration into [`ModelRouterPolicy`] values and retain ownership of
//! selector execution and concrete-provider selection.

mod identity;
mod policy;

pub use identity::{
    automatic_session_identity, session_identity_from_header, AffinityIdentityInput,
    ConversationPrefix, ConversationTextFragment, SessionIdentity, SessionSource,
    AFFINITY_SESSION_HEADER_MAX_BYTES, AUTOMATIC_FIRST_USER_MIN_BYTES, AUTOMATIC_PREFIX_MAX_BYTES,
};
pub use policy::{
    compile_model_router, validate_model_router_mapping, CompiledModelRoute, CompiledModelRouter,
    ModelRoutePolicy, ModelRouterPolicy, ModelRouterRegistry, ModelRoutingError,
    COMPILED_POLICY_MAX_BYTES, SELECTOR_PROTOCOL_VERSION,
};
