//! Live adoption of the bounded reason-specific backoff policy and the
//! unified health-reason vocabulary.
//!
//! Every test in this target is a regression guard for a behavior the
//! pre-milestone construction could not produce. Before the milestone the
//! live path wrote its backoff durations from inline constants, the failure
//! reasons were nine hand-written strings, and the escalation the policy
//! defines never happened outside the policy table.

use std::{
    collections::BTreeSet,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{FailureCategory, FailureObservation, FailureSource, RetryPolicy, classify},
    db::Account,
    health::{BackoffReason, HealthManager, compute_backoff_seconds, get_backoff_policy},
    quota::{AccountQuota, QuotaEstimator},
    routing::{
        EligibilityPolicy, FairnessMode, FairnessScope, LocalQuotaMode, RoutingRequestFacts,
        RoutingRouter,
    },
};

/// The reason vocabulary lives in one module. The writer serializes a reason
/// with `as_str` and the reader parses it back with `TryFrom<&str>`; the two
/// sets have to stay the same set.
const BACKOFF_SOURCE: &str = include_str!("../src/health/backoff.rs");
/// The failure classifier is where a hand-written reason string would come
/// back from.
const FAILURE_SOURCE: &str = include_str!("../src/coordinator/failure.rs");
/// The durable write owns the deadline clamp.
const REPOSITORY_SOURCE: &str = include_str!("../src/health/repository.rs");
/// The router owns the reconstruction of a reason from a stored string.
const ROUTER_SOURCE: &str = include_str!("../src/routing/router.rs");

/// The reason strings the pre-milestone classifier wrote, one per classified
/// failure. The values are a durable on-disk contract: a row written before
/// the change must stay readable and map to the same effect.
const PRE_CHANGE_REASON_STRINGS: [(&str, &str); 7] = [
    ("connection_failure", "connection_failure"),
    ("authentication_failed", "authentication_failed"),
    ("model_unavailable", "model_unavailable"),
    ("quota_exhausted", "quota_exhausted"),
    ("connect_timeout", "connect_timeout"),
    ("rate_limited", "rate_limited"),
    ("upstream_server_error", "upstream_server_error"),
];

fn account(id: i64, name: &str) -> Account {
    Account {
        id,
        name: name.into(),
        api_key_env: String::new(),
        enabled: true,
        weight: 1.0,
        provider_id: "provider-a".into(),
    }
}

/// A router whose health manager reads a clock the test controls, so a
/// cooldown is observed exactly rather than approximately.
fn fixture(clock: Arc<AtomicU64>) -> (RoutingRouter, HealthManager) {
    let mut config = Config::default();
    let mut provider = eggpool::config::ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider.invalid/v1".into(),
        protocols: vec!["openai".into()],
        auth: eggpool::config::ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    for name in ["account-a", "account-b"] {
        provider.accounts.push(eggpool::config::AccountConfig {
            name: name.into(),
            ..Default::default()
        });
    }
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[account(1, "account-a"), account(2, "account-b")],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    for name in ["account-a", "account-b"] {
        catalog.set_account_provider(name, "provider-a");
    }
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    for name in ["account-a", "account-b"] {
        catalog
            .update_from_account(name, "provider-a", &[model.clone()], true, true)
            .expect("catalog account");
    }
    let estimator = QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
    ]);
    let health_clock = clock.clone();
    let health =
        HealthManager::with_clock(move || health_clock.load(Ordering::SeqCst) as f64 / 1_000.0);
    for identity in registry.all() {
        health.register_account(identity.account_id, &identity.account_name);
    }
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator,
        Some(health.clone()),
        EligibilityPolicy {
            fairness_mode: FairnessMode::RoundRobin,
            fairness_scope: FairnessScope::ProviderModelProtocol,
            local_quota_mode: LocalQuotaMode::ScoreOnly,
            ..Default::default()
        },
    );
    (router, health)
}

fn facts(model: &str) -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new(model);
    facts.requested_protocol = Some("openai".into());
    facts.projected_tokens = 128;
    facts.now = 1_700_000_000;
    facts
}

