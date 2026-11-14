//! Provider-facing transport primitives.
//!
//! This module stops at neutral HTTP transport. Provider authentication, wire
//! selection, account routing, and retry policy live in their owning modules.

mod client_pool;
mod transport;

pub use client_pool::{
    AccountClientIdentity, ProviderClientPool, ProviderClientPoolCloseReport,
    ProviderClientPoolError, ProviderClientPoolSnapshot,
};
pub use transport::{
    ProviderBody, ProviderHttpClient, ProviderHttpConfig, ProviderResponse, TransportError,
};
