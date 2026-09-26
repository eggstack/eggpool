//! Canonical request/response/event representation.
//!
//! Compatibility facade: the implementation lives in the `eggpool-wire`
//! workspace crate. This module remains the EggPool canonical boundary and
//! re-exports the single source of truth so all canonical types stay
//! type-identical across admission, routing, runtime, codecs, and tests.

pub use eggpool_wire::ir::*;
