//! Neutral sans-I/O wire kernel for EggPool.
//!
//! This crate owns the canonical request/response/event types, the pure
//! structural decoder, adaptation policy, finite protocol codecs, the
//! wire-profile registry data types, and the streaming state machines. It has
//! no credential, environment, filesystem, network, clock, random, async
//! runtime, database, or logging side effects.
//!
//! EggPool-owned joining logic (request admission, profile selection from
//! config, routing/catalog adaptation, runtime objects, transport, and HTTP
//! status mapping) stays in the `eggpool` root package, which consumes this
//! crate as the single source of truth through `src/wire/` facades.

pub mod adaptation;
pub mod additional_codecs;
pub mod codec;
pub mod codecs;
pub mod decode;
pub mod ir;
pub mod profile;
pub mod stream;
