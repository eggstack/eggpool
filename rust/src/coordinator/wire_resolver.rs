//! Process-owned wire candidate ordering and negotiation ownership.
//!
//! The resolver is deliberately unaware of HTTP status codes and response
//! bodies.  Callers report an authorized accept/reject decision after the
//! provider boundary has classified the result.

use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use sha2::{Digest, Sha256};

use crate::config::WireNegotiationConfig;
use crate::wire::{ConfiguredWireProfile, WireSurface};

const DEFAULT_CACHE_CAPACITY: usize = 2_048;
const DEFAULT_LEARNED_TTL: Duration = Duration::from_secs(86_400);
const DEFAULT_REJECTION_TTL: Duration = Duration::from_secs(300);
const DEFAULT_NEGOTIATION_INTERVAL: Duration = Duration::from_secs(1);

#[derive(Debug, Clone)]
pub struct WireResolverConfig {
    pub enabled: bool,
    pub cache_capacity: usize,
    pub learned_ttl: Duration,
    pub rejection_ttl: Duration,
    pub min_negotiation_interval: Duration,
    pub max_concurrent_per_provider: usize,
    pub max_provider_state: usize,
    pub max_metric_labels: usize,
}

impl Default for WireResolverConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            cache_capacity: DEFAULT_CACHE_CAPACITY,
            learned_ttl: DEFAULT_LEARNED_TTL,
            rejection_ttl: DEFAULT_REJECTION_TTL,
            min_negotiation_interval: DEFAULT_NEGOTIATION_INTERVAL,
            max_concurrent_per_provider: 1,
            max_provider_state: 256,
            max_metric_labels: 256,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireCandidate {
    pub profile: ConfiguredWireProfile,
    pub fingerprint: String,
}

impl WireCandidate {
    pub fn new(profile: ConfiguredWireProfile, fingerprint: impl Into<String>) -> Self {
        Self {
            profile,
            fingerprint: fingerprint.into(),
        }
    }

    pub fn surface(&self) -> WireSurface {
        self.profile.definition.surface
    }
}

