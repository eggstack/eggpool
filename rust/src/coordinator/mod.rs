//! M7 coordinator boundaries.
//!
//! C002 owns only the durable publication boundary after M5 has selected an
//! account. Provider dispatch, wire negotiation, retries, and terminal
//! finalization are deliberately left to later coordinator slices.

mod publication;

pub use publication::{
    FinalizationIdentity, PostCommitInterruption, PublicationError, PublicationFaultInjector,
    PublicationInput, PublicationOutcome, PublicationService, PublicationStage, PublishedAttempt,
    RuntimePublicationReceipt,
};
