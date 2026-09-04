//! Routing health, bounded backoff, circuit ownership, and model quarantine.
//!
//! This module is deliberately request-independent. It records and exposes
//! state transitions, but it does not retry requests, choose destinations, or
//! finalize durable request/attempt rows.

mod backoff;
mod circuit_breaker;
mod effects;
mod health_manager;
mod quarantine;
mod repository;

pub use backoff::{
    BackoffPolicy, BackoffReason, FailureCategory, JitterSource, MAX_NONTERMINAL_BACKOFF_SECONDS,
    SeededJitter, classify_failure_category, compute_backoff_seconds,
    compute_backoff_seconds_with_rng, get_backoff_policy,
};
pub use circuit_breaker::{CircuitBreaker, CircuitState, CircuitStats, MonotonicClock};
pub use effects::{HealthEffect, HealthEffectApplier, HealthEffectOutcome};
pub use health_manager::{AccountHealth, AccountHealthSnapshot, HealthManager, HealthManagerError};
pub use quarantine::{
    EvidenceProvenance, ModelQuarantine, QuarantineEntry, QuarantineKey, QuarantineState,
    entry_from_row,
};
pub use repository::{
    AccountBackoffRecord, AccountBackoffRepository, AccountBackoffRepositoryError,
    ModelQuarantineRecord, ModelQuarantineRepository, ModelQuarantineRepositoryError,
};