/// The source section that starts at `start` and ends at `end`.
fn section<'a>(source: &'a str, start: &str, end: &str) -> &'a str {
    let (_, after_start) = source
        .split_once(start)
        .unwrap_or_else(|| panic!("the source no longer contains `{start}`"));
    let (body, _) = after_start
        .split_once(end)
        .unwrap_or_else(|| panic!("the source no longer contains `{end}`"));
    body
}

/// The double-quoted literals in a source section.
fn string_literals(source: &str) -> Vec<String> {
    let mut literals = Vec::new();
    let mut rest = source;
    while let Some((before, after)) = rest.split_once('"') {
        let Some((literal, tail)) = after.split_once('"') else {
            break;
        };
        literals.push(literal.to_owned());
        rest = tail;
        let _ = before;
    }
    literals
}

fn sorted(values: Vec<String>) -> Vec<String> {
    let mut values = values;
    values.sort();
    values.dedup();
    values
}

/// The values the durable writer serializes a reason with.
fn written_reason_values() -> Vec<String> {
    string_literals(section(
        BACKOFF_SOURCE,
        "pub const fn as_str(self)",
        "impl fmt::Display",
    ))
}

/// The values the durable reader accepts a stored reason from.
fn accepted_reason_values() -> Vec<String> {
    string_literals(section(
        BACKOFF_SOURCE,
        "impl TryFrom<&str> for BackoffReason",
        "/// Failure category used by health state",
    ))
}

#[test]
fn the_written_and_the_accepted_reason_vocabularies_are_the_same_set() {
    let written = sorted(written_reason_values());
    let accepted = sorted(accepted_reason_values());
    assert!(
        !written.is_empty(),
        "the writer must still serialize a reason vocabulary"
    );
    assert_eq!(
        written, accepted,
        "the reason values the writer serializes and the values the reader accepts must be the same set"
    );
}

#[test]
fn the_failure_classifier_carries_no_hand_written_reason_string() {
    assert!(
        !FAILURE_SOURCE.contains("backoff_reason = Some(\""),
        "a failure reason must come from the typed mapping, never from a string literal"
    );
}

#[test]
fn the_durable_deadline_clamp_takes_its_ceiling_from_the_policy() {
    assert!(
        !REPOSITORY_SOURCE.contains("1_800"),
        "the durable clamp must take its ceiling from the policy, never from a literal"
    );
    assert!(
        REPOSITORY_SOURCE.contains("get_backoff_policy"),
        "the durable clamp must read the cap from the reason's policy"
    );
}

#[test]
fn the_stored_reason_fallback_is_reported_at_an_observable_level() {
    let line = ROUTER_SOURCE
        .lines()
        .find(|line| line.contains("stored backoff reason"))
        .expect("the router reports an unrecognized stored reason");
    assert!(
        line.contains("tracing::warn!"),
        "a stored reason the reader cannot parse must be reported at a level an operator can observe: {line}"
    );
}

#[test]
fn every_failure_category_maps_to_a_reason_with_no_unmapped_arm() {
    let categories = [
        (FailureCategory::BadRequest, None, "unknown"),
        (
            FailureCategory::Authentication,
            None,
            "authentication_failed",
        ),
        (FailureCategory::Quota, None, "quota_exhausted"),
        (FailureCategory::RateLimit, None, "rate_limited"),
        (FailureCategory::Temporary, Some(408), "connect_timeout"),
        (
            FailureCategory::Temporary,
            Some(503),
            "upstream_server_error",
        ),
        (
            FailureCategory::TransientTransport,
            None,
            "connection_failure",
        ),
        (FailureCategory::ModelUnavailable, None, "model_unavailable"),
        (FailureCategory::WireRejected, None, "unknown"),
        (FailureCategory::Cancelled, None, "unknown"),
        (FailureCategory::Fatal, None, "unknown"),
    ];
    for (category, status, expected) in categories {
        let reason = category.backoff_reason(status);
        assert_eq!(
            reason.as_str(),
            expected,
            "{category:?} at {status:?} must map to {expected}"
        );
        assert!(
            BackoffReason::try_from(reason.as_str()).is_ok(),
            "{expected} must be a value the durable reader accepts"
        );
    }
}

