//! Stable, read-only routing eligibility gates.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::{
    accounts::{AccountIdentity, AccountRegistry},
    catalog::{CapabilityStatus, ModelCatalogCache, ProviderModelIdentity},
    health::{HealthManager, ModelQuarantine},
    quota::{QuotaEstimator, QuotaFairScorer, RoutingScore, ScoringPolicy},
};

/// Request-independent facts supplied by the future canonical request layer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RoutingRequestFacts {
    pub canonical_model_id: String,
    pub provider_id: Option<String>,
    pub requested_protocol: Option<String>,
    pub client_protocol: Option<String>,
    pub request_surface: String,
    pub transcode_protocols: Vec<String>,
    pub projected_tokens: i64,
    pub catalog_stale_after_s: Option<i64>,
    pub thinking: Option<ThinkingRequirement>,
    pub capability_policy: BTreeMap<String, String>,
    pub now: i64,
}

impl RoutingRequestFacts {
    pub fn new(model_id: impl Into<String>) -> Self {
        Self {
            canonical_model_id: model_id.into(),
            provider_id: None,
            requested_protocol: None,
            client_protocol: None,
            request_surface: "chat_completions".into(),
            transcode_protocols: Vec::new(),
            projected_tokens: 0,
            catalog_stale_after_s: None,
            thinking: None,
            capability_policy: BTreeMap::new(),
            now: 0,
        }
    }

