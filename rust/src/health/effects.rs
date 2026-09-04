//! Deterministic health-effect application boundary.
//!
//! Retryability, destination selection, response mapping, and request
//! finalization intentionally do not appear here.

use super::{
    AccountBackoffRecord, AccountBackoffRepository, BackoffReason, EvidenceProvenance,
    HealthManager, ModelQuarantine, ModelQuarantineRepository,
};

#[derive(Debug, Clone)]
pub struct HealthEffect {
    pub account_id: i64,
    pub account_name: String,
    pub provider_id: String,
    pub category: BackoffReason,
    pub model_id: Option<String>,
    pub upstream_model_id: Option<String>,
    pub upstream_protocol: String,
    pub status_code: Option<u16>,
    pub error_class: Option<String>,
    pub retry_after_seconds: Option<f64>,
    pub provenance: EvidenceProvenance,
    pub authoritative: bool,
    pub wall_now: f64,
}

impl HealthEffect {
    pub fn account(
        account_id: i64,
        account_name: impl Into<String>,
        provider_id: impl Into<String>,
        category: BackoffReason,
        wall_now: f64,
    ) -> Self {
        Self {
            account_id,
            account_name: account_name.into(),
            provider_id: provider_id.into(),
            category,
            model_id: None,
            upstream_model_id: None,
            upstream_protocol: "openai".to_owned(),
            status_code: None,
            error_class: None,
            retry_after_seconds: None,
            provenance: EvidenceProvenance::RuntimeHttp,
            authoritative: false,
            wall_now,
        }
    }

    pub fn model(
        mut self,
        canonical_model_id: impl Into<String>,
        upstream_model_id: Option<String>,
        protocol: impl Into<String>,
    ) -> Self {
        self.model_id = Some(canonical_model_id.into());
        self.upstream_model_id = upstream_model_id;
        self.upstream_protocol = protocol.into();
        self
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct HealthEffectOutcome {
    pub category: BackoffReason,
    pub account_changed: bool,
    pub model_changed: bool,
    pub circuit_penalized: bool,
    pub probe_released: bool,
    pub backoff_seconds: Option<f64>,
    pub terminal_withdrawal: bool,
}

pub struct HealthEffectApplier<'a> {
    pub health: &'a HealthManager,
    pub quarantine: &'a ModelQuarantine,
    pub account_backoffs: Option<&'a AccountBackoffRepository>,
    pub quarantine_repository: Option<&'a ModelQuarantineRepository>,
}

impl<'a> HealthEffectApplier<'a> {
    pub async fn apply(&self, effect: &HealthEffect) -> Result<HealthEffectOutcome, String> {
        let snapshot = self.health.snapshot(&effect.account_name);
        let next_failure = snapshot
            .as_ref()
            .map_or(1, |state| state.consecutive_failures + 1);
        let next_cooldown = snapshot
            .as_ref()
            .map_or(1, |state| state.consecutive_cooldowns + 1);
        let model_scoped =
            effect.category == BackoffReason::ModelUnavailable && effect.model_id.is_some();
        let mut outcome = HealthEffectOutcome {
            category: effect.category,
            account_changed: false,
            model_changed: false,
            circuit_penalized: false,
            probe_released: false,
            backoff_seconds: None,
            terminal_withdrawal: false,
        };

        match effect.category {
            BackoffReason::AuthenticationFailed => {
                self.health
                    .record_failure(&effect.account_name, BackoffReason::AuthenticationFailed);
                outcome.account_changed = true;
                outcome.circuit_penalized = true;
            }
            BackoffReason::QuotaExhausted | BackoffReason::RateLimited => {
                let delay = super::compute_backoff_seconds(
                    effect.category,
                    next_cooldown,
                    effect.retry_after_seconds,
                    false,
                );
                if let Some(delay) = delay {
                    self.health
                        .record_cooldown(&effect.account_name, effect.category, delay);
                    outcome.backoff_seconds = Some(delay);
                    outcome.account_changed = true;
                }
                self.health.release_request(&effect.account_name);
                outcome.probe_released = true;
            }
            BackoffReason::ModelUnavailable if model_scoped => {
                let model_id = effect.model_id.as_deref().expect("model scope checked");
                let key = self.quarantine.key(
                    &effect.provider_id,
                    &effect.account_name,
                    model_id,
                    effect.upstream_model_id.as_deref(),
                    &effect.upstream_protocol,
                );
                let entry = if effect.authoritative && effect.provenance.is_authoritative() {
                    self.quarantine
                        .set_terminal_withdrawn(
                            key,
                            "authoritative_model_withdrawal",
                            effect.provenance,
                            effect.wall_now,
                        )
                        .map_err(str::to_owned)?
                } else {
                    self.quarantine.record_observation(
                        key,
                        effect.provenance,
                        "model_unavailable",
                        effect.status_code,
                        effect.error_class.clone(),
                        effect.wall_now,
                    )
                };
                let delay = if entry.expiry.is_some() {
                    super::compute_backoff_seconds(effect.category, next_failure, None, false)
                } else {
                    None
                };
                self.health.disable_model(
                    &effect.account_name,
                    model_id,
                    delay,
                    entry.expiry.is_none(),
                );
                outcome.model_changed = true;
                outcome.backoff_seconds = delay;
                outcome.terminal_withdrawal = entry.expiry.is_none();
                self.health.release_request(&effect.account_name);
                outcome.probe_released = true;
                if let Some(repository) = self.quarantine_repository {
                    repository
                        .upsert_entry(&entry)
                        .await
                        .map_err(|error| error.to_string())?;
                }
            }
            BackoffReason::ContextLimitExceeded | BackoffReason::Unknown => {
                self.health.release_request(&effect.account_name);
                outcome.probe_released = true;
            }
            reason => {
                let delay = super::compute_backoff_seconds(reason, next_failure, None, false);
                self.health.record_failure(&effect.account_name, reason);
                outcome.account_changed = true;
                outcome.circuit_penalized = true;
                if let Some(delay) = delay {
                    self.health
                        .record_cooldown(&effect.account_name, reason, delay);
                    outcome.backoff_seconds = Some(delay);
                }
            }
        }

        if let Some(repository) = self.account_backoffs {
            if let Some(delay) = outcome.backoff_seconds {
                repository
                    .upsert(&AccountBackoffRecord {
                        id: 0,
                        account_id: effect.account_id,
                        model_id: model_scoped.then(|| effect.model_id.clone()).flatten(),
                        reason: effect.category,
                        status_code: effect.status_code,
                        error_class: effect.error_class.clone(),
                        consecutive_failures: next_failure.max(next_cooldown),
                        backoff_until_epoch: Some(effect.wall_now + delay),
                        last_failure_epoch: effect.wall_now,
                        updated_epoch: effect.wall_now,
                    })
                    .await
                    .map_err(|error| error.to_string())?;
            } else if effect.category == BackoffReason::AuthenticationFailed {
                repository
                    .upsert(&AccountBackoffRecord {
                        id: 0,
                        account_id: effect.account_id,
                        model_id: None,
                        reason: effect.category,
                        status_code: effect.status_code,
                        error_class: effect.error_class.clone(),
                        consecutive_failures: next_failure,
                        backoff_until_epoch: None,
                        last_failure_epoch: effect.wall_now,
                        updated_epoch: effect.wall_now,
                    })
                    .await
                    .map_err(|error| error.to_string())?;
            }
        }
        Ok(outcome)
    }
}
