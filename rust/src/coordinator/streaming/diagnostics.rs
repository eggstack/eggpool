//! Bounded, secret-free streaming outcome diagnostics.

// ---------------------------------------------------------------------------
// Chunk errors and diagnostics
// ---------------------------------------------------------------------------

use std::collections::BTreeMap;

use thiserror::Error;

/// Terminal failure surfaced by [`StreamingExecution::next_chunk`].
///
/// Every variant is terminal for this client request: the provider body is
/// released, C006 ownership is stored, and the caller must not retry
/// upstream. Variants carry no bodies, secrets, or provider prose.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum StreamChunkError {
    #[error("provider stream exceeded its idle timeout")]
    IdleTimeout,
    #[error("provider stream ended before any body bytes")]
    EmptyEof,
    #[error("provider stream ended without terminal evidence")]
    PrematureEof,
    #[error("provider stream carried malformed SSE or undecodable events")]
    MalformedEof,
    #[error("provider stream transport failed midstream")]
    UpstreamTransport,
    #[error("provider stream event could not be encoded for the client")]
    Translation,
}

pub const OUTCOME_RESPONSE_HEADER_TIMEOUT: &str = "response_header_timeout";
pub const OUTCOME_FIRST_BYTE_TIMEOUT: &str = "first_byte_timeout";
pub const OUTCOME_IDLE_TIMEOUT: &str = "stream_idle_timeout";
pub const OUTCOME_COMPLETED_CANONICAL: &str = "stream_completed_canonical";
pub const OUTCOME_COMPLETED_COMPATIBILITY: &str = "stream_completed_compatibility";
pub const OUTCOME_EMPTY_EOF: &str = "empty_eof";
pub const OUTCOME_PREMATURE_EOF_BEFORE_BODY: &str = "premature_eof_before_body";
pub const OUTCOME_PREMATURE_EOF_MIDSTREAM: &str = "premature_eof_midstream";
pub const OUTCOME_MALFORMED_EOF: &str = "malformed_eof";
pub const OUTCOME_TERMINAL_FAILURE: &str = "stream_responses_terminal_failure";
pub const OUTCOME_TERMINAL_INCOMPLETE: &str = "stream_responses_terminal_incomplete";
pub const OUTCOME_UPSTREAM_MIDSTREAM_ERROR: &str = "upstream_midstream_error";
pub const OUTCOME_CLIENT_CANCELLED: &str = "client_cancelled";

pub(crate) const KNOWN_OUTCOMES: &[&str] = &[
    OUTCOME_RESPONSE_HEADER_TIMEOUT,
    OUTCOME_FIRST_BYTE_TIMEOUT,
    OUTCOME_IDLE_TIMEOUT,
    OUTCOME_COMPLETED_CANONICAL,
    OUTCOME_COMPLETED_COMPATIBILITY,
    OUTCOME_EMPTY_EOF,
    OUTCOME_PREMATURE_EOF_BEFORE_BODY,
    OUTCOME_PREMATURE_EOF_MIDSTREAM,
    OUTCOME_MALFORMED_EOF,
    OUTCOME_TERMINAL_FAILURE,
    OUTCOME_TERMINAL_INCOMPLETE,
    OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
    OUTCOME_CLIENT_CANCELLED,
];

/// Scalar diagnostic event for a terminal streaming path.
///
/// Best-effort and secret-free: counts, durations, and labels only. No
/// request bodies, API keys, prompts, upstream chunks, or session identities.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamDiagnosticEvent {
    pub outcome: &'static str,
    pub attempt: u32,
    pub bytes_emitted: usize,
    pub elapsed_ms: i64,
}

/// Bounded process-local streaming outcome counters owned by one coordinator.
#[derive(Debug, Default)]
pub struct StreamDiagnostics {
    pub(crate) counts: BTreeMap<&'static str, u64>,
    pub(crate) last: Option<StreamDiagnosticEvent>,
}

impl StreamDiagnostics {
    pub(crate) fn new() -> Self {
        let mut counts = BTreeMap::new();
        for outcome in KNOWN_OUTCOMES {
            counts.insert(*outcome, 0);
        }
        Self { counts, last: None }
    }

    pub(crate) fn record(
        &mut self,
        outcome: &'static str,
        attempt: u32,
        bytes_emitted: usize,
        elapsed_ms: i64,
    ) {
        *counts_entry(&mut self.counts, outcome) += 1;
        self.last = Some(StreamDiagnosticEvent {
            outcome,
            attempt,
            bytes_emitted,
            elapsed_ms,
        });
    }

    pub(crate) fn count(&self, outcome: &str) -> u64 {
        self.counts.get(outcome).copied().unwrap_or(0)
    }
}

fn counts_entry<'a>(
    counts: &'a mut BTreeMap<&'static str, u64>,
    outcome: &'static str,
) -> &'a mut u64 {
    if !KNOWN_OUTCOMES.contains(&outcome) {
        return counts.entry("unknown").or_insert(0);
    }
    counts.entry(outcome).or_insert(0)
}

/// Snapshot of streaming outcome counters for tests and operators.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamDiagnosticsSnapshot {
    pub outcomes: BTreeMap<String, u64>,
    pub last: Option<StreamDiagnosticEvent>,
}
