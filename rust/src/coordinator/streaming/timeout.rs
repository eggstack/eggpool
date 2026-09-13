//! Timeout policy for the pre-handoff and downstream streaming phases.

// ---------------------------------------------------------------------------
// Timeout policy
// ---------------------------------------------------------------------------

use std::time::Duration;

use crate::config::{ProviderConfig, ProviderStreamTimeoutConfig};

/// M7-owned streaming timeout policy.
///
/// `None` preserves the historical transport behavior for that phase (no
/// coordinator timer). There is intentionally no whole-stream deadline field.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct StreamTimeoutPolicy {
    /// Time allowed for upstream response headers after dispatch.
    pub header_timeout: Option<Duration>,
    /// Time allowed for the first provider body byte after headers.
    pub first_byte_timeout: Option<Duration>,
    /// Inactivity allowed between provider body chunks once streaming.
    pub idle_timeout: Option<Duration>,
}

impl StreamTimeoutPolicy {
    /// Resolve the policy from one provider's configuration.
    ///
    /// The header timer uses the provider read timeout (the same guardrail
    /// the transport applies to header wait); first-byte/idle timers come
    /// from the explicit stream-timeout policy. `max_lifetime_s` is parsed
    /// for compatibility but never enforced.
    pub fn from_provider(provider: &ProviderConfig) -> Self {
        Self {
            header_timeout: duration_from_secs(provider.read_timeout_s),
            first_byte_timeout: provider
                .stream_timeouts
                .first_byte_timeout_s
                .and_then(duration_from_secs),
            idle_timeout: provider
                .stream_timeouts
                .idle_timeout_s
                .and_then(duration_from_secs),
        }
    }

    /// Test override for the provider stream-timeout configuration.
    pub fn test_config(
        first_byte_timeout_s: Option<f64>,
        idle_timeout_s: Option<f64>,
    ) -> ProviderStreamTimeoutConfig {
        ProviderStreamTimeoutConfig {
            first_byte_timeout_s,
            idle_timeout_s,
            max_lifetime_s: None,
        }
    }
}

fn duration_from_secs(seconds: f64) -> Option<Duration> {
    Duration::try_from_secs_f64(seconds)
        .ok()
        .filter(|duration| !duration.is_zero())
}
