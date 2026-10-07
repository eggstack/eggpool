//! Secret-free wire-surface re-export and adapter boundary.
//!
//! The crate never defines a second wire-surface vocabulary. It references the
//! surface identities owned by [`eggpool_wire`] so a downstream consumer that
//! already depends on the wire kernel selects, encodes, and negotiates exactly
//! the surfaces it names here.
//!
//! `eggpool-wire` intentionally owns no credential, catalog, routing, or
//! provider facts, so this edge creates no dependency cycle.

pub use eggpool_wire::profile::WireSurface;
