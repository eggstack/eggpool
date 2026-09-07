//! M7 coordinator boundaries.
//!
//! C002 owns only the durable publication boundary after M5 has selected an
//! account. Provider dispatch, wire negotiation, retries, and terminal
//! finalization are deliberately left to later coordinator slices.

mod attempt;
mod failure;
mod finalization;
mod finite;
mod publication;
mod streaming;
mod wire_resolver;

pub use attempt::{
    AttemptBuilder, AttemptError, AttemptInput, PreparedUpstreamAttempt, UpstreamResponseEvidence,
};
pub use failure::{
    EffectLedger, EffectLedgerError, FailureCategory, FailureDecisionEngine, FailureEffects,
    FailureObservation, FailureSource, NextAction, ProviderModelPresence, RetryPolicy, RetryScope,
    classify, parse_retry_after,
};
pub use finalization::{
    DurableFinalizer, FinalizationCommand, FinalizationData, FinalizationError, FinalizationHandle,
    FinalizationOutcome, FinalizationProgress, FinalizationResult, FinalizationSupervisor,
    SupervisorSnapshot,
};
pub use finite::{
    ClientResponseHeaders, DownstreamResult, FiniteClientResponse, FiniteCoordinator,
    FiniteCoordinatorError, FiniteExecution, FiniteRequest, ResponseHandoffState,
    filter_response_headers,
};

pub use publication::{
    FinalizationIdentity, PostCommitInterruption, PublicationError, PublicationFaultInjector,
    PublicationInput, PublicationOutcome, PublicationService, PublicationStage, PublishedAttempt,
    RuntimePublicationReceipt,
};

pub use streaming::{
    OUTCOME_CLIENT_CANCELLED, OUTCOME_COMPLETED_CANONICAL, OUTCOME_COMPLETED_COMPATIBILITY,
    OUTCOME_EMPTY_EOF, OUTCOME_FIRST_BYTE_TIMEOUT, OUTCOME_IDLE_TIMEOUT, OUTCOME_MALFORMED_EOF,
    OUTCOME_PREMATURE_EOF_BEFORE_BODY, OUTCOME_PREMATURE_EOF_MIDSTREAM,
    OUTCOME_RESPONSE_HEADER_TIMEOUT, OUTCOME_TERMINAL_FAILURE, OUTCOME_TERMINAL_INCOMPLETE,
    OUTCOME_UPSTREAM_MIDSTREAM_ERROR, StreamChunkError, StreamClientHeaders, StreamDiagnosticEvent,
    StreamDiagnostics, StreamDiagnosticsSnapshot, StreamPhase, StreamRequest, StreamTimeoutPolicy,
    StreamingCoordinator, StreamingCoordinatorError, StreamingExecution,
};

pub use wire_resolver::{
    NegotiationLease, NegotiationResult, NegotiationRole, WireCandidate, WireResolution,
    WireResolver, WireResolverConfig, WireResolverSnapshot,
};
