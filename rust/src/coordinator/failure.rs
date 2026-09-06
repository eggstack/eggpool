//! Centralized, bounded failure classification and retry legality.

use std::{
    collections::{BTreeMap, VecDeque},
    time::Duration,
};

use http::StatusCode;
use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum FailureSource {
    Transport,
    ProviderResponse,
    Client,
    Cancellation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum FailureCategory {
    BadRequest,
    Authentication,
    Quota,
    RateLimit,
    Temporary,
    TransientTransport,
    ModelUnavailable,
    WireRejected,
    Cancelled,
    Fatal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RetryScope {
    None,
    Account,
    Wire,
    Wait,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum NextAction {
    Complete,
    RetryAccount,
    RetryWire,
    WaitRateLimit,
    Exhaust,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureObservation {
    pub attempt_id: i64,
    pub attempt_number: u32,
    pub source: FailureSource,
    pub status: Option<u16>,
    pub category_hint: Option<FailureCategory>,
    pub response_started: bool,
    pub stream_terminal: bool,
    pub wire_rejection: bool,
    pub retry_after: Option<Duration>,
    pub signal: Option<String>,
    pub provider_id: Option<String>,
    pub account_name: Option<String>,
    pub model_id: Option<String>,
    pub upstream_model_id: Option<String>,
    pub client_protocol: String,
    pub upstream_protocol: String,
    pub wire_surface: Option<String>,
    pub candidate_fingerprint: Option<String>,
    pub transport_phase: Option<String>,
    pub error_class: Option<String>,
    pub downstream_started: bool,
    pub alternate_wire_available: bool,
    pub credential_configured: bool,
    pub provider_model_presence: ProviderModelPresence,
    pub dispatch_phase: String,
}

impl FailureObservation {
    pub fn response(attempt_id: i64, attempt_number: u32, status: StatusCode) -> Self {
        Self {
            attempt_id,
            attempt_number,
            source: FailureSource::ProviderResponse,
            status: Some(status.as_u16()),
            category_hint: None,
            response_started: false,
            stream_terminal: false,
            wire_rejection: false,
            retry_after: None,
            signal: None,
            provider_id: None,
            account_name: None,
            model_id: None,
            upstream_model_id: None,
            client_protocol: String::new(),
            upstream_protocol: String::new(),
            wire_surface: None,
            candidate_fingerprint: None,
            transport_phase: None,
            error_class: None,
            downstream_started: false,
            alternate_wire_available: false,
            credential_configured: false,
            provider_model_presence: ProviderModelPresence::Unknown,
            dispatch_phase: "response_status".into(),
        }
    }

    pub fn signal(mut self, signal: impl Into<String>) -> Self {
        self.signal = Some(normalize_signal(&signal.into()));
        self
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProviderModelPresence {
    Known,
    Unknown,
    AbsentAuthoritative,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureEffects {
    pub category: FailureCategory,
    pub retry_scope: RetryScope,
    pub action: NextAction,
    pub apply_account_penalty: bool,
    pub quarantine_model: bool,
    pub persist_backoff: bool,
    pub retry: bool,
    pub client_outcome: String,
    pub account_effect: String,
    pub model_effect: String,
    pub circuit_effect: String,
    pub wire_effect: String,
    pub backoff_reason: Option<String>,
    pub retry_after: Option<Duration>,
    pub provider_attributable: bool,
    pub downstream_started: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetryPolicy {
    pub max_attempts: u32,
    pub max_retry_after: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 3,
            max_retry_after: Duration::from_secs(1_800),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum EffectLedgerError {
    #[error("attempt effect registry is at capacity")]
    Capacity,
}

#[derive(Debug)]
pub struct EffectLedger {
    applied: BTreeMap<i64, ()>,
    order: VecDeque<i64>,
    capacity: usize,
}

impl Default for EffectLedger {
    fn default() -> Self {
        Self::with_capacity(256)
    }
}

impl EffectLedger {
    pub fn with_capacity(capacity: usize) -> Self {
        Self {
            applied: BTreeMap::new(),
            order: VecDeque::new(),
            capacity: capacity.max(1),
        }
    }

    pub fn try_apply_once(&mut self, attempt_id: i64) -> Result<bool, EffectLedgerError> {
        if self.applied.contains_key(&attempt_id) {
            return Ok(false);
        }
        if self.applied.len() >= self.capacity {
            return Err(EffectLedgerError::Capacity);
        }
        self.applied.insert(attempt_id, ());
        self.order.push_back(attempt_id);
        Ok(true)
    }

    pub fn retire(&mut self, attempt_id: i64) -> bool {
        let removed = self.applied.remove(&attempt_id).is_some();
        if removed {
            self.order.retain(|id| *id != attempt_id);
        }
        removed
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }
}

#[derive(Debug)]
pub struct FailureDecisionEngine {
    pub policy: RetryPolicy,
    pub ledger: EffectLedger,
}

impl FailureDecisionEngine {
    pub fn new(policy: RetryPolicy) -> Self {
        Self {
            policy,
            ledger: EffectLedger::default(),
        }
    }

    /// Classify once and return whether the caller owns the first effect
    /// application for this attempt.  Retried finalization observes the same
    /// decision without applying account/model effects twice.
    pub fn decide(&mut self, observation: &FailureObservation) -> (FailureEffects, bool) {
        let effects = classify(observation, self.policy);
        let first_application = self.ledger.apply_once(observation.attempt_id);
        (effects, first_application)
    }

    pub fn try_decide(
        &mut self,
        observation: &FailureObservation,
    ) -> Result<(FailureEffects, bool), EffectLedgerError> {
        let effects = classify(observation, self.policy);
        Ok((effects, self.ledger.try_apply_once(observation.attempt_id)?))
    }
}

impl EffectLedger {
    pub fn apply_once(&mut self, attempt_id: i64) -> bool {
        self.try_apply_once(attempt_id).unwrap_or(false)
    }

    pub fn len(&self) -> usize {
        self.applied.len()
    }

    pub fn is_empty(&self) -> bool {
        self.applied.is_empty()
    }
}

pub fn classify(observation: &FailureObservation, policy: RetryPolicy) -> FailureEffects {
    let signal = observation
        .signal
        .as_deref()
        .map(normalize_signal)
        .unwrap_or_default();
    let local = matches!(
        observation.source,
        FailureSource::Client | FailureSource::Cancellation
    ) || matches!(observation.category_hint, Some(FailureCategory::BadRequest))
        && observation.source == FailureSource::Client;
    let retryable = !observation.response_started
        && !observation.downstream_started
        && observation.attempt_number < policy.max_attempts;
    let mut category = observation
        .category_hint
        .unwrap_or(match observation.source {
            FailureSource::Cancellation => FailureCategory::Cancelled,
            FailureSource::Client => FailureCategory::BadRequest,
            FailureSource::Transport => FailureCategory::TransientTransport,
            FailureSource::ProviderResponse => match observation.status {
                Some(401 | 403) => FailureCategory::Authentication,
                Some(408 | 425 | 429) => FailureCategory::RateLimit,
                Some(400..=499) => FailureCategory::BadRequest,
                Some(500..=599) => FailureCategory::Temporary,
                _ => FailureCategory::Fatal,
            },
        });
    if observation.wire_rejection
        || (observation.alternate_wire_available
            && observation.dispatch_phase == "response_status"
            && matches!(
                signal.as_str(),
                "wire_auth_mismatch"
                    | "wire_surface_unsupported"
                    | "wire_schema_mismatch"
                    | "model_unsupported_on_surface"
            ))
    {
        category = FailureCategory::WireRejected;
    }
    let mut retry_scope = RetryScope::None;
    let mut action = NextAction::Complete;
    let mut account_effect = "none";
    let mut model_effect = "none";
    let mut circuit_effect = "none";
    let mut wire_effect = "none";
    let mut client_outcome = if observation
        .status
        .is_some_and(|status| (400..500).contains(&status))
    {
        "client_error"
    } else {
        "upstream_error"
    };
    let mut persist_backoff = false;
    let mut backoff_reason = None;
    let mut provider_attributable = false;

    if local {
        category = if observation.source == FailureSource::Cancellation {
            FailureCategory::Cancelled
        } else {
            FailureCategory::BadRequest
        };
        client_outcome = if observation.source == FailureSource::Cancellation {
            "upstream_error"
        } else {
            "client_error"
        };
    } else if category == FailureCategory::WireRejected && !observation.response_started {
        wire_effect = "reject_candidate";
        if retryable {
            retry_scope = RetryScope::Wire;
            action = NextAction::RetryWire;
        }
    } else if observation.source == FailureSource::Transport {
        account_effect = "failure";
        circuit_effect = "failure";
        persist_backoff = true;
        backoff_reason = Some("connection_failure");
        provider_attributable = true;
        if retryable {
            retry_scope = RetryScope::Account;
            action = NextAction::RetryAccount;
        }
        client_outcome = "service_unavailable";
    } else {
        match (observation.status, signal.as_str()) {
            (_, "credential_invalid") => {
                category = FailureCategory::Authentication;
                account_effect = "disable_auth";
                circuit_effect = "failure";
                persist_backoff = true;
                backoff_reason = Some("authentication_failed");
                provider_attributable = true;
                if retryable {
                    retry_scope = RetryScope::Account;
                    action = NextAction::RetryAccount;
                }
            }
            (
                _,
                "wire_auth_mismatch"
                | "wire_surface_unsupported"
                | "wire_schema_mismatch"
                | "model_unsupported_on_surface",
            ) if observation.alternate_wire_available
                && observation.dispatch_phase == "response_status" =>
            {
                category = FailureCategory::WireRejected;
                wire_effect = "reject_candidate";
                if retryable {
                    retry_scope = RetryScope::Wire;
                    action = NextAction::RetryWire;
                }
            }
            (Some(400), _) | (Some(409 | 422), _) => {
                category = FailureCategory::BadRequest;
            }
            (Some(401), "model_absent") => {
                category = FailureCategory::ModelUnavailable;
                model_effect = "quarantine";
                persist_backoff = true;
                backoff_reason = Some("model_unavailable");
                provider_attributable = true;
                if retryable {
                    retry_scope = RetryScope::Account;
                    action = NextAction::RetryAccount;
                }
            }
            (Some(401), _) => {
                category = FailureCategory::Authentication;
            }
            (Some(402), _) | (Some(403), "quota_exhausted") => {
                category = FailureCategory::Quota;
                account_effect = "quota";
                persist_backoff = true;
                backoff_reason = Some("quota_exhausted");
                provider_attributable = true;
                if retryable {
                    retry_scope = RetryScope::Account;
                    action = NextAction::RetryAccount;
                }
            }
            (Some(403), _) => {
                category = FailureCategory::BadRequest;
            }
            (Some(404), "model_absent") => {
                category = FailureCategory::ModelUnavailable;
                model_effect = if observation.provider_model_presence
                    == ProviderModelPresence::AbsentAuthoritative
                {
                    "terminal_withdrawal"
                } else {
                    "quarantine"
                };
                persist_backoff = true;
                backoff_reason = Some("model_unavailable");
                provider_attributable = true;
                if retryable {
                    retry_scope = RetryScope::Account;
                    action = NextAction::RetryAccount;
                }
            }
            (Some(404), _)
                if observation.alternate_wire_available
                    && observation.dispatch_phase == "response_status" =>
            {
                category = FailureCategory::WireRejected;
                wire_effect = "reject_candidate";
                if retryable {
                    retry_scope = RetryScope::Wire;
                    action = NextAction::RetryWire;
                }
            }
            (Some(408), _) => {
                category = FailureCategory::Temporary;
                account_effect = "failure";
                model_effect = "quarantine";
                circuit_effect = "failure";
                persist_backoff = true;
                backoff_reason = Some("connect_timeout");
                provider_attributable = true;
                if retryable {
                    retry_scope = RetryScope::Account;
                    action = NextAction::RetryAccount;
                }
                client_outcome = "timeout";
            }
            (Some(429), _) => {
                category = FailureCategory::RateLimit;
                account_effect = "rate_limit";
                persist_backoff = true;
                backoff_reason = Some("rate_limited");
                provider_attributable = true;
                if retryable {
                    retry_scope = RetryScope::Account;
                    action = NextAction::RetryAccount;
                }
            }
            (Some(500..=599), _) => {
                category = FailureCategory::Temporary;
                account_effect = "failure";
                model_effect = "quarantine";
                circuit_effect = "failure";
                persist_backoff = true;
                backoff_reason = Some("upstream_server_error");
                provider_attributable = true;
                if retryable {
                    retry_scope = RetryScope::Account;
                    action = NextAction::RetryAccount;
                }
            }
            _ => {}
        }
    }
    if observation.response_started || observation.downstream_started {
        retry_scope = RetryScope::None;
        action = NextAction::Complete;
    } else if action == NextAction::Complete
        && retryable
        && !matches!(
            category,
            FailureCategory::BadRequest
                | FailureCategory::Authentication
                | FailureCategory::Cancelled
                | FailureCategory::Quota
                | FailureCategory::Fatal
        )
    {
        action = NextAction::Exhaust;
    }
    FailureEffects {
        category,
        retry_scope,
        action,
        apply_account_penalty: account_effect != "none",
        quarantine_model: model_effect == "quarantine",
        persist_backoff,
        retry: matches!(
            action,
            NextAction::RetryAccount | NextAction::RetryWire | NextAction::WaitRateLimit
        ),
        client_outcome: client_outcome.into(),
        account_effect: account_effect.into(),
        model_effect: model_effect.into(),
        circuit_effect: circuit_effect.into(),
        wire_effect: wire_effect.into(),
        backoff_reason: backoff_reason.map(str::to_owned),
        retry_after: observation.retry_after,
        provider_attributable,
        downstream_started: observation.downstream_started || observation.response_started,
    }
}

fn normalize_signal(value: &str) -> String {
    value
        .chars()
        .filter(|character| {
            character.is_ascii_alphanumeric() || *character == '_' || *character == '-'
        })
        .take(80)
        .collect::<String>()
        .to_ascii_lowercase()
}

pub fn parse_retry_after(
    value: &str,
    now_epoch_seconds: i64,
    policy: RetryPolicy,
) -> Option<Duration> {
    let seconds = value
        .trim()
        .parse::<i64>()
        .ok()
        .or_else(|| parse_rfc1123(value).map(|epoch| epoch - now_epoch_seconds))?;
    if seconds < 0 {
        return None;
    }
    Some(Duration::from_secs(seconds as u64).min(policy.max_retry_after))
}

fn parse_rfc1123(value: &str) -> Option<i64> {
    let mut fields = value.split_whitespace();
    let _weekday = fields.next()?;
    let day = fields.next()?.parse::<u32>().ok()?;
    let month = match fields.next()? {
        "Jan" => 1,
        "Feb" => 2,
        "Mar" => 3,
        "Apr" => 4,
        "May" => 5,
        "Jun" => 6,
        "Jul" => 7,
        "Aug" => 8,
        "Sep" => 9,
        "Oct" => 10,
        "Nov" => 11,
        "Dec" => 12,
        _ => return None,
    };
    let year = fields.next()?.parse::<i64>().ok()?;
    let time = fields.next()?;
    if fields.next()? != "GMT" || day == 0 || day > 31 {
        return None;
    }
    let mut clock = time.split(':');
    let hour = clock.next()?.parse::<i64>().ok()?;
    let minute = clock.next()?.parse::<i64>().ok()?;
    let second = clock.next()?.parse::<i64>().ok()?;
    if hour > 23 || minute > 59 || second > 60 {
        return None;
    }
    let month_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let leap_days = |y: i64| y / 4 - y / 100 + y / 400;
    let years = year.checked_sub(1970)?;
    let mut days = years * 365 + leap_days(year - 1) - leap_days(1969);
    for index in 1..month {
        days += i64::from(month_days[(index - 1) as usize]);
        if index == 2 && (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) {
            days += 1;
        }
    }
    Some(days * 86_400 + (i64::from(day) - 1) * 86_400 + hour * 3_600 + minute * 60 + second)
}
