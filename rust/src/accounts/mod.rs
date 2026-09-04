//! Request-independent account identity and credential boundaries.

mod registry;

pub use registry::{
    AccountIdentity, AccountRegistry, AccountRegistryError, CredentialStore, QuotaOffsets,
    RequestSurface,
};
