//! Package version sourced from Cargo metadata.

/// The candidate version is supplied by `Cargo.toml` at compile time.
pub const PACKAGE_VERSION: &str = env!("CARGO_PKG_VERSION");
