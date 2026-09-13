//! Streaming coordinator internals.
//!
//! The streaming boundary deliberately has two ownership phases:
//!
//! ```text
//! server/Axum
//!    -> StreamingCoordinator (pre-handoff attempts and timeouts)
//!       -> provider transport
//!       -> WireStream (incremental protocol semantics)
//!    -> StreamingExecution (post-handoff body and cancellation)
//!       -> retained finalization/accounting
//! ```
//!
//! Retry authorization ends when [`StreamingExecution`] is returned. The
//! coordinator may classify `WireStream` terminal summaries, but protocol
//! decoding remains owned by the wire runtime. These modules are an internal
//! decomposition; the public streaming API is re-exported here so callers do
//! not depend on implementation filenames.

mod coordinator;
mod diagnostics;
mod execution;
mod terminal;
mod timeout;
mod types;

pub use coordinator::StreamingCoordinator;
pub use diagnostics::{
    OUTCOME_CLIENT_CANCELLED, OUTCOME_COMPLETED_CANONICAL, OUTCOME_COMPLETED_COMPATIBILITY,
    OUTCOME_EMPTY_EOF, OUTCOME_FIRST_BYTE_TIMEOUT, OUTCOME_IDLE_TIMEOUT, OUTCOME_MALFORMED_EOF,
    OUTCOME_PREMATURE_EOF_BEFORE_BODY, OUTCOME_PREMATURE_EOF_MIDSTREAM,
    OUTCOME_RESPONSE_HEADER_TIMEOUT, OUTCOME_TERMINAL_FAILURE, OUTCOME_TERMINAL_INCOMPLETE,
    OUTCOME_UPSTREAM_MIDSTREAM_ERROR, StreamChunkError, StreamDiagnosticEvent, StreamDiagnostics,
    StreamDiagnosticsSnapshot,
};
pub use execution::StreamingExecution;
pub use timeout::StreamTimeoutPolicy;
pub use types::{StreamClientHeaders, StreamPhase, StreamRequest, StreamingCoordinatorError};

pub(crate) use execution::{
    ActiveStream, AttemptStreamFacts, PendingStreamFinalization, PendingStreamFinalizationParts,
};
pub(crate) use terminal::{
    bounded_i64, bounded_request_id, bounded_usize, cache_status, category_label, duration_i64,
    is_event_stream, protocol_for_surface, provider_error_signal, store_eof, store_idle_timeout,
    store_midstream_transport, store_translation_error,
};
pub(crate) use types::LastUpstream;
