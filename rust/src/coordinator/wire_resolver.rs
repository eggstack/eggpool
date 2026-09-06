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

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::wire::{ConfiguredWireProfile, WireSurface};

const DEFAULT_CACHE_CAPACITY: usize = 2_048;
const DEFAULT_LEARNED_TTL: Duration = Duration::from_secs(86_400);
const DEFAULT_REJECTION_TTL: Duration = Duration::from_secs(300);
const DEFAULT_NEGOTIATION_INTERVAL: Duration = Duration::from_secs(1);

#[derive(Debug, Clone)]
pub struct WireResolverConfig {
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
    expires_at: Instant,
}

#[derive(Debug, Default)]
struct CacheEntry {
    learned: Option<Learned>,
    rejected_until: BTreeMap<WireSurface, Instant>,
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
    permit: Option<OwnedSemaphorePermit>,
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
    config: WireResolverConfig,
    state: Arc<Mutex<ResolverState>>,
    provider_gates: Arc<Mutex<BTreeMap<String, Arc<Semaphore>>>>,
}

impl WireResolver {
    pub fn new(config: WireResolverConfig) -> Self {
        Self {
            config,
            state: Arc::new(Mutex::new(ResolverState::default())),
            provider_gates: Arc::new(Mutex::new(BTreeMap::new())),
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
        let mut state = self.state.lock().expect("wire resolver lock");
        let entry = state.entries.entry(key.clone()).or_default();
        entry.rejected_until.retain(|_, until| *until > now);
        let learned = entry
            .learned
            .as_ref()
            .filter(|learned| learned.expires_at > now)
            .map(|learned| learned.surface);
        candidates.retain(|candidate| {
            entry
                .rejected_until
                .get(&candidate.surface())
                .is_none_or(|until| *until <= now)
        });
        let fixed = preference
            .0
            .filter(|(_, fixed)| *fixed)
            .map(|(surface, _)| surface);
        let preferred = learned
            .or_else(|| preference.0.map(|(surface, _)| surface))
            .or(preference.1);
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
        increment_metric(&mut state, "wire_selection", self.config.max_metric_labels);
        touch_lru(&mut state, key, self.config.cache_capacity);
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
            let state = self.state.lock().expect("wire resolver lock");
            !state.flights.contains_key(&flight_key)
                && (state
                    .negotiation_delay_until
                    .get(provider_id)
                    .is_some_and(|until| *until > now)
                    || state.last_negotiation.get(provider_id).is_some_and(|last| {
                        now.saturating_duration_since(*last) < self.config.min_negotiation_interval
                    }))
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
        let gate = {
            let mut gates = self.provider_gates.lock().expect("wire gates lock");
            gates
                .entry(provider_id.to_owned())
                .or_insert_with(|| {
                    Arc::new(Semaphore::new(
                        self.config.max_concurrent_per_provider.max(1),
                    ))
                })
                .clone()
        };
        let permitted = gate.try_acquire_owned();
        let role = if permitted.is_ok() {
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
            trim_provider_state(&mut state, self.config.max_provider_state);
        }
        NegotiationLease {
            resolver: self.clone(),
            key,
            flight_key,
            role,
            permit: permitted.ok(),
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
        let mut state = self.state.lock().expect("wire resolver lock");
        state
            .entries
            .entry(key.clone())
            .or_default()
            .rejected_until
            .insert(surface, now + self.config.rejection_ttl);
        touch_lru(&mut state, key, self.config.cache_capacity);
        increment_metric(&mut state, "wire_rejection", self.config.max_metric_labels);
    }

    pub fn set_operator_preference(
        &self,
        provider_id: &str,
        model_id: &str,
        surface: WireSurface,
        fixed: bool,
    ) {
        let mut state = self.state.lock().expect("wire resolver lock");
        state.operator_preferences.insert(
            (provider_id.to_owned(), model_id.to_owned()),
            (surface, fixed),
        );
        trim_preference_state(&mut state, self.config.max_provider_state);
    }

    pub fn set_metadata_hint(&self, provider_id: &str, model_id: &str, surface: WireSurface) {
        let mut state = self.state.lock().expect("wire resolver lock");
        state
            .metadata_hints
            .insert((provider_id.to_owned(), model_id.to_owned()), surface);
        trim_preference_state(&mut state, self.config.max_provider_state);
    }

    /// C005 supplies rate-limit evidence; the resolver only stores the
    /// bounded provider-wide negotiation delay and never interprets HTTP.
    pub fn delay_provider_negotiation(&self, provider_id: &str, delay: Duration, now: Instant) {
        let bounded = delay.min(Duration::from_secs(1_800));
        let mut state = self.state.lock().expect("wire resolver lock");
        state
            .negotiation_delay_until
            .insert(provider_id.to_owned(), now + bounded);
        trim_provider_state(&mut state, self.config.max_provider_state);
    }

    pub fn snapshot_size(&self) -> usize {
        self.state.lock().expect("wire resolver lock").entries.len()
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
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.to_owned(),
        };
        let mut state = self.state.lock().expect("wire resolver lock");
        state.entries.entry(key.clone()).or_default().learned = Some(Learned {
            surface,
            expires_at: now + self.config.learned_ttl,
        });
        touch_lru(&mut state, key, self.config.cache_capacity);
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
        if let NegotiationResult::Accepted(surface) = result {
            state.entries.entry(key.clone()).or_default().learned = Some(Learned {
                surface,
                expires_at: now + self.config.learned_ttl,
            });
        }
        touch_lru(&mut state, key.clone(), self.config.cache_capacity);
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
            self.provider_gates
                .lock()
                .expect("wire gates lock")
                .remove(provider_id);
        }
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
