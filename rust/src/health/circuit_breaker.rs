//! Synchronous three-state circuit breaker with one half-open probe.

use std::{
    sync::{Arc, Mutex},
    time::Instant,
};

/// Circuit states visible to routing and diagnostics.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CircuitState {
    Closed,
    Open,
    HalfOpen,
}

impl CircuitState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Closed => "closed",
            Self::Open => "open",
            Self::HalfOpen => "half_open",
        }
    }
}

/// Monotonic clock used by the default breaker.
#[derive(Debug, Clone)]
pub struct MonotonicClock(Instant);

impl Default for MonotonicClock {
    fn default() -> Self {
        Self(Instant::now())
    }
}

impl MonotonicClock {
    pub fn now(&self) -> f64 {
        self.0.elapsed().as_secs_f64()
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct CircuitInner {
    state: CircuitState,
    failure_count: u32,
    success_count: u32,
    last_failure_at: Option<f64>,
    last_state_change: f64,
    probe_acquired_at: Option<f64>,
    probe_in_flight: bool,
}

/// Diagnostic snapshot of the breaker, with no secret or request data.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CircuitStats {
    pub state: CircuitState,
    pub failure_count: u32,
    pub success_count: u32,
    pub last_failure_at: Option<f64>,
    pub last_state_change: f64,
    pub probe_in_flight: bool,
}

/// A lock-protected breaker. Critical sections are synchronous and contain no
/// await, so it is safe to use from both Tokio and ordinary diagnostic code.
#[derive(Clone)]
pub struct CircuitBreaker {
    inner: Arc<Mutex<CircuitInner>>,
    clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    failure_threshold: u32,
    recovery_timeout: f64,
    success_threshold: u32,
}

impl std::fmt::Debug for CircuitBreaker {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CircuitBreaker")
            .field("stats", &self.stats())
            .field("failure_threshold", &self.failure_threshold)
            .field("recovery_timeout", &self.recovery_timeout)
            .field("success_threshold", &self.success_threshold)
            .finish()
    }
}

impl Default for CircuitBreaker {
    fn default() -> Self {
        let clock = MonotonicClock::default();
        Self::with_clock(move || clock.now(), 5, 300.0, 1)
    }
}

impl CircuitBreaker {
    pub fn new(clock: impl Fn() -> f64 + Send + Sync + 'static) -> Self {
        Self::with_clock(clock, 5, 300.0, 1)
    }

    pub fn with_clock<C>(
        clock: C,
        failure_threshold: u32,
        recovery_timeout: f64,
        success_threshold: u32,
    ) -> Self
    where
        C: Fn() -> f64 + Send + Sync + 'static,
    {
        let now = clock();
        Self {
            inner: Arc::new(Mutex::new(CircuitInner {
                state: CircuitState::Closed,
                failure_count: 0,
                success_count: 0,
                last_failure_at: None,
                last_state_change: now,
                probe_acquired_at: None,
                probe_in_flight: false,
            })),
            clock: Arc::new(clock),
            failure_threshold: failure_threshold.max(1),
            recovery_timeout: recovery_timeout.max(0.0),
            success_threshold: success_threshold.max(1),
        }
    }

    /// Fail-closed gating view: poisoned locks deny new work. `state()` and
    /// `stats()` below are best-effort diagnostics that report `Open` on
    /// poison so dashboards do not show a healthy breaker while gating is
    /// denying traffic.
    pub fn state(&self) -> CircuitState {
        match self.inner.lock() {
            Ok(inner) => inner.state,
            Err(_) => CircuitState::Open,
        }
    }

    pub fn can_request(&self) -> bool {
        let Ok(inner) = self.inner.lock() else {
            return false;
        };
        match inner.state {
            CircuitState::Closed => true,
            CircuitState::Open => self.recovery_elapsed(&inner),
            CircuitState::HalfOpen => !inner.probe_in_flight,
        }
    }

    pub fn allow_request(&self) -> bool {
        let now = self.now();
        let Ok(mut inner) = self.inner.lock() else {
            return false;
        };
        match inner.state {
            CircuitState::Closed => true,
            CircuitState::Open if self.recovery_elapsed(&inner) => {
                inner.state = CircuitState::HalfOpen;
                inner.last_state_change = now;
                inner.probe_acquired_at = Some(now);
                inner.probe_in_flight = true;
                true
            }
            CircuitState::Open => false,
            CircuitState::HalfOpen => {
                if inner.probe_in_flight {
                    false
                } else {
                    inner.probe_acquired_at = Some(now);
                    inner.probe_in_flight = true;
                    true
                }
            }
        }
    }

