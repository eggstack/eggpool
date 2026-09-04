//! Provider-facing transport primitives.
//!
//! This module deliberately stops at neutral HTTP transport.  Provider
//! authentication, wire selection, account routing, and retry policy belong to
//! later migration milestones.

mod transport;

pub use transport::{
    ProviderBody, ProviderHttpClient, ProviderHttpConfig, ProviderResponse, TransportError,
};
