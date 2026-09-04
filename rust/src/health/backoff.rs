//! Normalized failure categories and bounded reason-specific backoff.

use std::fmt;

use serde::{Deserialize, Serialize};

/// No non-terminal suppression may last longer than this duration.
pub const MAX_NONTERMINAL_BACKOFF_SECONDS: f64 = 1_800.0;

const AUTH_FAILURE_CLASSES: [&str; 7] = [
    "auth",
    "auth_error",
    "auth_failed",
    "authentication",
    "authentication_error",
    "authentication_failed",
    "authenticationerror",
];

/// Stable failure vocabulary shared by health and later coordinator policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackoffReason {
    AuthenticationFailed,
    QuotaExhausted,
    RateLimited,
    ModelUnavailable,
    ConnectTimeout,
    ConnectionFailure,
    UpstreamServerError,
    ProtocolError,
    ContextLimitExceeded,
    Unknown,
}

impl BackoffReason {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::AuthenticationFailed => "authentication_failed",
            Self::QuotaExhausted => "quota_exhausted",
            Self::RateLimited => "rate_limited",
            Self::ModelUnavailable => "model_unavailable",
            Self::ConnectTimeout => "connect_timeout",
            Self::ConnectionFailure => "connection_failure",
            Self::UpstreamServerError => "upstream_server_error",
            Self::ProtocolError => "protocol_error",
            Self::ContextLimitExceeded => "context_limit_exceeded",
            Self::Unknown => "unknown",
        }
    }
}

impl fmt::Display for BackoffReason {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl TryFrom<&str> for BackoffReason {
    type Error = ();

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Ok(match value {
            "authentication_failed" => Self::AuthenticationFailed,
            "quota_exhausted" => Self::QuotaExhausted,
            "rate_limited" => Self::RateLimited,
            "model_unavailable" => Self::ModelUnavailable,
            "connect_timeout" => Self::ConnectTimeout,
            "connection_failure" => Self::ConnectionFailure,
            "upstream_server_error" => Self::UpstreamServerError,
            "protocol_error" => Self::ProtocolError,
            "context_limit_exceeded" => Self::ContextLimitExceeded,
            "unknown" => Self::Unknown,
            _ => return Err(()),
        })
    }
}

/// Failure category used by health state. It is separate from retry policy:
/// later milestones decide whether a categorized outcome is retried.
pub type FailureCategory = BackoffReason;

/// Classify request-independent provider observations using the Python rules.
pub fn classify_failure_category(
    error_class: Option<&str>,
    status_code: Option<u16>,
) -> FailureCategory {
    if status_code == Some(402) {
        return BackoffReason::QuotaExhausted;
    }
    if status_code == Some(408) {
        return BackoffReason::ConnectTimeout;
    }
    if matches!(status_code, Some(409 | 422)) {
        return BackoffReason::Unknown;
    }
    let Some(error_class) = error_class else {
        return if status_code.is_some_and(|status| (500..600).contains(&status)) {
            BackoffReason::UpstreamServerError
        } else {
            BackoffReason::Unknown
        };
    };
    let error_class = error_class.to_ascii_lowercase();
    if error_class.contains("contextlimitexceeded")
        || error_class.contains("context_limit_exceeded")
    {
        return BackoffReason::ContextLimitExceeded;
    }
    if AUTH_FAILURE_CLASSES
        .iter()
        .any(|candidate| *candidate == error_class)
    {
        return BackoffReason::AuthenticationFailed;
    }
    if error_class.contains("quotaexhausted") || error_class.contains("quota_exhausted") {
        return BackoffReason::QuotaExhausted;
    }
    if error_class.contains("ratelimit")
        || error_class.contains("rate_limit")
        || status_code == Some(429)
    {
        return BackoffReason::RateLimited;
    }
    if error_class.contains("modelunavailable") || error_class.contains("model_not_found") {
        return BackoffReason::ModelUnavailable;
    }
    if error_class.contains("connecttimeout") || error_class.contains("connect_timeout") {
        return BackoffReason::ConnectTimeout;
    }
    if [
        "connectionfailure",
        "connection_failure",
        "connectionerror",
        "connecterror",
    ]
    .iter()
    .any(|term| error_class.contains(term))
    {
        return BackoffReason::ConnectionFailure;
    }
    if error_class.contains("timeout") {
        return BackoffReason::ConnectTimeout;
    }
    if (error_class.contains("temporary") || error_class.contains("transient"))
        && status_code.is_some_and(|status| (500..600).contains(&status))
    {
        return BackoffReason::UpstreamServerError;
    }
    if status_code.is_some_and(|status| (500..600).contains(&status)) {
        return BackoffReason::UpstreamServerError;
    }
    BackoffReason::Unknown
}

