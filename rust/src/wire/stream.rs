//! Bounded SSE framing and canonical streaming semantics.
//!
//! Compatibility facade: the implementation lives in the `eggpool-wire`
//! workspace crate. The EggPool runtime join that decides NativeObserved vs
//! Translated and forwards bytes stays in `wire::runtime`.

pub use eggpool_wire::stream::*;
