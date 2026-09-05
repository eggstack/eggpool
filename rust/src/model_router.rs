//! Immutable virtual-model router policy and bounded semantic affinity.
//!
//! This is the M5 state boundary only.  It deliberately has no request,
//! provider, catalog, or selector-client dependency.  A later milestone may
//! supply an async selection closure, but this module only validates that its
//! result belongs to the compiled route set.

use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    fmt,
    sync::{Arc, Mutex, OnceLock},
    time::Instant,
};

use sha2::{Digest, Sha256};
use tokio::sync::watch;

use crate::config::{ConfigError, ModelRouterConfig};

pub const AFFINITY_CACHE_MAX_ENTRIES: usize = 4_096;
pub const AFFINITY_SESSION_HEADER_MAX_BYTES: usize = 512;
pub const AUTOMATIC_PREFIX_MAX_BYTES: usize = 4_096;
pub const AUTOMATIC_FIRST_USER_MIN_BYTES: usize = 1_536;
pub const COMPILED_POLICY_MAX_BYTES: usize = 64 * 1024;
pub const SELECTOR_PROTOCOL_VERSION: &str = "model-router/v1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledModelRoute {
    pub route_id: String,
    pub label: String,
    pub model: String,
    pub description: String,
}

#[derive(Debug, Clone)]
pub struct CompiledModelRouter {
    pub virtual_model: String,
    pub selector_model: String,
    pub default_model: String,
    pub routes: Arc<[CompiledModelRoute]>,
    pub route_by_id: Arc<BTreeMap<String, CompiledModelRoute>>,
    pub config_fingerprint: String,
    pub static_policy: Arc<[u8]>,
    pub sticky: bool,
    pub affinity_ttl_s: f64,
    pub selector_timeout_s: f64,
    pub max_input_bytes: u64,
    pub repair_attempts: u8,
}

impl CompiledModelRouter {
    pub fn route_for_id(&self, route_id: &str) -> Option<&CompiledModelRoute> {
        self.route_by_id.get(route_id)
    }

    pub fn contains_model(&self, model: &str) -> bool {
        self.routes.iter().any(|route| route.model == model)
    }
}

#[derive(Debug, Clone)]
struct RegistryInner {
    routers: BTreeMap<String, Arc<CompiledModelRouter>>,
}

#[derive(Debug, Clone)]
pub struct ModelRouterRegistry {
    inner: Arc<RegistryInner>,
}

impl ModelRouterRegistry {
    pub fn empty() -> Self {
        static EMPTY: OnceLock<Arc<RegistryInner>> = OnceLock::new();
        Self {
            inner: EMPTY
                .get_or_init(|| {
                    Arc::new(RegistryInner {
                        routers: BTreeMap::new(),
                    })
                })
                .clone(),
        }
    }

    pub fn from_config(
        model_routers: &BTreeMap<String, ModelRouterConfig>,
    ) -> Result<Self, ConfigError> {
        validate_model_router_mapping(model_routers)?;
        if model_routers.is_empty() {
            return Ok(Self::empty());
        }
        let routers = model_routers
            .iter()
            .map(|(virtual_model, config)| {
                compile_model_router(virtual_model, config)
                    .map(|router| (virtual_model.clone(), Arc::new(router)))
            })
            .collect::<Result<BTreeMap<_, _>, _>>()?;
        Ok(Self {
            inner: Arc::new(RegistryInner { routers }),
        })
    }

    pub fn get(&self, virtual_model_id: &str) -> Option<Arc<CompiledModelRouter>> {
        self.inner.routers.get(virtual_model_id).cloned()
    }

    pub fn is_virtual(&self, model_id: &str) -> bool {
        self.inner.routers.contains_key(model_id)
    }

    pub fn virtual_model_ids(&self) -> impl ExactSizeIterator<Item = &str> {
        self.inner.routers.keys().map(String::as_str)
    }

    pub fn len(&self) -> usize {
        self.inner.routers.len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.routers.is_empty()
    }
}