    pub fn from_model_id(
        model_id: &str,
        known_providers: &std::collections::BTreeSet<String>,
    ) -> Self {
        let (canonical_model_id, provider_id) =
            ModelCatalogCache::parse_model_provider(model_id, known_providers);
        let mut facts = Self::new(canonical_model_id);
        facts.provider_id = provider_id;
        facts
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ThinkingRequirement {
    pub requested: bool,
    pub requested_toggle: Option<bool>,
    pub effort: Option<String>,
    pub budget_tokens: Option<u64>,
    pub explicit_disable: bool,
}

impl ThinkingRequirement {
    pub fn enabled() -> Self {
        Self {
            requested: true,
            requested_toggle: None,
            effort: None,
            budget_tokens: None,
            explicit_disable: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RoutingExclusion {
    pub account_name: String,
    pub reason_code: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RoutingCandidate {
    pub account_name: String,
    pub provider_id: String,
    pub canonical_model_id: String,
    pub upstream_model_id: String,
    pub protocol: Option<String>,
    pub priority: u32,
    pub requires_transcode: bool,
    pub score: RoutingScore,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RoutingPlan {
    pub requested_model_id: String,
    pub requested_provider_id: Option<String>,
    pub requested_protocol: Option<String>,
    pub request_surface: String,
    pub eligible_account_names: Vec<String>,
    pub candidates: Vec<RoutingCandidate>,
    pub exclusions: Vec<RoutingExclusion>,
    pub fairness: Option<crate::routing::fairness::FairnessDecision>,
    pub catalog_version: usize,
    pub health_version: usize,
}

#[derive(Debug, Clone)]
pub struct EligibilityPolicy {
    pub local_quota_mode: LocalQuotaMode,
    pub scorer: ScoringPolicy,
    pub fairness_mode: FairnessMode,
    pub fairness_epsilon: Option<f64>,
    pub fairness_scope: FairnessScope,
    pub capability_policy: BTreeMap<String, String>,
}

impl Default for EligibilityPolicy {
    fn default() -> Self {
        Self {
            local_quota_mode: LocalQuotaMode::ScoreOnly,
            scorer: ScoringPolicy::default(),
            fairness_mode: FairnessMode::RoundRobin,
            fairness_epsilon: None,
            fairness_scope: FairnessScope::ProviderModelProtocol,
            capability_policy: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LocalQuotaMode {
    ScoreOnly,
    HardCap,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FairnessMode {
    Off,
    RoundRobin,
    Random,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FairnessScope {
    ProviderModelProtocol,
    ProviderModel,
    PriorityModelProtocol,
}

impl EligibilityPolicy {
    pub fn from_config(config: &crate::Config) -> Self {
        let mode = match config.routing.local_quota_mode.as_str() {
            "hard_cap" => LocalQuotaMode::HardCap,
            _ => LocalQuotaMode::ScoreOnly,
        };
        let fairness_mode = match config.routing.fairness_mode.as_str() {
            "off" => FairnessMode::Off,
            "random" => FairnessMode::Random,
            _ => FairnessMode::RoundRobin,
        };
        let fairness_scope = match config.routing.fairness_scope.as_str() {
            "provider_model" => FairnessScope::ProviderModel,
            "priority_model_protocol" => FairnessScope::PriorityModelProtocol,
            _ => FairnessScope::ProviderModelProtocol,
        };
        let mut capability_policy = BTreeMap::new();
        capability_policy.insert(
            "unsupported_thinking".into(),
            config
                .transcoder
                .capability_policy
                .unsupported_thinking
                .clone(),
        );
        capability_policy.insert(
            "unknown_thinking".into(),
            config.transcoder.capability_policy.unknown_thinking.clone(),
        );
        capability_policy.insert(
            "mixed_thinking".into(),
            config
                .transcoder
                .capability_policy
                .mixed_collapsed_thinking
                .clone(),
        );
        capability_policy.insert("unsupported_control".into(), "reject".into());
        capability_policy.insert("unknown_control".into(), "reject".into());
        Self {
            local_quota_mode: mode,
            scorer: ScoringPolicy {
                near_tie_epsilon: config.routing.near_tie_epsilon,
                prefer_native: config.transcoder.prefer_native,
                ..ScoringPolicy::default()
            },
            fairness_mode,
            fairness_epsilon: config.routing.fairness_epsilon,
            fairness_scope,
            capability_policy,
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub fn build_eligible_candidates(
    registry: &AccountRegistry,
    catalog: &ModelCatalogCache,
    estimator: &QuotaEstimator,
    health: Option<&HealthManager>,
    quarantine: Option<&ModelQuarantine>,
    facts: &RoutingRequestFacts,
    policy: EligibilityPolicy,
    active_requests: &BTreeMap<String, i64>,
) -> (Vec<RoutingCandidate>, Vec<RoutingExclusion>) {
    let mut eligible = Vec::new();
    let mut exclusions = Vec::new();
    for identity in registry.all() {
        let Some(candidate) = candidate_for_account(
            identity,
            catalog,
            estimator,
            health,
            quarantine,
            facts,
            policy.local_quota_mode,
            &if facts.capability_policy.is_empty() {
                policy.capability_policy.clone()
            } else {
                facts.capability_policy.clone()
            },
            &mut exclusions,
        ) else {
            continue;
        };
        eligible.push(candidate);
    }
    let names: Vec<String> = eligible
        .iter()
        .map(|item| item.account_name.clone())
        .collect();
    let active: BTreeMap<String, i64> = names
        .iter()
        .map(|name| (name.clone(), *active_requests.get(name).unwrap_or(&0)))
        .collect();
    let projected: BTreeMap<String, i64> = names
        .iter()
        .map(|name| (name.clone(), facts.projected_tokens.max(0)))
        .collect();
    let penalties = BTreeMap::new();
    let scorer = QuotaFairScorer::new(policy.scorer);
    let scores = scorer.score_accounts(estimator, &names, &active, &projected, &penalties);
    let by_name: BTreeMap<String, RoutingCandidate> = eligible
        .into_iter()
        .map(|candidate| (candidate.account_name.clone(), candidate))
        .collect();
    let scored = scores
        .into_iter()
        .filter_map(|score| {
            by_name
                .get(&score.account_name)
                .cloned()
                .map(|mut candidate| {
                    candidate.score = score;
                    candidate
                })
        })
        .collect::<Vec<_>>();
    let mut valid = Vec::with_capacity(scored.len());
    for candidate in scored {
        if candidate.score.is_eligible && candidate.score.final_score().is_finite() {
            valid.push(candidate);
        } else {
            exclusions.push(RoutingExclusion {
                account_name: candidate.account_name,
                reason_code: "malformed_score".into(),
            });
        }
    }
    let mut scored = valid;
    for candidate in &mut scored {
        candidate.score.tier = candidate.priority;
        candidate.score.requires_transcode = candidate.requires_transcode;
    }
    scored.sort_by(|left, right| {
        let native_order = if policy.scorer.prefer_native {
            left.requires_transcode.cmp(&right.requires_transcode)
        } else {
            std::cmp::Ordering::Equal
        };
        right
            .priority
            .cmp(&left.priority)
            .then_with(|| {
                left.score
                    .final_score()
                    .total_cmp(&right.score.final_score())
            })
            .then_with(|| native_order)
            .then_with(|| left.account_name.cmp(&right.account_name))
    });
    (scored, exclusions)
}

#[allow(clippy::too_many_arguments)]
fn candidate_for_account(
    identity: &AccountIdentity,
    catalog: &ModelCatalogCache,
    estimator: &QuotaEstimator,
    health: Option<&HealthManager>,
    quarantine: Option<&ModelQuarantine>,
    facts: &RoutingRequestFacts,
    quota_mode: LocalQuotaMode,
    capability_policy: &BTreeMap<String, String>,
    exclusions: &mut Vec<RoutingExclusion>,
) -> Option<RoutingCandidate> {
    let mut exclude = |reason: &str| {
        exclusions.push(RoutingExclusion {
            account_name: identity.account_name.clone(),
            reason_code: reason.into(),
        });
    };
    if !identity.enabled {
        exclude("disabled");
        return None;
    }
    if !identity.has_usable_credentials {
        exclude("auth_failed");
        return None;
    }
    if let Some(requested_provider) = &facts.provider_id {
        if identity.provider_id != *requested_provider {
            exclude(if identity.provider_id.is_empty() {
                "no_provider"
            } else {
                "wrong_provider"
            });
            return None;
        }
    }
    if facts.request_surface != "chat_completions"
        && !identity
            .supported_request_surfaces
            .iter()
            .any(|surface| surface.as_str() == facts.request_surface)
    {
        exclude("no_surface");
        return None;
    }
    if let Some(requested) = &facts.requested_protocol {
        let native = identity
            .supported_protocols
            .iter()
            .any(|protocol| protocol == requested);
        let transcodable = facts.transcode_protocols.iter().any(|protocol| {
            identity
                .supported_protocols
                .iter()
                .any(|supported| supported == protocol)
        });
        if !native && !transcodable {
            exclude("no_protocol");
            return None;
        }
    }
    if let Some(health) = health {
        if !health.is_model_healthy_read_only(&identity.account_name, &facts.canonical_model_id) {
            let reason =
                health
                    .snapshot(&identity.account_name)
                    .map_or("circuit_open", |snapshot| {
                        match snapshot.health_state.as_str() {
                            "authentication_failed" => "auth_failed",
                            "quota_exhausted" => "quota_exhausted",
                            "cooldown" => "cooldown",
                            "rate_limited" => "rate_limited",
                            _ => "circuit_open",
                        }
                    });
            exclude(reason);
            return None;
        }
    }
    let provider_model =
        catalog.get_provider_model(&facts.canonical_model_id, &identity.provider_id);
    if let Some(quarantine) = quarantine {
        let upstream_model_id = provider_model.map(|model| model.model_id.as_str());
        let upstream_protocol = provider_model
            .and_then(|model| model.protocol.as_deref())
            .or(facts.requested_protocol.as_deref())
            .unwrap_or("openai");
        let exact_upstream = upstream_model_id.is_some_and(|upstream_model_id| {
            quarantine.is_model_quarantined_for(
                &identity.provider_id,
                &identity.account_name,
                &facts.canonical_model_id,
                Some(upstream_model_id),
                upstream_protocol,
                facts.now as f64,
            )
        });
        // Before M7 selects and freezes a concrete upstream model, durable
        // catalog/quarantine state may intentionally use a NULL upstream
        // identity.  Honor that canonical-only key as well; otherwise a
        // pre-dispatch routing check could bypass an exact persisted
        // provider/account/model/protocol quarantine.
        let canonical_only = quarantine.is_model_quarantined_for(
            &identity.provider_id,
            &identity.account_name,
            &facts.canonical_model_id,
            None,
            upstream_protocol,
            facts.now as f64,
        );
        if exact_upstream || canonical_only {
            exclude("model_quarantined");
            return None;
        }
    }
    if !catalog.account_supports_model(&identity.account_name, &facts.canonical_model_id) {
        exclude("no_model");
        return None;
    }
    if let Some(ttl) = facts.catalog_stale_after_s {
        if !catalog.account_model_is_fresh(&identity.account_name, ttl, facts.now) {
            exclude("model_stale");
            return None;
        }
    }
    if let Some(requirement) = &facts.thinking {
        if requirement.requested && !requirement.explicit_disable {
            if let Some(entry) = provider_model {
                if let Some(reason) = thinking_exclusion(entry, requirement, capability_policy) {
                    exclude(reason);
                    return None;
                }
            } else {
                exclude("thinking_unknown");
                return None;
            }
        }
    }
    if quota_mode == LocalQuotaMode::HardCap
        && estimator
            .get_account_quota(&identity.account_name)
            .is_some_and(|mut quota| !quota.is_within_limits(facts.now as f64))
    {
        exclude("quota_exhausted");
        return None;
    }
    let resolved_protocol = provider_model.and_then(|model| model.protocol.clone());
    let requires_transcode = facts.requested_protocol.as_ref().is_some_and(|requested| {
        resolved_protocol
            .as_ref()
            .is_some_and(|resolved| resolved != requested)
    });
    if requires_transcode
        && facts
            .transcode_protocols
            .iter()
            .all(|protocol| Some(protocol) != resolved_protocol.as_ref())
    {
        exclude("protocol_mismatch");
        return None;
    }
    Some(RoutingCandidate {
        account_name: identity.account_name.clone(),
        provider_id: identity.provider_id.clone(),
        canonical_model_id: facts.canonical_model_id.clone(),
        upstream_model_id: facts.canonical_model_id.clone(),
        protocol: resolved_protocol.or_else(|| facts.requested_protocol.clone()),
        priority: identity.routing_priority,
        requires_transcode,
        score: RoutingScore {
            account_name: identity.account_name.clone(),
            quota_score: 0.0,
            weight: identity.weight,
            is_eligible: true,
            inflight_penalty: 0.0,
            health_penalty: 0.0,
            reserved_microdollars: 0,
            reserved_requests: 0,
            reserved_tokens: 0,
            cost_5h_microdollars: 0,
            cost_7d_microdollars: 0,
            cost_30d_microdollars: 0,
            request_count_5h: 0,
            request_count_7d: 0,
            request_count_30d: 0,
            token_count_5h: 0,
            token_count_7d: 0,
            token_count_30d: 0,
            capacity_5h_microdollars: 0,
            capacity_7d_microdollars: 0,
            capacity_30d_microdollars: 0,
            capacity_5h_requests: 0,
            capacity_7d_requests: 0,
            capacity_30d_requests: 0,
            capacity_5h_tokens: 0,
            capacity_7d_tokens: 0,
            capacity_30d_tokens: 0,
            active_request_count: 0,
            tier: identity.routing_priority,
            requires_transcode,
        },
    })
}

fn thinking_exclusion(
    entry: &ProviderModelIdentity,
    requirement: &ThinkingRequirement,
    policy: &BTreeMap<String, String>,
) -> Option<&'static str> {
    let unsupported_rejects = policy
        .get("unsupported_thinking")
        .is_none_or(|action| action == "reject");
    let unknown_rejects = policy
        .get("unknown_thinking")
        .is_none_or(|action| action == "reject");
    let capability = &entry.capabilities.thinking;
    if requirement.requested && !requirement.explicit_disable {
        match capability.status {
            CapabilityStatus::Unsupported if unsupported_rejects => {
                return Some("thinking_unsupported");
            }
            CapabilityStatus::Unknown if unknown_rejects => return Some("thinking_unknown"),
            CapabilityStatus::Conflicting => return Some("thinking_conflicting"),
            CapabilityStatus::Mixed
                if policy
                    .get("mixed_thinking")
                    .is_none_or(|action| action == "reject") =>
            {
                return Some("thinking_conflicting");
            }
            _ => {}
        }
    }
    let control = |status: CapabilityStatus, dimension: &'static str| -> Option<&'static str> {
        match status {
            CapabilityStatus::Unsupported
                if policy
                    .get("unsupported_control")
                    .is_none_or(|action| action == "reject") =>
            {
                Some(match dimension {
                    "toggle" => "thinking_toggle_unsupported",
                    "effort" => "thinking_effort_unsupported",
                    _ => "thinking_budget_unsupported",
                })
            }
            CapabilityStatus::Unknown if unknown_rejects => Some(match dimension {
                "toggle" => "thinking_toggle_unknown",
                "effort" => "thinking_effort_unknown",
                _ => "thinking_budget_unknown",
            }),
            CapabilityStatus::Conflicting => Some("thinking_conflicting"),
            _ => None,
        }
    };
    if requirement.requested_toggle.is_some() {
        if let Some(reason) = control(capability.toggle, "toggle") {
            return Some(reason);
        }
    }
    if let Some(effort) = &requirement.effort {
        if let Some(reason) = control(capability.effort, "effort") {
            return Some(reason);
        }
        if capability.effort == CapabilityStatus::Supported
            && !capability.supported_efforts.is_empty()
            && !capability.supported_efforts.iter().any(|value| {
                value.eq_ignore_ascii_case(effort)
                    || (value.eq_ignore_ascii_case("med") && effort.eq_ignore_ascii_case("medium"))
            })
        {
            return Some("thinking_effort_unsupported");
        }
    }
    if let Some(budget) = requirement.budget_tokens {
        if let Some(reason) = control(capability.budget, "budget") {
            return Some(reason);
        }
        if capability.budget == CapabilityStatus::Supported
            && (capability
                .budget_tokens_min
                .is_some_and(|minimum| budget < minimum)
                || capability
                    .budget_tokens_max
                    .is_some_and(|maximum| budget > maximum))
        {
            return Some("thinking_budget_unsupported");
        }
    }
    None
}
