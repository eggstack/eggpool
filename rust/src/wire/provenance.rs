//! Bounded source-native provenance, separate from the semantic IR.
//!
//! Compatibility facade: the implementation lives in the `eggpool-wire`
//! workspace crate. EggPool-owned preservation mapping stays in
//! `wire::adapters`.

pub use eggpool_wire::provenance::*;
