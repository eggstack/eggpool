//! EggPool-owned bounded semantic affinity around the neutral routing crate.
//!
//! Policy compilation, registry semantics, and session identity primitives
//! live in `eggpool-model-routing`. This module retains only the application
//! cache because its Tokio single-flight behavior is an EggPool runtime
//! concern and is not yet required by downstream consumers.

use std::{
    collections::{HashMap, VecDeque},
    fmt,
    sync::{Arc, Mutex},
    time::Instant,
};

use eggpool_model_routing::SessionIdentity;
use tokio::sync::watch;

pub use eggpool_model_routing::{
    AFFINITY_SESSION_HEADER_MAX_BYTES, AUTOMATIC_FIRST_USER_MIN_BYTES, AUTOMATIC_PREFIX_MAX_BYTES,
    AffinityIdentityInput, COMPILED_POLICY_MAX_BYTES, CompiledModelRoute, CompiledModelRouter,
    ConversationPrefix, ConversationTextFragment, ModelRoutePolicy, ModelRouterPolicy,
    ModelRouterRegistry, SELECTOR_PROTOCOL_VERSION, SessionSource, automatic_session_identity,
    session_identity_from_header,
};

pub fn compile_model_router(
    virtual_model: &str,
    router: &crate::config::ModelRouterConfig,
) -> Result<CompiledModelRouter, crate::config::ConfigError> {
    eggpool_model_routing::compile_model_router(
        virtual_model,
        &crate::config::model_router_policy(router),
    )
    .map_err(|error| crate::config::ConfigError::validation(error.to_string()))
}

pub const AFFINITY_CACHE_MAX_ENTRIES: usize = 4_096;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AffinityDecisionSource {
    Selector,
    Default,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AffinitySelection {
    pub virtual_model: String,
    pub route_id: String,
    pub route_label: String,
    pub concrete_model: String,
    pub source: AffinityDecisionSource,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AffinityDecision {
    pub virtual_model: String,
    pub router_fingerprint: String,
    pub session_digest: [u8; 32],
    pub route_id: String,
    pub route_label: String,
    pub concrete_model: String,
    pub source: AffinityDecisionSource,
    pub expires_at_monotonic: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AffinityResolution {
    pub decision: AffinityDecision,
    pub cache_hit: bool,
    pub single_flight_join: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AffinityStats {
    pub hits: u64,
    pub misses: u64,
    pub expirations: u64,
    pub evictions: u64,
    pub single_flight_leaders: u64,
    pub single_flight_joins: u64,
    pub entry_count: usize,
    pub inflight_key_count: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AffinityError {
    InvalidSelection,
    SelectorFailed,
}

impl fmt::Display for AffinityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSelection => {
                formatter.write_str("model-router selection is not in the compiled route map")
            }
            Self::SelectorFailed => formatter.write_str("model-router selector failed"),
        }
    }
}

impl std::error::Error for AffinityError {}

#[derive(Clone, Hash, PartialEq, Eq)]
struct AffinityKey {
    virtual_model: String,
    fingerprint: String,
    session_digest: [u8; 32],
}

#[derive(Clone)]
enum FlightResult {
    Succeeded(AffinityDecision),
    Failed(AffinityError),
    Aborted,
}

struct Flight {
    result: watch::Sender<Option<FlightResult>>,
}

struct AffinityState {
    entries: HashMap<AffinityKey, AffinityDecision>,
    lru: VecDeque<AffinityKey>,
    flights: HashMap<AffinityKey, Arc<Flight>>,
    stats: AffinityStats,
}

struct FlightGuard {
    owner: Arc<Mutex<AffinityState>>,
    key: AffinityKey,
    flight: Arc<Flight>,
    armed: bool,
}

impl Drop for FlightGuard {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        if let Ok(mut state) = self.owner.lock()
            && state
                .flights
                .get(&self.key)
                .is_some_and(|flight| Arc::ptr_eq(flight, &self.flight))
        {
            state.flights.remove(&self.key);
            self.flight.result.send_replace(Some(FlightResult::Aborted));
        }
        self.armed = false;
    }
}

pub struct ModelRouterAffinity {
    max_entries: usize,
    clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    state: Arc<Mutex<AffinityState>>,
}

impl fmt::Debug for ModelRouterAffinity {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ModelRouterAffinity")
            .field("max_entries", &self.max_entries)
            .field("stats", &self.stats())
            .finish()
    }
}