/// A reason-specific bounded exponential policy.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BackoffPolicy {
    pub base_delay: f64,
    pub multiplier: f64,
    pub cap: f64,
    pub jitter: f64,
    pub account_model_scope: bool,
    pub max_consecutive: u32,
}

pub fn get_backoff_policy(reason: BackoffReason) -> Option<BackoffPolicy> {
    let common = |base_delay, max_consecutive| BackoffPolicy {
        base_delay,
        multiplier: 2.0,
        cap: MAX_NONTERMINAL_BACKOFF_SECONDS,
        jitter: 0.15,
        account_model_scope: false,
        max_consecutive,
    };
    Some(match reason {
        BackoffReason::AuthenticationFailed => BackoffPolicy {
            base_delay: 0.0,
            multiplier: 1.0,
            cap: 0.0,
            jitter: 0.0,
            account_model_scope: false,
            max_consecutive: 0,
        },
        BackoffReason::QuotaExhausted => common(300.0, 3),
        BackoffReason::RateLimited => common(60.0, 5),
        BackoffReason::UpstreamServerError => common(20.0, 7),
        BackoffReason::ConnectTimeout
        | BackoffReason::ConnectionFailure
        | BackoffReason::ProtocolError => common(30.0, 6),
        BackoffReason::ModelUnavailable => BackoffPolicy {
            account_model_scope: true,
            ..common(300.0, 3)
        },
        BackoffReason::ContextLimitExceeded | BackoffReason::Unknown => return None,
    })
}

/// Injectable uniform source used only for bounded multiplicative jitter.
pub trait JitterSource {
    fn next_unit(&mut self) -> f64;
}

/// Small deterministic RNG for tests and differential traces.
#[derive(Debug, Clone)]
pub struct SeededJitter(u64);

impl SeededJitter {
    pub const fn new(seed: u64) -> Self {
        Self(seed)
    }
}

impl JitterSource for SeededJitter {
    fn next_unit(&mut self) -> f64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1);
        (self.0 >> 11) as f64 / (1_u64 << 53) as f64
    }
}

struct ZeroJitter;

impl JitterSource for ZeroJitter {
    fn next_unit(&mut self) -> f64 {
        0.5
    }
}

pub fn compute_backoff_seconds(
    reason: BackoffReason,
    consecutive_failures: u32,
    retry_after: Option<f64>,
    jitter: bool,
) -> Option<f64> {
    let mut source = ZeroJitter;
    compute_backoff_seconds_with_rng(
        reason,
        consecutive_failures,
        retry_after,
        jitter,
        &mut source,
    )
}

pub fn compute_backoff_seconds_with_rng(
    reason: BackoffReason,
    consecutive_failures: u32,
    retry_after: Option<f64>,
    jitter: bool,
    source: &mut dyn JitterSource,
) -> Option<f64> {
    let policy = get_backoff_policy(reason)?;
    if policy.base_delay <= 0.0 || policy.cap <= 0.0 {
        return None;
    }
    if matches!(
        reason,
        BackoffReason::QuotaExhausted | BackoffReason::RateLimited
    ) && retry_after.is_some_and(|value| value.is_finite() && value >= 0.0)
    {
        let mut delay = retry_after
            .unwrap_or_default()
            .min(MAX_NONTERMINAL_BACKOFF_SECONDS);
        if jitter {
            delay *= 1.0 - source.next_unit() * policy.jitter;
        }
        return Some(delay.clamp(0.0, MAX_NONTERMINAL_BACKOFF_SECONDS));
    }
    let doublings = consecutive_failures
        .saturating_sub(1)
        .min(policy.max_consecutive);
    let mut delay = policy.base_delay;
    for _ in 0..doublings {
        delay = (delay * policy.multiplier).min(policy.cap);
    }
    if jitter {
        delay *= (1.0 - policy.jitter) + source.next_unit() * (2.0 * policy.jitter);
    }
    Some(delay.clamp(0.0, MAX_NONTERMINAL_BACKOFF_SECONDS))
}