#[test]
fn the_live_classification_writes_the_pre_change_reason_strings() {
    let policy = RetryPolicy::default();
    let mut transport = FailureObservation::response(1, 1, http::StatusCode::BAD_GATEWAY);
    transport.source = FailureSource::Transport;
    transport.status = None;

    let mut model_absent = FailureObservation::response(2, 1, http::StatusCode::NOT_FOUND);
    model_absent.signal = Some("model_absent".into());

    let mut quota = FailureObservation::response(3, 1, http::StatusCode::PAYMENT_REQUIRED);
    quota.signal = Some("quota_exhausted".into());

    let cases = [
        (transport, "connection_failure"),
        (
            FailureObservation::response(4, 1, http::StatusCode::UNAUTHORIZED),
            "authentication_failed",
        ),
        (model_absent, "model_unavailable"),
        (quota, "quota_exhausted"),
        (
            FailureObservation::response(5, 1, http::StatusCode::REQUEST_TIMEOUT),
            "connect_timeout",
        ),
        (
            FailureObservation::response(6, 1, http::StatusCode::TOO_MANY_REQUESTS),
            "rate_limited",
        ),
        (
            FailureObservation::response(7, 1, http::StatusCode::INTERNAL_SERVER_ERROR),
            "upstream_server_error",
        ),
    ];
    for (observation, expected) in cases {
        let effects = classify(&observation, policy);
        assert_eq!(
            effects.backoff_reason.as_deref(),
            Some(expected),
            "the classified reason must stay byte-identical to the pre-change value"
        );
    }
    // The pre-change literal set is the vocabulary the durable tables hold.
    for (literal, expected) in PRE_CHANGE_REASON_STRINGS {
        assert_eq!(literal, expected);
        assert!(
            BackoffReason::try_from(literal).is_ok(),
            "{literal} must stay readable by the durable reader"
        );
    }
}

#[test]
fn the_live_backoff_duration_comes_from_the_policy_base() {
    let policy = RetryPolicy::default();
    let mut transport = FailureObservation::response(1, 1, http::StatusCode::BAD_GATEWAY);
    transport.source = FailureSource::Transport;
    transport.status = None;

    let mut model_absent = FailureObservation::response(2, 1, http::StatusCode::NOT_FOUND);
    model_absent.signal = Some("model_absent".into());

    let cases = [
        (
            transport,
            BackoffReason::ConnectionFailure,
            Duration::from_secs(30),
        ),
        (
            FailureObservation::response(3, 1, http::StatusCode::REQUEST_TIMEOUT),
            BackoffReason::ConnectTimeout,
            Duration::from_secs(30),
        ),
        (
            FailureObservation::response(4, 1, http::StatusCode::INTERNAL_SERVER_ERROR),
            BackoffReason::UpstreamServerError,
            Duration::from_secs(20),
        ),
        (
            model_absent,
            BackoffReason::ModelUnavailable,
            Duration::from_secs(300),
        ),
    ];
    for (observation, reason, expected) in cases {
        let effects = classify(&observation, policy);
        assert_eq!(effects.backoff_reason.as_deref(), Some(reason.as_str()));
        assert_eq!(
            effects.backoff_until,
            Some(expected),
            "{} must take its duration from the policy base",
            reason.as_str()
        );
        let policy = get_backoff_policy(reason).expect("the reason has a policy");
        assert_eq!(policy.base_delay, expected.as_secs_f64());
    }
}

#[test]
fn the_policy_never_exceeds_its_own_cap() {
    for reason in [
        BackoffReason::QuotaExhausted,
        BackoffReason::RateLimited,
        BackoffReason::UpstreamServerError,
        BackoffReason::ConnectTimeout,
        BackoffReason::ConnectionFailure,
        BackoffReason::ModelUnavailable,
    ] {
        let policy = get_backoff_policy(reason).expect("the reason has a policy");
        for rung in 1..=32u32 {
            let seconds = compute_backoff_seconds(reason, rung, None, true)
                .expect("a timed reason always produces a duration");
            assert!(
                seconds <= policy.cap,
                "{reason:?} at rung {rung} produced {seconds}s above its {}s cap",
                policy.cap
            );
            assert!(seconds.is_finite() && seconds >= 0.0);
        }
    }
}