impl ModelRouterAffinity {
    pub fn new() -> Self {
        let origin = Instant::now();
        Self::with_clock(AFFINITY_CACHE_MAX_ENTRIES, move || {
            origin.elapsed().as_secs_f64()
        })
    }

    pub fn with_capacity(max_entries: usize) -> Self {
        let origin = Instant::now();
        Self::with_clock(max_entries, move || origin.elapsed().as_secs_f64())
    }

    pub fn with_clock<F>(max_entries: usize, clock: F) -> Self
    where
        F: Fn() -> f64 + Send + Sync + 'static,
    {
        assert!(max_entries > 0, "affinity cache capacity must be positive");
        Self {
            max_entries,
            clock: Arc::new(clock),
            state: Arc::new(Mutex::new(AffinityState {
                entries: HashMap::new(),
                lru: VecDeque::new(),
                flights: HashMap::new(),
                stats: AffinityStats {
                    hits: 0,
                    misses: 0,
                    expirations: 0,
                    evictions: 0,
                    single_flight_leaders: 0,
                    single_flight_joins: 0,
                    entry_count: 0,
                    inflight_key_count: 0,
                },
            })),
        }
    }

    pub fn max_entries(&self) -> usize {
        self.max_entries
    }

    /// Read a still-valid decision without invoking selection. The current
    /// compiled route map is checked before a hit is returned.
    pub fn get(
        &self,
        router: &CompiledModelRouter,
        identity: &SessionIdentity,
    ) -> Option<AffinityDecision> {
        self.lookup(&Self::key(router, identity), router)
    }

    pub fn stats(&self) -> AffinityStats {
        let mut state = self.state.lock().expect("affinity state lock");
        state.stats.entry_count = state.entries.len();
        state.stats.inflight_key_count = state.flights.len();
        state.stats
    }

    fn key(router: &CompiledModelRouter, identity: &SessionIdentity) -> AffinityKey {
        AffinityKey {
            virtual_model: router.virtual_model.clone(),
            fingerprint: router.config_fingerprint.clone(),
            session_digest: identity.digest,
        }
    }

    fn valid_cached_target(router: &CompiledModelRouter, decision: &AffinityDecision) -> bool {
        router
            .route_by_id
            .get(&decision.route_id)
            .is_some_and(|route| {
                route.label == decision.route_label
                    && route.model == decision.concrete_model
                    && decision.virtual_model == router.virtual_model
            })
    }

    fn lookup(&self, key: &AffinityKey, router: &CompiledModelRouter) -> Option<AffinityDecision> {
        let mut state = self.state.lock().expect("affinity state lock");
        let now = (self.clock)();
        let Some(decision) = state.entries.get(key).cloned() else {
            state.stats.misses += 1;
            return None;
        };
        if decision.expires_at_monotonic <= now {
            state.entries.remove(key);
            state.lru.retain(|item| item != key);
            state.stats.expirations += 1;
            state.stats.misses += 1;
            return None;
        }
        if !Self::valid_cached_target(router, &decision) {
            state.entries.remove(key);
            state.lru.retain(|item| item != key);
            state.stats.misses += 1;
            return None;
        }
        state.lru.retain(|item| item != key);
        state.lru.push_back(key.clone());
        state.stats.hits += 1;
        Some(decision)
    }

    fn cleanup_expired(&self, state: &mut AffinityState, limit: usize) {
        let now = (self.clock)();
        let mut checked = 0;
        let mut index = 0;
        while index < state.lru.len() && checked < limit {
            let key = state.lru[index].clone();
            checked += 1;
            if state
                .entries
                .get(&key)
                .is_some_and(|decision| decision.expires_at_monotonic <= now)
            {
                state.entries.remove(&key);
                state.lru.remove(index);
                state.stats.expirations += 1;
            } else {
                index += 1;
            }
        }
    }

    fn store(&self, key: AffinityKey, decision: AffinityDecision) {
        let mut state = self.state.lock().expect("affinity state lock");
        self.cleanup_expired(&mut state, 16);
        state.entries.remove(&key);
        state.lru.retain(|item| item != &key);
        while state.entries.len() >= self.max_entries {
            if let Some(oldest) = state.lru.pop_front() {
                if state.entries.remove(&oldest).is_some() {
                    state.stats.evictions += 1;
                }
            } else {
                break;
            }
        }
        state.lru.push_back(key.clone());
        state.entries.insert(key, decision);
    }