pub fn validate_model_router_mapping(
    routers: &BTreeMap<String, ModelRouterConfig>,
) -> Result<(), ConfigError> {
    let virtual_ids = routers.keys().collect::<std::collections::BTreeSet<_>>();
    for (virtual_id, router) in routers {
        validate_virtual_model_id(virtual_id, "model router virtual ID")?;
        if router.routes.is_empty() {
            return Err(ConfigError::validation(
                "model router must declare at least one route",
            ));
        }
        validate_reference(&router.selector_model, "selector_model")?;
        validate_reference(&router.default_model, "default_model")?;
        if virtual_ids.contains(&router.selector_model) {
            return Err(ConfigError::validation(format!(
                "model router {virtual_id:?} selector_model cannot target virtual model {:?}",
                router.selector_model
            )));
        }
        for (label, route) in &router.routes {
            validate_route_label(label)?;
            validate_reference(&route.model, "route model")?;
            validate_description(&route.description)?;
            if virtual_ids.contains(&route.model) {
                return Err(ConfigError::validation(format!(
                    "model router {virtual_id:?} route {label:?} cannot target virtual model {:?}",
                    route.model
                )));
            }
        }
        if !router
            .routes
            .values()
            .any(|route| route.model == router.default_model)
        {
            return Err(ConfigError::validation(
                "model router default_model must exactly match at least one route model",
            ));
        }
        if !(1.0..=604_800.0).contains(&router.affinity_ttl_s) || !router.affinity_ttl_s.is_finite()
        {
            return Err(ConfigError::validation(
                "model router affinity_ttl_s must be between 1 and 604800 seconds",
            ));
        }
        if !(0.05..=30.0).contains(&router.selector_timeout_s)
            || !router.selector_timeout_s.is_finite()
        {
            return Err(ConfigError::validation(
                "model router selector_timeout_s must be between 0.05 and 30 seconds",
            ));
        }
        if !(128..=16_384).contains(&router.max_input_bytes) {
            return Err(ConfigError::validation(
                "model router max_input_bytes must be between 128 and 16384 bytes",
            ));
        }
        if router.repair_attempts > 1 {
            return Err(ConfigError::validation(
                "model router repair_attempts must be 0 or 1",
            ));
        }
    }
    Ok(())
}

fn utf8_len(value: &str) -> usize {
    value.len()
}

fn contains_control(value: &str, allow_ascii_whitespace: bool) -> bool {
    value.chars().any(|character| {
        character.is_control()
            && !(allow_ascii_whitespace
                && matches!(character, '\t' | '\n' | '\r' | '\u{0b}' | '\u{0c}'))
    })
}

fn validate_virtual_model_id(value: &str, field: &str) -> Result<(), ConfigError> {
    if value.trim().is_empty()
        || utf8_len(value) > 128
        || contains_control(value, false)
        || value.contains('/')
    {
        return Err(ConfigError::validation(format!(
            "{field} must be a non-empty control-free UTF-8 value of at most 128 bytes without '/'"
        )));
    }
    Ok(())
}

fn validate_reference(value: &str, field: &str) -> Result<(), ConfigError> {
    if value.trim().is_empty() || utf8_len(value) > 128 || contains_control(value, false) {
        return Err(ConfigError::validation(format!(
            "{field} must be a non-empty control-free UTF-8 value of at most 128 bytes"
        )));
    }
    Ok(())
}

fn validate_route_label(value: &str) -> Result<(), ConfigError> {
    if value.trim().is_empty() || utf8_len(value) > 128 || contains_control(value, false) {
        return Err(ConfigError::validation(
            "route label must be a non-empty control-free UTF-8 value of at most 128 bytes",
        ));
    }
    Ok(())
}

fn validate_description(value: &str) -> Result<(), ConfigError> {
    if value.trim().is_empty() || utf8_len(value) > 512 || contains_control(value, true) {
        return Err(ConfigError::validation(
            "route description must be a non-empty UTF-8 value of at most 512 bytes",
        ));
    }
    Ok(())
}

