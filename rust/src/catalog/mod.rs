//! Typed, in-memory catalog state for routing-domain generations.

mod cache;
mod refresh;

pub use cache::{
    AccountCatalogOutcome, AccountCatalogUpdateResult, AccountFreshness, CacheSnapshot,
    CapabilityStatus, CatalogCacheError, EffectiveModelLimits, ModelCapabilities,
    ModelCatalogCache, ModelIdentity, ModelInput, ProtocolResolutionStatus, ProviderModelIdentity,
    ThinkingCapability,
};
pub use refresh::{
    AccountCatalogFetch, CatalogModelEvent, CatalogRefreshError, CatalogRefreshResult,
    CatalogService, CatalogTransportObservation, ModelReappearance, RefreshOutcome,
};