    fn decision_from_selection(
        router: &CompiledModelRouter,
        identity: &SessionIdentity,
        selection: AffinitySelection,
        expires_at_monotonic: f64,
    ) -> Result<AffinityDecision, AffinityError> {
        let Some(route) = router.route_by_id.get(&selection.route_id) else {
            return Err(AffinityError::InvalidSelection);
        };
        if selection.virtual_model != router.virtual_model
            || route.label != selection.route_label
            || route.model != selection.concrete_model
        {
            return Err(AffinityError::InvalidSelection);
        }
        Ok(AffinityDecision {
            virtual_model: router.virtual_model.clone(),
            router_fingerprint: router.config_fingerprint.clone(),
            session_digest: identity.digest,
            route_id: route.route_id.clone(),
            route_label: route.label.clone(),
            concrete_model: route.model.clone(),
            source: selection.source,
            expires_at_monotonic,
        })
    }

    pub async fn resolve<F, Fut>(
        &self,
        router: &CompiledModelRouter,
        identity: &SessionIdentity,
        selector: F,
    ) -> Result<AffinityResolution, AffinityError>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<AffinitySelection, AffinityError>>,
    {
        if !router.sticky {
            let selection = selector().await?;
            let decision = Self::decision_from_selection(
                router,
                identity,
                selection,
                (self.clock)() + router.affinity_ttl_s,
            )?;
            return Ok(AffinityResolution {
                decision,
                cache_hit: false,
                single_flight_join: false,
            });
        }
        let key = Self::key(router, identity);
        let mut selector = Some(selector);
        loop {
            if let Some(decision) = self.lookup(&key, router) {
                return Ok(AffinityResolution {
                    decision,
                    cache_hit: true,
                    single_flight_join: false,
                });
            }

            let (flight, leader) = {
                let mut state = self.state.lock().expect("affinity state lock");
                if let Some(flight) = state.flights.get(&key).cloned() {
                    state.stats.single_flight_joins += 1;
                    (Some(flight), false)
                } else if state.flights.len() >= self.max_entries {
                    (None, false)
                } else {
                    let (sender, _) = watch::channel(None);
                    let flight = Arc::new(Flight { result: sender });
                    state.flights.insert(key.clone(), flight.clone());
                    state.stats.single_flight_leaders += 1;
                    (Some(flight), true)
                }
            };

            if !leader {
                if let Some(flight) = flight {
                    let mut receiver = flight.result.subscribe();
                    loop {
                        if let Some(result) = receiver.borrow().clone() {
                            match result {
                                FlightResult::Succeeded(decision) => {
                                    return Ok(AffinityResolution {
                                        decision,
                                        cache_hit: false,
                                        single_flight_join: true,
                                    });
                                }
                                FlightResult::Failed(error) => return Err(error),
                                FlightResult::Aborted => break,
                            }
                        }
                        if receiver.changed().await.is_err() {
                            break;
                        }
                    }
                    continue;
                }
                let selection = selector.take().expect("selector closure")().await?;
                let decision = Self::decision_from_selection(
                    router,
                    identity,
                    selection,
                    (self.clock)() + router.affinity_ttl_s,
                )?;
                self.store(key.clone(), decision.clone());
                return Ok(AffinityResolution {
                    decision,
                    cache_hit: false,
                    single_flight_join: false,
                });
            }

            let flight = flight.expect("leader flight");
            let mut guard = FlightGuard {
                owner: self.state.clone(),
                key: key.clone(),
                flight: flight.clone(),
                armed: true,
            };
            let selection = selector.take().expect("selector closure")().await;
            let decision = match selection {
                Ok(selection) => Self::decision_from_selection(
                    router,
                    identity,
                    selection,
                    (self.clock)() + router.affinity_ttl_s,
                ),
                Err(error) => Err(error),
            };
            match decision {
                Ok(decision) => {
                    self.store(key.clone(), decision.clone());
                    {
                        let mut state = self.state.lock().expect("affinity state lock");
                        state.flights.remove(&key);
                    }
                    flight
                        .result
                        .send_replace(Some(FlightResult::Succeeded(decision.clone())));
                    guard.armed = false;
                    return Ok(AffinityResolution {
                        decision,
                        cache_hit: false,
                        single_flight_join: false,
                    });
                }
                Err(error) => {
                    {
                        let mut state = self.state.lock().expect("affinity state lock");
                        state.flights.remove(&key);
                    }
                    flight
                        .result
                        .send_replace(Some(FlightResult::Failed(error)));
                    guard.armed = false;
                    return Err(error);
                }
            }
        }
    }
}

impl Default for ModelRouterAffinity {
    fn default() -> Self {
        Self::new()
    }
}
