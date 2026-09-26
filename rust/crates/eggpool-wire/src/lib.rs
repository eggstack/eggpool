//! Neutral sans-I/O wire kernel for EggPool (internal; `publish = false`).
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
//!
//! # Three-layer model
//!
//! * **Semantic IR** ([`ir`]): provider-neutral meaning — canonical requests,
//!   responses, and events. Codecs translate between wire grammars and this
//!   layer; it never carries opaque provider JSON.
//! * **Provenance** ([`provenance`]): bounded source-native residue with no
//!   canonical projection (source identity, counts, structural field paths).
//!   Separate from the IR, redaction-safe in `Debug`, same-request lifetime,
//!   no persistence contract. Cross-surface encoding never consults it.
//! * **Adaptation and fidelity** ([`adaptation`], [`fidelity`]): the shared
//!   decision engine (`request_notices`, `native_summary_notices`, loss
//!   policy) plus the preflight [`TranslationPlan`](fidelity::TranslationPlan)
//!   projection. The planner and the encoders agree by construction.
//!
//! # Codec coverage
//!
//! Five finite surfaces ([`profile::WireSurface`]) with request/response
//! codecs ([`codecs`], [`additional_codecs`]) and five streaming dialects
//! ([`codec::StreamAdapterKind`]) with incremental decoders ([`stream`]).
//! Protocol-only conformance vectors live in [`conformance`].
//!
//! # Limits and guarantees
//!
//! * MSRV 1.89. No `unsafe` code (`unsafe_code = "forbid"`).
//! * No I/O: pure functions over caller-supplied bytes and values. Byte
//!   forwarding stays caller-owned; observation stays in the kernel.
//! * BoundedEverything attacker-controlled is capped: adaptation notices
//!   ([`adaptation::MAX_ADAPTATION_NOTICES`]), SSE frames
//!   ([`stream::MAX_SSE_FRAME_BYTES`]), provenance fragments and bytes
//!   ([`provenance::MAX_PROVENANCE_FRAGMENTS`],
//!   [`provenance::MAX_PROVENANCE_TOTAL_BYTES`]).
//! * Unsupported semantics stay blockers: unknown or provider-native fields
//!   are never silently dropped on a path that claims exactness, and
//!   provider-owned signatures, encrypted reasoning, IDs, and terminal
//!   events are never synthesized.
//!
//! # Comparison boundary
//!
//! This is a protocol kernel, not an SDK, provider client, router, or agent
//! framework: no HTTP clients, auth, catalogs, retries, routing, tool
//! execution, MCP, storage, or server APIs live here.

pub mod adaptation;
pub mod additional_codecs;
pub mod codec;
pub mod codecs;
pub mod conformance;
pub mod decode;
pub mod fidelity;
pub mod ir;
pub mod profile;
pub mod provenance;
pub mod stream;

pub use conformance::{StreamConformanceVector, sse_split_points, stream_conformance_vectors};
pub use fidelity::{
    AdaptationEffect, AdaptationEffectClass, Fidelity, TranslationPlan, classify_notice,
    effects_for_notices, fidelity_for_response_notices, plan_request_translation,
};
pub use provenance::{
    MAX_PROVENANCE_DEPTH, MAX_PROVENANCE_FRAGMENTS, MAX_PROVENANCE_NAME_BYTES,
    MAX_PROVENANCE_TOTAL_BYTES, ProvenanceCompleteness, ProvenanceFragment, ProvenanceShape,
    TruncationReason, WireProvenance, may_restore_exact, may_restore_exact_for,
};