impl WireResolverConfig {
    /// Convert the validated configuration surface into the process-owned
    /// resolver policy. The provider-state and metric-label bounds remain
    /// implementation-owned safety limits.
    pub fn from_config(config: &WireNegotiationConfig) -> Self {
        Self {
            enabled: config.enabled,
            cache_capacity: config.cache_max_entries as usize,
            learned_ttl: duration_from_seconds(config.learned_preference_ttl_s),
            rejection_ttl: duration_from_seconds(config.rejection_cooldown_s),
            min_negotiation_interval: duration_from_seconds(config.min_negotiation_interval_s),
            max_concurrent_per_provider: usize::from(config.max_concurrent_per_provider),
            ..Self::default()
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireResolution {
    pub candidates: Vec<WireCandidate>,
    pub fingerprint: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NegotiationRole {
    NotNeeded,
    Leader,
    Follower,
    Throttled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NegotiationResult {
    Accepted(WireSurface),
    Rejected,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct CacheKey {
    provider_id: String,
    model_id: String,
    fingerprint: String,
}

type FlightKey = (String, String);

#[derive(Debug, Clone)]
struct Learned {
    surface: WireSurface,
    observed_at: Instant,
}

#[derive(Debug, Default)]
struct CacheEntry {
    learned: Option<Learned>,
    rejected_at: BTreeMap<WireSurface, Instant>,
}

#[derive(Debug)]
struct Flight {
    result: Mutex<Option<NegotiationResult>>,
    notify: tokio::sync::Notify,
}

#[derive(Debug, Default)]
struct ResolverState {
    entries: BTreeMap<CacheKey, CacheEntry>,
    lru: VecDeque<CacheKey>,
    flights: BTreeMap<FlightKey, Arc<Flight>>,
    last_negotiation: BTreeMap<String, Instant>,
    negotiation_delay_until: BTreeMap<String, Instant>,
    operator_preferences: BTreeMap<(String, String), (WireSurface, bool)>,
    metadata_hints: BTreeMap<(String, String), WireSurface>,
    metrics: BTreeMap<String, u64>,
}

#[derive(Debug)]
pub struct NegotiationLease {
    resolver: WireResolver,
    key: CacheKey,
    flight_key: FlightKey,
    role: NegotiationRole,
    permit: Option<ProviderPermit>,
    flight: Arc<Flight>,
    finished: bool,
}

impl NegotiationLease {
    pub fn role(&self) -> NegotiationRole {
        self.role
    }

    pub async fn wait(self) -> NegotiationResult {
        if self.role == NegotiationRole::Leader {
            return NegotiationResult::Rejected;
        }
        loop {
            if let Some(result) = self.flight.result.lock().expect("flight lock").clone() {
                return result;
            }
            self.flight.notify.notified().await;
        }
    }

    pub fn finish(mut self, result: NegotiationResult, now: Instant) {
        if self.role != NegotiationRole::Leader {
            return;
        }
        self.finished = true;
        self.resolver
            .finish_leader(&self.key, &self.flight, result, now);
    }
}

impl Drop for NegotiationLease {
    fn drop(&mut self) {
        if self.role == NegotiationRole::Leader && !self.finished {
            self.resolver.cancel_leader(&self.flight_key, &self.flight);
        }
        let _ = self.permit.take();
        self.resolver.cleanup_provider_state(&self.key.provider_id);
    }
}

#[derive(Debug, Clone)]
pub struct WireResolver {
    config: Arc<Mutex<WireResolverConfig>>,
    state: Arc<Mutex<ResolverState>>,
    provider_gates: Arc<Mutex<BTreeMap<String, Arc<ProviderGate>>>>,
}

#[derive(Debug)]
struct ProviderGate {
    limit: std::sync::atomic::AtomicUsize,
    in_flight: std::sync::atomic::AtomicUsize,
}

impl ProviderGate {
    fn new(limit: usize) -> Self {
        Self {
            limit: std::sync::atomic::AtomicUsize::new(limit.max(1)),
            in_flight: std::sync::atomic::AtomicUsize::new(0),
        }
    }

    fn set_limit(&self, limit: usize) {
        self.limit
            .store(limit.max(1), std::sync::atomic::Ordering::Release);
    }

    fn try_acquire(self: &Arc<Self>) -> Option<ProviderPermit> {
        let limit = self.limit.load(std::sync::atomic::Ordering::Acquire);
        let mut current = self.in_flight.load(std::sync::atomic::Ordering::Acquire);
        loop {
            if current >= limit {
                return None;
            }
            match self.in_flight.compare_exchange_weak(
                current,
                current + 1,
                std::sync::atomic::Ordering::AcqRel,
                std::sync::atomic::Ordering::Acquire,
            ) {
                Ok(_) => {
                    return Some(ProviderPermit {
                        gate: Arc::clone(self),
                    });
                }
                Err(observed) => current = observed,
            }
        }
    }

    fn is_idle(&self) -> bool {
        self.in_flight.load(std::sync::atomic::Ordering::Acquire) == 0
    }
}

#[derive(Debug)]
struct ProviderPermit {
    gate: Arc<ProviderGate>,
}

impl Drop for ProviderPermit {
    fn drop(&mut self) {
        self.gate
            .in_flight
            .fetch_sub(1, std::sync::atomic::Ordering::AcqRel);
    }
}

/// A policy change prepared outside the reload admission gate and committed
/// only during the short R007 acceptance window.
#[derive(Debug)]
pub struct WireResolverPolicyStage {
    resolver: WireResolver,
    previous: WireResolverConfig,
    next: WireResolverConfig,
    committed: bool,
    finalized: bool,
}

impl WireResolverPolicyStage {
    pub fn commit(&mut self) {
        if self.committed {
            return;
        }
        self.resolver.apply_config(self.next.clone());
        self.committed = true;
    }

    pub fn rollback(&mut self) {
        if self.committed && !self.finalized {
            self.resolver.apply_config(self.previous.clone());
            self.committed = false;
        }
    }

    pub fn finalize(&mut self) {
        self.resolver.enforce_bounds();
        self.finalized = true;
    }
}

impl Drop for WireResolverPolicyStage {
    fn drop(&mut self) {
        self.rollback();
    }
}

impl WireResolver {
    pub fn new(config: WireResolverConfig) -> Self {
        Self {
            config: Arc::new(Mutex::new(config)),
            state: Arc::new(Mutex::new(ResolverState::default())),
            provider_gates: Arc::new(Mutex::new(BTreeMap::new())),
        }
    }

    pub fn config(&self) -> WireResolverConfig {
        self.config
            .lock()
            .expect("wire resolver config lock")
            .clone()
    }

    pub fn stage_config(&self, next: WireResolverConfig) -> WireResolverPolicyStage {
        WireResolverPolicyStage {
            resolver: self.clone(),
            previous: self.config(),
            next,
            committed: false,
            finalized: false,
        }
    }

    pub fn resolve(
        &self,
        provider_id: &str,
        model_id: &str,
        mut candidates: Vec<WireCandidate>,
        now: Instant,
    ) -> WireResolution {
        let structure = candidates
            .iter()
            .map(|candidate| {
                format!(
                    "{}:{}",
                    candidate.profile.definition.surface.as_str(),
                    candidate.fingerprint
                )
            })
            .collect::<Vec<_>>()
            .join("|");
        let preference = {
            let state = self.state.lock().expect("wire resolver lock");
            (
                state
                    .operator_preferences
                    .get(&(provider_id.to_owned(), model_id.to_owned()))
                    .copied(),
                state
                    .metadata_hints
                    .get(&(provider_id.to_owned(), model_id.to_owned()))
                    .copied(),
            )
        };
        let fingerprint = fingerprint(&structure, preference);
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.clone(),
        };
        let config = self.config();
        let mut state = self.state.lock().expect("wire resolver lock");
        let entry = state.entries.entry(key.clone()).or_default();
        entry.rejected_at.retain(|_, rejected_at| {
            now.saturating_duration_since(*rejected_at) < config.rejection_ttl
        });
        let learned = entry
            .learned
            .as_ref()
            .filter(|learned| {
                now.saturating_duration_since(learned.observed_at) < config.learned_ttl
            })
            .map(|learned| learned.surface);
        if config.enabled {
            candidates.retain(|candidate| {
                entry
                    .rejected_at
                    .get(&candidate.surface())
                    .is_none_or(|rejected_at| {
                        now.saturating_duration_since(*rejected_at) >= config.rejection_ttl
                    })
            });
        }
        let fixed = preference
            .0
            .filter(|(_, fixed)| *fixed)
            .map(|(surface, _)| surface);
        let preferred = fixed
            .or(config.enabled.then_some(learned).flatten())
            .or_else(|| preference.0.map(|(surface, _)| surface))
            .or(config.enabled.then_some(preference.1).flatten());
        candidates.sort_by_key(|candidate| {
            let rank = if Some(candidate.surface()) == preferred {
                0
            } else {
                1
            };
            (rank, candidate.profile.priority, candidate.surface())
        });
        if let Some(fixed) = fixed {
            if candidates
                .first()
                .is_some_and(|candidate| candidate.surface() == fixed)
            {
                candidates.truncate(1);
            }
        }
        increment_metric(&mut state, "wire_selection", config.max_metric_labels);
        touch_lru(&mut state, key, config.cache_capacity);
        WireResolution {
            candidates,
            fingerprint,
        }
    }

    pub async fn begin_negotiation(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        now: Instant,
    ) -> NegotiationLease {
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.to_owned(),
        };
        let flight_key = (provider_id.to_owned(), model_id.to_owned());
        let throttled_by_interval = {
            let config = self.config();
            let state = self.state.lock().expect("wire resolver lock");
            if !config.enabled {
                true
            } else {
                !state.flights.contains_key(&flight_key)
                    && (state
                        .negotiation_delay_until
                        .get(provider_id)
                        .is_some_and(|until| *until > now)
                        || state.last_negotiation.get(provider_id).is_some_and(|last| {
                            now.saturating_duration_since(*last) < config.min_negotiation_interval
                        }))
            }
        };
        if throttled_by_interval {
            let flight = Arc::new(Flight {
                result: Mutex::new(Some(NegotiationResult::Rejected)),
                notify: tokio::sync::Notify::new(),
            });
            return NegotiationLease {
                resolver: self.clone(),
                key,
                flight_key,
                role: NegotiationRole::Throttled,
                permit: None,
                flight,
                finished: true,
            };
        }
        let (flight, role) = {
            let mut state = self.state.lock().expect("wire resolver lock");
            if let Some(flight) = state.flights.get(&flight_key) {
                (Arc::clone(flight), NegotiationRole::Follower)
            } else {
                let flight = Arc::new(Flight {
                    result: Mutex::new(None),
                    notify: tokio::sync::Notify::new(),
                });
                state
                    .flights
                    .insert(flight_key.clone(), Arc::clone(&flight));
                (flight, NegotiationRole::Leader)
            }
        };
        if role == NegotiationRole::Follower {
            return NegotiationLease {
                resolver: self.clone(),
                key,
                flight_key,
                role,
                permit: None,
                flight,
                finished: true,
            };
        }
        let config = self.config();
        let gate = {
            let mut gates = self.provider_gates.lock().expect("wire gates lock");
            gates
                .entry(provider_id.to_owned())
                .or_insert_with(|| Arc::new(ProviderGate::new(config.max_concurrent_per_provider)))
                .clone()
        };
        let permitted = gate.try_acquire();
        let role = if permitted.is_some() {
            NegotiationRole::Leader
        } else {
            self.cancel_leader(&flight_key, &flight);
            NegotiationRole::Throttled
        };
        if role == NegotiationRole::Throttled {
            return NegotiationLease {
                resolver: self.clone(),
                key,
                flight_key,
                role,
                permit: None,
                flight,
                finished: true,
            };
        }
        {
            let mut state = self.state.lock().expect("wire resolver lock");
            state.last_negotiation.insert(provider_id.to_owned(), now);
            trim_provider_state(&mut state, config.max_provider_state);
        }
        NegotiationLease {
            resolver: self.clone(),
            key,
            flight_key,
            role,
            permit: permitted,
            flight,
            finished: false,
        }
    }

    pub fn accept(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        surface: WireSurface,
        now: Instant,
    ) {
        self.record_learning(provider_id, model_id, fingerprint, surface, now);
    }

    pub fn reject(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        surface: WireSurface,
        now: Instant,
    ) {
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.to_owned(),
        };
        let config = self.config();
        if !config.enabled {
            return;
        }
        let mut state = self.state.lock().expect("wire resolver lock");
        state
            .entries
            .entry(key.clone())
            .or_default()
            .rejected_at
            .insert(surface, now);
        touch_lru(&mut state, key, config.cache_capacity);
        increment_metric(&mut state, "wire_rejection", config.max_metric_labels);
    }

    pub fn set_operator_preference(
        &self,
        provider_id: &str,
        model_id: &str,
        surface: WireSurface,
        fixed: bool,
    ) {
        let config = self.config();
        let mut state = self.state.lock().expect("wire resolver lock");
        state.operator_preferences.insert(
            (provider_id.to_owned(), model_id.to_owned()),
            (surface, fixed),
        );
        trim_preference_state(&mut state, config.max_provider_state);
    }

    pub fn set_metadata_hint(&self, provider_id: &str, model_id: &str, surface: WireSurface) {
        let config = self.config();
        let mut state = self.state.lock().expect("wire resolver lock");
        state
            .metadata_hints
            .insert((provider_id.to_owned(), model_id.to_owned()), surface);
        trim_preference_state(&mut state, config.max_provider_state);
    }

    /// C005 supplies rate-limit evidence; the resolver only stores the
    /// bounded provider-wide negotiation delay and never interprets HTTP.
    pub fn delay_provider_negotiation(&self, provider_id: &str, delay: Duration, now: Instant) {
        let bounded = delay.min(Duration::from_secs(1_800));
        let config = self.config();
        let mut state = self.state.lock().expect("wire resolver lock");
        state
            .negotiation_delay_until
            .insert(provider_id.to_owned(), now + bounded);
        trim_provider_state(&mut state, config.max_provider_state);
    }

    pub fn snapshot_size(&self) -> usize {
        self.state.lock().expect("wire resolver lock").entries.len()
    }

    /// Return whether two handles observe the same process-owned resolver
    /// state.  This is intentionally an identity check, not a comparison of
    /// bounded diagnostics.
    pub fn same_as(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.state, &other.state)
    }

    pub fn snapshot(&self) -> WireResolverSnapshot {
        let state = self.state.lock().expect("wire resolver lock");
        WireResolverSnapshot {
            entries: state.entries.len(),
            flights: state.flights.len(),
            provider_gates: self.provider_gates.lock().expect("wire gates lock").len(),
            last_negotiation: state.last_negotiation.len(),
            delayed_providers: state.negotiation_delay_until.len(),
            metric_labels: state.metrics.len(),
        }
    }

    fn record_learning(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        surface: WireSurface,
        now: Instant,
    ) {
        if !self.config().enabled {
            return;
        }
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.to_owned(),
        };
        let mut state = self.state.lock().expect("wire resolver lock");
        let config = self.config();
        state.entries.entry(key.clone()).or_default().learned = Some(Learned {
            surface,
            observed_at: now,
        });
        touch_lru(&mut state, key, config.cache_capacity);
    }

    fn finish_leader(
        &self,
        key: &CacheKey,
        flight: &Arc<Flight>,
        result: NegotiationResult,
        now: Instant,
    ) {
        *flight.result.lock().expect("flight lock") = Some(result.clone());
        flight.notify.notify_waiters();
        let mut state = self.state.lock().expect("wire resolver lock");
        let flight_key = (key.provider_id.clone(), key.model_id.clone());
        state.flights.remove(&flight_key);
        if self.config().enabled {
            if let NegotiationResult::Accepted(surface) = result {
                state.entries.entry(key.clone()).or_default().learned = Some(Learned {
                    surface,
                    observed_at: now,
                });
            }
        }
        touch_lru(&mut state, key.clone(), self.config().cache_capacity);
    }

    fn cancel_leader(&self, key: &FlightKey, flight: &Arc<Flight>) {
        *flight.result.lock().expect("flight lock") = Some(NegotiationResult::Rejected);
        flight.notify.notify_waiters();
        self.state
            .lock()
            .expect("wire resolver lock")
            .flights
            .remove(key);
    }

    fn cleanup_provider_state(&self, provider_id: &str) {
        let has_flight = self
            .state
            .lock()
            .expect("wire resolver lock")
            .flights
            .keys()
            .any(|key| key.0 == provider_id);
        if !has_flight {
            let mut gates = self.provider_gates.lock().expect("wire gates lock");
            if gates.get(provider_id).is_some_and(|gate| gate.is_idle()) {
                gates.remove(provider_id);
            }
        }
    }

    fn apply_config(&self, config: WireResolverConfig) {
        *self.config.lock().expect("wire resolver config lock") = config.clone();
        let gates = self.provider_gates.lock().expect("wire gates lock");
        for gate in gates.values() {
            gate.set_limit(config.max_concurrent_per_provider);
        }
    }

    fn enforce_bounds(&self) {
        let config = self.config();
        let mut state = self.state.lock().expect("wire resolver lock");
        trim_cache(&mut state, config.cache_capacity);
        trim_provider_state(&mut state, config.max_provider_state);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WireResolverSnapshot {
    pub entries: usize,
    pub flights: usize,
    pub provider_gates: usize,
    pub last_negotiation: usize,
    pub delayed_providers: usize,
    pub metric_labels: usize,
}

fn fingerprint(
    structure: &str,
    preference: (Option<(WireSurface, bool)>, Option<WireSurface>),
) -> String {
    let mut digest = Sha256::new();
    digest.update(structure.as_bytes());
    digest.update(format!("|preference={preference:?}").as_bytes());
    format!("{:x}", digest.finalize())
}

fn increment_metric(state: &mut ResolverState, label: &str, capacity: usize) {
    if !state.metrics.contains_key(label) && state.metrics.len() >= capacity.max(1) {
        if let Some(first) = state.metrics.keys().next().cloned() {
            state.metrics.remove(&first);
        }
    }
    *state.metrics.entry(label.to_owned()).or_default() += 1;
}

fn trim_provider_state(state: &mut ResolverState, capacity: usize) {
    let capacity = capacity.max(1);
    while state.last_negotiation.len() > capacity {
        if let Some(key) = state.last_negotiation.keys().next().cloned() {
            state.last_negotiation.remove(&key);
        }
    }
    while state.negotiation_delay_until.len() > capacity {
        if let Some(key) = state.negotiation_delay_until.keys().next().cloned() {
            state.negotiation_delay_until.remove(&key);
        }
    }
    trim_preference_state(state, capacity);
}

fn trim_preference_state(state: &mut ResolverState, capacity: usize) {
    let capacity = capacity.max(1);
    while state.operator_preferences.len() > capacity {
        if let Some(key) = state.operator_preferences.keys().next().cloned() {
            state.operator_preferences.remove(&key);
        }
    }
    while state.metadata_hints.len() > capacity {
        if let Some(key) = state.metadata_hints.keys().next().cloned() {
            state.metadata_hints.remove(&key);
        }
    }
}

fn touch_lru(state: &mut ResolverState, key: CacheKey, capacity: usize) {
    state.lru.retain(|existing| existing != &key);
    state.lru.push_back(key);
    while state.entries.len() > capacity.max(1) {
        let Some(oldest) = state.lru.pop_front() else {
            break;
        };
        state.entries.remove(&oldest);
    }
}

fn trim_cache(state: &mut ResolverState, capacity: usize) {
    while state.entries.len() > capacity.max(1) {
        let Some(oldest) = state.lru.pop_front() else {
            break;
        };
        state.entries.remove(&oldest);
    }
    state.lru.retain(|key| state.entries.contains_key(key));
}

fn duration_from_seconds(seconds: f64) -> Duration {
    if seconds.is_finite() && seconds > 0.0 {
        Duration::from_secs_f64(seconds)
    } else {
        Duration::ZERO
    }
}