fn normalize_description(value: &str) -> String {
    value
        .trim()
        .split_ascii_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn python_float_repr(value: f64) -> String {
    // The accepted configuration range is finite and ordinary decimal input;
    // Rust's shortest round-trip Debug representation matches Python repr for
    // this contract's values, including the required trailing `.0`.
    format!("{value:?}")
}

fn length_delimited_hash(fields: impl IntoIterator<Item = String>) -> String {
    let mut digest = Sha256::new();
    for field in fields {
        let bytes = field.as_bytes();
        digest.update((bytes.len() as u64).to_be_bytes());
        digest.update(bytes);
    }
    format!("{:x}", digest.finalize())
}

pub fn compile_model_router(
    virtual_model: &str,
    router: &ModelRouterConfig,
) -> Result<CompiledModelRouter, ConfigError> {
    validate_virtual_model_id(virtual_model, "model router virtual ID")?;
    let mut routes = router.routes.iter().collect::<Vec<_>>();
    routes.sort_by_key(|(label, _)| *label);
    let routes = routes
        .into_iter()
        .enumerate()
        .map(|(index, (label, route))| CompiledModelRoute {
            route_id: index.to_string(),
            label: label.clone(),
            model: route.model.clone(),
            description: normalize_description(&route.description),
        })
        .collect::<Vec<_>>();
    let route_by_id = routes
        .iter()
        .map(|route| (route.route_id.clone(), route.clone()))
        .collect::<BTreeMap<_, _>>();
    let static_policy = format!(
        "{SELECTOR_PROTOCOL_VERSION}|choose id;reply id only|{}",
        routes
            .iter()
            .map(|route| format!("{}={}", route.route_id, route.description))
            .collect::<Vec<_>>()
            .join("|")
    )
    .into_bytes();
    if static_policy.len() > COMPILED_POLICY_MAX_BYTES {
        return Err(ConfigError::validation(format!(
            "compiled policy for model router {virtual_model:?} exceeds the {COMPILED_POLICY_MAX_BYTES}-byte limit"
        )));
    }
    let mut fingerprint = vec![
        SELECTOR_PROTOCOL_VERSION.to_owned(),
        virtual_model.to_owned(),
        router.selector_model.clone(),
        router.default_model.clone(),
    ];
    for route in &routes {
        fingerprint.extend([
            route.label.clone(),
            route.model.clone(),
            route.description.clone(),
        ]);
    }
    fingerprint.extend([
        if router.sticky { "True" } else { "False" }.to_owned(),
        python_float_repr(router.affinity_ttl_s),
        python_float_repr(router.selector_timeout_s),
        router.max_input_bytes.to_string(),
        router.repair_attempts.to_string(),
    ]);
    Ok(CompiledModelRouter {
        virtual_model: virtual_model.to_owned(),
        selector_model: router.selector_model.clone(),
        default_model: router.default_model.clone(),
        routes: Arc::from(routes),
        route_by_id: Arc::new(route_by_id),
        config_fingerprint: length_delimited_hash(fingerprint),
        static_policy: Arc::from(static_policy),
        sticky: router.sticky,
        affinity_ttl_s: router.affinity_ttl_s,
        selector_timeout_s: router.selector_timeout_s,
        max_input_bytes: router.max_input_bytes,
        repair_attempts: router.repair_attempts,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionSource {
    ExplicitSession,
    AutomaticSession,
}

#[derive(Clone, PartialEq, Eq)]
pub struct SessionIdentity {
    pub digest: [u8; 32],
    pub source: SessionSource,
}

impl fmt::Debug for SessionIdentity {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SessionIdentity")
            .field(
                "digest",
                &self
                    .digest
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>(),
            )
            .field("source", &self.source)
            .finish()
    }
}

pub fn session_identity_from_header(value: Option<&str>) -> Option<SessionIdentity> {
    let value = value?;
    if value.is_empty()
        || value.len() > AFFINITY_SESSION_HEADER_MAX_BYTES
        || value
            .chars()
            .any(|character| (character as u32) < 32 || character == '\u{7f}')
    {
        return None;
    }
    let digest = Sha256::digest(value.as_bytes());
    Some(SessionIdentity {
        digest: digest.into(),
        source: SessionSource::ExplicitSession,
    })
}

#[derive(Clone, PartialEq, Eq)]
pub struct ConversationTextFragment {
    pub role: String,
    pub text: String,
}

impl fmt::Debug for ConversationTextFragment {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ConversationTextFragment")
            .field("role", &self.role)
            .field("text_bytes", &self.text.len())
            .finish()
    }
}

impl ConversationTextFragment {
    pub fn new(role: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            role: role.into(),
            text: text.into(),
        }
    }
}

#[derive(Clone, Default, PartialEq, Eq)]
pub struct ConversationPrefix {
    pub system_developer: Vec<ConversationTextFragment>,
    pub first_user_text: Option<String>,
}

impl fmt::Debug for ConversationPrefix {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ConversationPrefix")
            .field("system_developer", &self.system_developer)
            .field(
                "first_user_text_bytes",
                &self.first_user_text.as_ref().map(String::len),
            )
            .finish()
    }
}

