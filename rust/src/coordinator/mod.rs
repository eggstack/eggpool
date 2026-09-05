//! M7 coordinator boundaries.
//!
//! C002 owns only the durable publication boundary after M5 has selected an
//! account. Provider dispatch, wire negotiation, retries, and terminal
//! finalization are deliberately left to later coordinator slices.

mod attempt;
mod failure;
mod finalization;
mod publication;
mod wire_resolver;

pub use attempt::{
    AttemptBuilder, AttemptError, AttemptInput, PreparedUpstreamAttempt, UpstreamResponseEvidence,
};
pub use failure::{
    EffectLedger, FailureCategory, FailureDecisionEngine, FailureEffects, FailureObservation,
    FailureSource, NextAction, RetryPolicy, RetryScope, classify, parse_retry_after,
};
pub use finalization::{
    DurableFinalizer, FinalizationCommand, FinalizationData, FinalizationError, FinalizationHandle,
    FinalizationOutcome, FinalizationResult, FinalizationSupervisor, SupervisorSnapshot,
};

pub use publication::{
    FinalizationIdentity, PostCommitInterruption, PublicationError, PublicationFaultInjector,
    PublicationInput, PublicationOutcome, PublicationService, PublicationStage, PublishedAttempt,
    RuntimePublicationReceipt,
};

pub use wire_resolver::{
    NegotiationLease, NegotiationResult, NegotiationRole, WireCandidate, WireResolution,
    WireResolver, WireResolverConfig,
};
