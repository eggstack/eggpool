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
            })),
            clock: Arc::new(clock),
            failure_threshold: failure_threshold.max(1),
            recovery_timeout: recovery_timeout.max(0.0),
            success_threshold: success_threshold.max(1),
        }
    }

    pub fn state(&self) -> CircuitState {
        self.inner
            .lock()
            .expect("circuit lock is not poisoned")
            .state
    }

    pub fn can_request(&self) -> bool {
        let inner = self.inner.lock().expect("circuit lock is not poisoned");
        match inner.state {
            CircuitState::Closed => true,
            CircuitState::Open => self.recovery_elapsed(&inner),
            CircuitState::HalfOpen => inner
                .probe_acquired_at
                .is_none_or(|acquired| self.now() - acquired >= self.recovery_timeout),
        }
    }

    pub fn allow_request(&self) -> bool {
        let now = self.now();
        let mut inner = self.inner.lock().expect("circuit lock is not poisoned");
        match inner.state {
            CircuitState::Closed => true,
            CircuitState::Open if self.recovery_elapsed(&inner) => {
                inner.state = CircuitState::HalfOpen;
                inner.last_state_change = now;
                inner.probe_acquired_at = Some(now);
                true
            }
            CircuitState::Open => false,
            CircuitState::HalfOpen => {
                if inner
                    .probe_acquired_at
                    .is_some_and(|acquired| now - acquired < self.recovery_timeout)
                {
                    false
                } else {
                    inner.probe_acquired_at = Some(now);
                    true
                }
            }
        }
    }

    pub fn release_probe(&self) {
        self.inner
            .lock()
            .expect("circuit lock is not poisoned")
            .probe_acquired_at = None;
    }

    pub fn record_success(&self) {
        let now = self.now();
        let mut inner = self.inner.lock().expect("circuit lock is not poisoned");
        match inner.state {
            CircuitState::HalfOpen => {
                inner.success_count = inner.success_count.saturating_add(1);
                inner.probe_acquired_at = None;
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
        let mut inner = self.inner.lock().expect("circuit lock is not poisoned");
        match inner.state {
            CircuitState::HalfOpen => {
                inner.state = CircuitState::Open;
                inner.failure_count = self.failure_threshold;
                inner.success_count = 0;
                inner.last_failure_at = Some(now);
                inner.last_state_change = now;
                inner.probe_acquired_at = None;
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
        *self.inner.lock().expect("circuit lock is not poisoned") = CircuitInner {
            state: CircuitState::Closed,
            failure_count: 0,
            success_count: 0,
            last_failure_at: None,
            last_state_change: now,
            probe_acquired_at: None,
        };
    }

    pub fn stats(&self) -> CircuitStats {
        let inner = self.inner.lock().expect("circuit lock is not poisoned");
        CircuitStats {
            state: inner.state,
            failure_count: inner.failure_count,
            success_count: inner.success_count,
            last_failure_at: inner.last_failure_at,
            last_state_change: inner.last_state_change,
            probe_in_flight: inner.probe_acquired_at.is_some(),
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