impl ConversationPrefix {
    pub fn new(
        system_developer: Vec<ConversationTextFragment>,
        first_user_text: Option<String>,
    ) -> Self {
        Self {
            system_developer,
            first_user_text,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AffinityIdentityInput {
    pub client_surface: String,
    pub explicit_session: Option<SessionIdentity>,
    pub conversation_prefix: Option<ConversationPrefix>,
}

impl AffinityIdentityInput {
    pub fn explicit(client_surface: impl Into<String>, identity: SessionIdentity) -> Self {
        Self {
            client_surface: client_surface.into(),
            explicit_session: Some(identity),
            conversation_prefix: None,
        }
    }

    pub fn automatic(client_surface: impl Into<String>, prefix: ConversationPrefix) -> Self {
        Self {
            client_surface: client_surface.into(),
            explicit_session: None,
            conversation_prefix: Some(prefix),
        }
    }

    pub fn session_identity(&self) -> Option<SessionIdentity> {
        self.explicit_session.clone().or_else(|| {
            self.conversation_prefix
                .as_ref()
                .and_then(|prefix| automatic_session_identity(prefix, &self.client_surface))
        })
    }
}

fn normalize_identity_text(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn bounded_utf8(value: &str, max_bytes: usize) -> Vec<u8> {
    if max_bytes == 0 {
        return Vec::new();
    }
    let bytes = value.as_bytes();
    if bytes.len() <= max_bytes {
        return bytes.to_vec();
    }
    let head_budget = max_bytes / 2;
    let tail_budget = max_bytes - head_budget;
    let mut head_end = head_budget;
    while head_end > 0 && !value.is_char_boundary(head_end) {
        head_end -= 1;
    }
    let mut tail_start = bytes.len() - tail_budget;
    while tail_start < bytes.len() && !value.is_char_boundary(tail_start) {
        tail_start += 1;
    }
    bytes[..head_end]
        .iter()
        .chain(bytes[tail_start..].iter())
        .copied()
        .collect()
}

fn bounded_identity_field(role: &str, text: &str, max_bytes: usize) -> Vec<u8> {
    let role_bytes = role.as_bytes();
    let framing_bytes = 2 + role_bytes.len() + 4;
    if max_bytes <= framing_bytes {
        return Vec::new();
    }
    let text_bytes = bounded_utf8(text, max_bytes - framing_bytes);
    if text_bytes.is_empty() {
        return Vec::new();
    }
    let mut field = Vec::with_capacity(framing_bytes + text_bytes.len());
    field.extend((role_bytes.len() as u16).to_be_bytes());
    field.extend(role_bytes);
    field.extend((text_bytes.len() as u32).to_be_bytes());
    field.extend(text_bytes);
    field
}

pub fn automatic_session_identity(
    prefix: &ConversationPrefix,
    client_surface: &str,
) -> Option<SessionIdentity> {
    if client_surface == "responses" {
        return None;
    }
    let user_text = normalize_identity_text(prefix.first_user_text.as_deref()?);
    if user_text.is_empty() {
        return None;
    }
    let mut digest = Sha256::new();
    digest.update(b"eggpool-route-affinity/v1");
    digest.update((client_surface.len() as u16).to_be_bytes());
    digest.update(client_surface.as_bytes());
    let user_role_overhead = 2 + 4 + 4;
    let reserved_user_bytes = AUTOMATIC_FIRST_USER_MIN_BYTES
        .min(AUTOMATIC_PREFIX_MAX_BYTES.saturating_sub(user_role_overhead));
    let mut remaining = AUTOMATIC_PREFIX_MAX_BYTES;
    let mut system_budget = AUTOMATIC_PREFIX_MAX_BYTES
        .saturating_sub(user_role_overhead)
        .saturating_sub(reserved_user_bytes);
    for fragment in &prefix.system_developer {
        if !matches!(fragment.role.as_str(), "system" | "developer") || system_budget == 0 {
            continue;
        }
        let text = normalize_identity_text(&fragment.text);
        let field = bounded_identity_field(&fragment.role, &text, system_budget);
        if field.is_empty() {
            continue;
        }
        remaining = remaining.saturating_sub(field.len());
        system_budget = system_budget.saturating_sub(field.len());
        digest.update(field);
    }
    let user_field = bounded_identity_field("user", &user_text, remaining);
    if !user_field.is_empty() {
        digest.update(user_field);
    }
    Some(SessionIdentity {
        digest: digest.finalize().into(),
        source: SessionSource::AutomaticSession,
    })
}

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
        if let Ok(mut state) = self.owner.lock() {
            if state
                .flights
                .get(&self.key)
                .is_some_and(|flight| Arc::ptr_eq(flight, &self.flight))
            {
                state.flights.remove(&self.key);
                self.flight.result.send_replace(Some(FlightResult::Aborted));
            }
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

    /// Read a still-valid decision without invoking selection.  The current
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
        let selector = Some(selector);
        let mut selector = selector;
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