    /// Cancel-safe probe acquisition. The returned guard releases the
    /// half-open slot on drop, so a cancelled request cannot stall the
    /// breaker in one-probe-forever. Callers that complete the probe must
    /// still call `record_success`/`record_failure` (which consume the slot)
    /// and may drop the guard afterwards; dropping without recording only
    /// releases the slot.
    pub fn try_acquire_probe(&self) -> Option<ProbeGuard> {
        let now = self.now();
        let mut inner = self.inner.lock().ok()?;
        match inner.state {
            CircuitState::Closed => Some(ProbeGuard {
                breaker: self.clone(),
                armed: false,
            }),
            CircuitState::Open if self.recovery_elapsed(&inner) => {
                inner.state = CircuitState::HalfOpen;
                inner.last_state_change = now;
                inner.probe_acquired_at = Some(now);
                inner.probe_in_flight = true;
                Some(ProbeGuard {
                    breaker: self.clone(),
                    armed: true,
                })
            }
            CircuitState::Open => None,
            CircuitState::HalfOpen => {
                if inner.probe_in_flight {
                    None
                } else {
                    inner.probe_acquired_at = Some(now);
                    inner.probe_in_flight = true;
                    Some(ProbeGuard {
                        breaker: self.clone(),
                        armed: true,
                    })
                }
            }
        }
    }

    pub fn release_probe(&self) {
        let mut inner = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        inner.probe_acquired_at = None;
        inner.probe_in_flight = false;
    }

    pub fn record_success(&self) {
        let now = self.now();
        let mut inner = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        match inner.state {
            CircuitState::HalfOpen => {
                inner.success_count = inner.success_count.saturating_add(1);
                inner.probe_acquired_at = None;
                inner.probe_in_flight = false;
                if inner.success_count >= self.success_threshold {
                    inner.state = CircuitState::Closed;
                    inner.failure_count = 0;
                    inner.success_count = 0;
                    inner.last_failure_at = None;
                    inner.last_state_change = now;
                }
            }
            CircuitState::Closed => inner.failure_count = 0,
            CircuitState::Open => {}
        }
    }

    pub fn record_failure(&self) {
        let now = self.now();
        let mut inner = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        match inner.state {
            CircuitState::HalfOpen => {
                inner.state = CircuitState::Open;
                inner.failure_count = self.failure_threshold;
                inner.success_count = 0;
                inner.last_failure_at = Some(now);
                inner.last_state_change = now;
                inner.probe_acquired_at = None;
                inner.probe_in_flight = false;
            }
            CircuitState::Closed => {
                inner.failure_count = inner.failure_count.saturating_add(1);
                if inner.failure_count >= self.failure_threshold {
                    inner.state = CircuitState::Open;
                    inner.last_failure_at = Some(now);
                    inner.last_state_change = now;
                }
            }
            CircuitState::Open => {}
        }
    }

    pub fn reset(&self) {
        let now = self.now();
        *self.inner.lock().unwrap_or_else(|error| error.into_inner()) = CircuitInner {
            state: CircuitState::Closed,
            failure_count: 0,
            success_count: 0,
            last_failure_at: None,
            last_state_change: now,
            probe_acquired_at: None,
            probe_in_flight: false,
        };
    }

    pub fn stats(&self) -> CircuitStats {
        match self.inner.lock() {
            Ok(inner) => CircuitStats {
                state: inner.state,
                failure_count: inner.failure_count,
                success_count: inner.success_count,
                last_failure_at: inner.last_failure_at,
                last_state_change: inner.last_state_change,
                probe_in_flight: inner.probe_in_flight,
            },
            Err(error) => {
                let inner = error.into_inner();
                // Fail-closed diagnostic view: report Open while preserving
                // counters from the poisoned snapshot.
                CircuitStats {
                    state: CircuitState::Open,
                    failure_count: inner.failure_count,
                    success_count: inner.success_count,
                    last_failure_at: inner.last_failure_at,
                    last_state_change: inner.last_state_change,
                    probe_in_flight: true,
                }
            }
        }
    }

    fn now(&self) -> f64 {
        (self.clock)()
    }

    fn recovery_elapsed(&self, inner: &CircuitInner) -> bool {
        inner
            .last_failure_at
            .is_some_and(|failure| self.now() - failure >= self.recovery_timeout)
    }
}

/// RAII lease for a half-open probe slot. Dropping without an explicit
/// `record_success`/`record_failure` releases the slot so cancellation cannot
/// stall the breaker. Closed-state acquisitions are no-ops on drop.
pub struct ProbeGuard {
    breaker: CircuitBreaker,
    armed: bool,
}

impl ProbeGuard {
    /// Disarm the guard after the probe completed via `record_success` or
    /// `record_failure` (also fine to drop armed: drop only releases).
    pub fn disarm(mut self) {
        self.armed = false;
    }
}

impl Drop for ProbeGuard {
    fn drop(&mut self) {
        if self.armed {
            self.breaker.release_probe();
        }
    }
}

impl std::fmt::Debug for ProbeGuard {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ProbeGuard")
            .field("armed", &self.armed)
            .finish()
    }
}