#[tokio::test(flavor = "current_thread")]
async fn a_repeated_failure_escalates_under_the_policy_and_never_exceeds_the_cap() {
    let clock = Arc::new(AtomicU64::new(0));
    let (router, health) = fixture(clock);
    let claim = router
        .select_and_claim(&facts("model-a"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");

    // The quota exhaustion the live classifier produces: the account effect is
    // applied and the provider sent no retry-after hint, so the policy owns
    // the duration and escalates it on every consecutive failure.
    for expected in [300.0, 600.0, 1200.0, 1800.0, 1800.0] {
        router
            .apply_failure_effects(
                &claim,
                true,
                false,
                "none",
                Some("quota_exhausted"),
                None,
                false,
            )
            .await;
        let snapshot = health
            .snapshot(claim.account_name())
            .expect("the account has health state");
        assert_eq!(
            snapshot.cooldown_until, expected,
            "a repeated failure must escalate under the policy"
        );
        assert!(
            snapshot.cooldown_until
                <= get_backoff_policy(BackoffReason::QuotaExhausted)
                    .expect("the reason has a policy")
                    .cap
        );
    }
}

#[tokio::test(flavor = "current_thread")]
async fn a_model_scoped_failure_escalates_on_its_own_model_counter() {
    let clock = Arc::new(AtomicU64::new(0));
    let (router, health) = fixture(clock);
    let claim = router
        .select_and_claim(&facts("model-a"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");

    for expected in [300.0, 600.0, 1200.0, 1800.0] {
        router
            .apply_failure_effects(
                &claim,
                false,
                true,
                "quarantine",
                Some("model_unavailable"),
                Some(Duration::from_secs(300)),
                false,
            )
            .await;
        let snapshot = health
            .snapshot(claim.account_name())
            .expect("the account has health state");
        assert_eq!(
            snapshot.disabled_models.get("model-a"),
            Some(&Some(expected)),
            "a model-scoped failure must escalate on the model counter"
        );
    }
}

#[tokio::test(flavor = "current_thread")]
async fn a_reason_without_a_policy_produces_no_timed_backoff() {
    let clock = Arc::new(AtomicU64::new(0));
    let (router, health) = fixture(clock);
    let claim = router
        .select_and_claim(&facts("model-a"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");

    // A stored reason the reader cannot parse is corrupt state, not a new
    // vocabulary: it normalizes to `Unknown`, which has no policy, so the
    // effect carries a circuit penalty instead of an unbounded duration.
    router
        .apply_failure_effects(
            &claim,
            true,
            false,
            "none",
            Some("not-a-reason"),
            None,
            true,
        )
        .await;
    let snapshot = health
        .snapshot(claim.account_name())
        .expect("the account has health state");
    assert_eq!(
        snapshot.cooldown_until, 0.0,
        "no policy means no timed backoff"
    );
    assert_eq!(
        snapshot.consecutive_failures, 1,
        "the circuit penalty applies"
    );
    assert_eq!(snapshot.last_failure_category, Some(BackoffReason::Unknown));
    assert!(get_backoff_policy(BackoffReason::Unknown).is_none());
}

#[tokio::test(flavor = "current_thread")]
async fn an_authentication_failure_keeps_its_no_timed_backoff_effect() {
    let clock = Arc::new(AtomicU64::new(0));
    let (router, health) = fixture(clock);
    let claim = router
        .select_and_claim(&facts("model-a"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");

    router
        .apply_failure_effects(
            &claim,
            true,
            false,
            "none",
            Some("authentication_failed"),
            None,
            true,
        )
        .await;
    let snapshot = health
        .snapshot(claim.account_name())
        .expect("the account has health state");
    assert_eq!(
        snapshot.cooldown_until, 0.0,
        "an authentication failure must not produce a timed backoff"
    );
    assert_eq!(snapshot.health_state, "authentication_failed");
    let policy =
        get_backoff_policy(BackoffReason::AuthenticationFailed).expect("the reason has a policy");
    assert_eq!(policy.base_delay, 0.0);
    assert_eq!(policy.cap, 0.0);
}
