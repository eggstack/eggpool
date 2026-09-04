//! Typed, in-memory catalog state for routing-domain generations.

mod cache;

pub use cache::{
    AccountCatalogOutcome, AccountCatalogUpdateResult, AccountFreshness, CacheSnapshot,
    CapabilityStatus, CatalogCacheError, EffectiveModelLimits, ModelCapabilities,
    ModelCatalogCache, ModelIdentity, ModelInput, ProtocolResolutionStatus, ProviderModelIdentity,
    ThinkingCapability,
};
