use std::{
    collections::{BTreeMap, BTreeSet},
    fmt,
    sync::{Arc, OnceLock},
};

use sha2::{Digest, Sha256};

pub const COMPILED_POLICY_MAX_BYTES: usize = 64 * 1024;
pub const SELECTOR_PROTOCOL_VERSION: &str = "model-router/v1";

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ModelRoutePolicy {
    pub model: String,
    pub description: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ModelRouterPolicy {
    pub selector_model: String,
    pub default_model: String,
    pub routes: BTreeMap<String, ModelRoutePolicy>,
    pub sticky: bool,
    pub affinity_ttl_s: f64,
    pub selector_timeout_s: f64,
    pub max_input_bytes: u64,
    pub repair_attempts: u8,
}

impl ModelRouterPolicy {
    pub fn default_values() -> Self {
        Self {
            selector_model: String::new(),
            default_model: String::new(),
            routes: BTreeMap::new(),
            sticky: true,
            affinity_ttl_s: 43_200.0,
            selector_timeout_s: 2.0,
            max_input_bytes: 2_048,
            repair_attempts: 1,
        }
    }
}

impl Default for ModelRouterPolicy {
    fn default() -> Self {
        Self::default_values()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelRoutingError {
    detail: String,
}

impl ModelRoutingError {
    fn validation(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }

    pub fn detail(&self) -> &str {
        &self.detail
    }
}

impl fmt::Display for ModelRoutingError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl std::error::Error for ModelRoutingError {}

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

    /// Resolve only an exact compiled route ID returned by a selector.
    pub fn resolve_route_id(
        &self,
        route_id: &str,
    ) -> Result<&CompiledModelRoute, ModelRoutingError> {
        self.route_for_id(route_id).ok_or_else(|| {
            ModelRoutingError::validation("model-router selection is not in the compiled route map")
        })
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

    pub fn from_policies(
        model_routers: &BTreeMap<String, ModelRouterPolicy>,
    ) -> Result<Self, ModelRoutingError> {
        validate_model_router_mapping(model_routers)?;
        if model_routers.is_empty() {
            return Ok(Self::empty());
        }
        let routers = model_routers
            .iter()
            .map(|(virtual_model, policy)| {
                compile_model_router(virtual_model, policy)
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
    routers: &BTreeMap<String, ModelRouterPolicy>,
) -> Result<(), ModelRoutingError> {
    let virtual_ids = routers.keys().collect::<BTreeSet<_>>();
    for (virtual_id, router) in routers {
        validate_virtual_model_id(virtual_id, "model router virtual ID")?;
        if router.routes.is_empty() {
            return Err(ModelRoutingError::validation(
                "model router must declare at least one route",
            ));
        }
        validate_reference(&router.selector_model, "selector_model")?;
        validate_reference(&router.default_model, "default_model")?;
        if virtual_ids.contains(&router.selector_model) {
            return Err(ModelRoutingError::validation(format!(
                "model router {virtual_id:?} selector_model cannot target virtual model {:?}",
                router.selector_model
            )));
        }
        for (label, route) in &router.routes {
            validate_route_label(label)?;
            validate_reference(&route.model, "route model")?;
            validate_description(&route.description)?;
            if virtual_ids.contains(&route.model) {
                return Err(ModelRoutingError::validation(format!(
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
            return Err(ModelRoutingError::validation(
                "model router default_model must exactly match at least one route model",
            ));
        }
        if !(1.0..=604_800.0).contains(&router.affinity_ttl_s) || !router.affinity_ttl_s.is_finite()
        {
            return Err(ModelRoutingError::validation(
                "model router affinity_ttl_s must be between 1 and 604800 seconds",
            ));
        }
        if !(0.05..=30.0).contains(&router.selector_timeout_s)
            || !router.selector_timeout_s.is_finite()
        {
            return Err(ModelRoutingError::validation(
                "model router selector_timeout_s must be between 0.05 and 30 seconds",
            ));
        }
        if !(128..=16_384).contains(&router.max_input_bytes) {
            return Err(ModelRoutingError::validation(
                "model router max_input_bytes must be between 128 and 16384 bytes",
            ));
        }
        if router.repair_attempts > 1 {
            return Err(ModelRoutingError::validation(
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

fn validate_virtual_model_id(value: &str, field: &str) -> Result<(), ModelRoutingError> {
    if value.trim().is_empty()
        || utf8_len(value) > 128
        || contains_control(value, false)
        || value.contains('/')
    {
        return Err(ModelRoutingError::validation(format!(
            "{field} must be a non-empty control-free UTF-8 value of at most 128 bytes without '/'"
        )));
    }
    Ok(())
}

fn validate_reference(value: &str, field: &str) -> Result<(), ModelRoutingError> {
    if value.trim().is_empty() || utf8_len(value) > 128 || contains_control(value, false) {
        return Err(ModelRoutingError::validation(format!(
            "{field} must be a non-empty control-free UTF-8 value of at most 128 bytes"
        )));
    }
    Ok(())
}

fn validate_route_label(value: &str) -> Result<(), ModelRoutingError> {
    if value.trim().is_empty() || utf8_len(value) > 128 || contains_control(value, false) {
        return Err(ModelRoutingError::validation(
            "route label must be a non-empty control-free UTF-8 value of at most 128 bytes",
        ));
    }
    Ok(())
}

fn validate_description(value: &str) -> Result<(), ModelRoutingError> {
    if value.trim().is_empty() || utf8_len(value) > 512 || contains_control(value, true) {
        return Err(ModelRoutingError::validation(
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
    router: &ModelRouterPolicy,
) -> Result<CompiledModelRouter, ModelRoutingError> {
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
        return Err(ModelRoutingError::validation(format!(
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

#[cfg(test)]
mod tests {
    use super::*;

    fn router_policy(
        routes: impl IntoIterator<Item = (&'static str, &'static str, &'static str)>,
        default_model: &str,
    ) -> ModelRouterPolicy {
        ModelRouterPolicy {
            selector_model: "selector-model".into(),
            default_model: default_model.into(),
            routes: routes
                .into_iter()
                .map(|(label, model, description)| {
                    (
                        label.into(),
                        ModelRoutePolicy {
                            model: model.into(),
                            description: description.into(),
                        },
                    )
                })
                .collect(),
            affinity_ttl_s: 60.0,
            max_input_bytes: 256,
            ..Default::default()
        }
    }

    #[test]
    fn compiled_router_matches_d001_golden_policy_and_fingerprint() {
        let policy = router_policy(
            [
                ("z-fast", "model-fast", " Fast\tpath "),
                ("a-default", "model-default", "Default\npath"),
            ],
            "model-default",
        );
        let router = compile_model_router("virtual-route", &policy).expect("router compiles");
        assert_eq!(
            router
                .routes
                .iter()
                .map(|route| (&route.route_id, &route.label, &route.description))
                .collect::<Vec<_>>(),
            vec![
                (
                    &"0".to_owned(),
                    &"a-default".to_owned(),
                    &"Default path".to_owned()
                ),
                (
                    &"1".to_owned(),
                    &"z-fast".to_owned(),
                    &"Fast path".to_owned()
                ),
            ]
        );
        assert_eq!(
            router.static_policy.as_ref(),
            b"model-router/v1|choose id;reply id only|0=Default path|1=Fast path"
        );
        assert_eq!(
            router.config_fingerprint,
            "70c26421aa06f8d476e158e3a9f477526d5dc80eccb8634bd5a16e12329c0f8a"
        );
    }

    #[test]
    fn registry_is_exact_and_empty_registry_is_shared() {
        let empty_a = ModelRouterRegistry::from_policies(&BTreeMap::new()).expect("empty");
        let empty_b = ModelRouterRegistry::empty();
        assert!(empty_a.is_empty());
        assert_eq!(empty_a.len(), 0);
        assert_eq!(
            empty_a.virtual_model_ids().collect::<Vec<_>>(),
            Vec::<&str>::new()
        );
        let mut policies = BTreeMap::new();
        policies.insert(
            "gpt-4".into(),
            router_policy([("default", "gpt-4-real", "route")], "gpt-4-real"),
        );
        let registry = ModelRouterRegistry::from_policies(&policies).expect("registry");
        assert!(registry.is_virtual("gpt-4"));
        assert!(registry.get("gpt-4").is_some());
        assert!(registry.get("gpt-4/provider-a").is_none());
        assert!(!empty_b.is_virtual("gpt-4"));
    }

    #[test]
    fn route_lookup_accepts_only_exact_compiled_ids() {
        let policy = router_policy([("default", "model-default", "Default")], "model-default");
        let router = compile_model_router("virtual", &policy).expect("router");
        assert_eq!(
            router.resolve_route_id("0").expect("route").model,
            "model-default"
        );
        assert!(router.resolve_route_id("default").is_err());
        assert!(router.resolve_route_id("1").is_err());
    }

    #[test]
    fn validation_is_structural_and_uses_utf8_byte_bounds() {
        let mut policies = BTreeMap::new();
        policies.insert(
            "future".into(),
            router_policy([("default", "missing-yet", "route")], "missing-yet"),
        );
        validate_model_router_mapping(&policies).expect("catalog availability is not validation");
        policies.insert(
            "second".into(),
            router_policy([("default", "future", "virtual target")], "future"),
        );
        assert!(validate_model_router_mapping(&policies).is_err());
        let mut byte_bound = ModelRouterPolicy {
            selector_model: "selector-model".into(),
            default_model: "model".into(),
            affinity_ttl_s: 60.0,
            max_input_bytes: 256,
            ..Default::default()
        };
        byte_bound.routes.insert(
            "default".into(),
            ModelRoutePolicy {
                model: "model".into(),
                description: "é".repeat(257),
            },
        );
        policies.clear();
        policies.insert("virtual".into(), byte_bound);
        assert!(validate_model_router_mapping(&policies).is_err());
    }
}
