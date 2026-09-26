//! Immutable static wire-profile registry.
//!
//! Compatibility facade: the neutral registry implementation lives in the
//! `eggpool-wire` workspace crate (`eggpool_wire::profile`). EggPool-owned
//! config joins stay in `wire::adapters`. The `wire::registry` path keeps
//! working for existing imports; the canonical types are type-identical to
//! the crate types.

pub use eggpool_wire::profile::*;
