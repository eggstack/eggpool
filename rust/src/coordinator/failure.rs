//! Centralized, bounded failure classification and retry legality.

use std::time::Duration;

use http::StatusCode;
use serde::{Deserialize, Serialize};

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
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FailureEffects {
    pub category: FailureCategory,
    pub retry_scope: RetryScope,
    pub action: NextAction,
    pub apply_account_penalty: bool,
    pub quarantine_model: bool,
    pub persist_backoff: bool,
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

#[derive(Debug, Default)]
pub struct EffectLedger {
    applied: std::collections::BTreeSet<i64>,
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
}

impl EffectLedger {
    pub fn apply_once(&mut self, attempt_id: i64) -> bool {
        self.applied.insert(attempt_id)
    }

    pub fn len(&self) -> usize {
        self.applied.len()
    }

    pub fn is_empty(&self) -> bool {
        self.applied.is_empty()
    }
}

pub fn classify(observation: &FailureObservation, policy: RetryPolicy) -> FailureEffects {
    let mut category = observation.category_hint.unwrap_or({
        match (observation.source, observation.status) {
            (FailureSource::Cancellation, _) => FailureCategory::Cancelled,
            (FailureSource::Client, _) => FailureCategory::BadRequest,
            (FailureSource::Transport, _) => FailureCategory::TransientTransport,
            (_, Some(401 | 403)) => FailureCategory::Authentication,
            (_, Some(408 | 425 | 429)) => FailureCategory::RateLimit,
            (_, Some(400..=499)) => FailureCategory::BadRequest,
            (_, Some(500..=599)) => FailureCategory::Temporary,
            _ => FailureCategory::Fatal,
        }
    });
    if observation.wire_rejection && !observation.response_started {
        category = FailureCategory::WireRejected;
    }
    let retryable =
        !observation.response_started && observation.attempt_number < policy.max_attempts;
    let (retry_scope, action) = match category {
        FailureCategory::WireRejected if retryable => (RetryScope::Wire, NextAction::RetryWire),
        FailureCategory::Authentication if retryable => {
            (RetryScope::Account, NextAction::RetryAccount)
        }
        FailureCategory::RateLimit
            if !observation.response_started && observation.retry_after.is_some() =>
        {
            (RetryScope::Wait, NextAction::WaitRateLimit)
        }
        FailureCategory::Temporary | FailureCategory::TransientTransport if retryable => {
            (RetryScope::Account, NextAction::RetryAccount)
        }
        FailureCategory::Cancelled => (RetryScope::None, NextAction::Complete),
        _ if retryable && matches!(category, FailureCategory::ModelUnavailable) => {
            (RetryScope::Account, NextAction::RetryAccount)
        }
        _ => (
            RetryScope::None,
            if retryable {
                NextAction::Exhaust
            } else {
                NextAction::Complete
            },
        ),
    };
    FailureEffects {
        category,
        retry_scope,
        action,
        apply_account_penalty: matches!(
            category,
            FailureCategory::Authentication | FailureCategory::TransientTransport
        ),
        quarantine_model: matches!(category, FailureCategory::ModelUnavailable),
        persist_backoff: matches!(
            category,
            FailureCategory::RateLimit
                | FailureCategory::Temporary
                | FailureCategory::TransientTransport
        ),
    }
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
