//! Preflight translation fidelity over the shared adaptation decision engine.
//!
//! Compatibility facade: the implementation lives in the `eggpool-wire`
//! workspace crate. EggPool does not route, admit, or select providers based
//! on fidelity; the planner is an additive preflight API only.

pub use eggpool_wire::fidelity::*;
