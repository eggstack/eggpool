//! Provider-facing transport primitives.
//!
//! This module deliberately stops at neutral HTTP transport.  Provider
//! authentication, wire selection, account routing, and retry policy belong to
//! later migration milestones.

mod client_pool;
mod transport;

pub use client_pool::{
    AccountClientIdentity, ProviderClientPool, ProviderClientPoolError, ProviderClientPoolSnapshot,
};
pub use transport::{
    ProviderBody, ProviderHttpClient, ProviderHttpConfig, ProviderResponse, TransportError,
};
