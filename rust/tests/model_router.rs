use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

use eggpool::{
    config::{Config, ModelRouteConfig, ModelRouterConfig},
    model_router::{
        AffinityDecisionSource, AffinityError, AffinitySelection, ConversationPrefix,
        ConversationTextFragment, ModelRouterAffinity, ModelRouterRegistry, SessionSource,
        automatic_session_identity, compile_model_router, session_identity_from_header,
    },
};
use tokio::sync::Notify;

fn router_config(
    routes: impl IntoIterator<Item = (&'static str, &'static str, &'static str)>,
    default_model: &str,
) -> ModelRouterConfig {
    ModelRouterConfig {
        selector_model: "selector-model".into(),
        default_model: default_model.into(),
        routes: routes
            .into_iter()
            .map(|(label, model, description)| {
                (
                    label.into(),
                    ModelRouteConfig {
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

fn selection(
    router: &eggpool::model_router::CompiledModelRouter,
    route_id: &str,
) -> AffinitySelection {
    let route = router.route_for_id(route_id).expect("route");
    AffinitySelection {
        virtual_model: router.virtual_model.clone(),
        route_id: route.route_id.clone(),
        route_label: route.label.clone(),
        concrete_model: route.model.clone(),
        source: AffinityDecisionSource::Default,
    }
}

#[test]
fn compiled_router_matches_d001_golden_policy_and_fingerprint() {
    let config = router_config(
        [
            ("z-fast", "model-fast", " Fast\tpath "),
            ("a-default", "model-default", "Default\npath"),
        ],
        "model-default",
    );
    let router = compile_model_router("virtual-route", &config).expect("router compiles");

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
    let empty_a = ModelRouterRegistry::from_config(&BTreeMap::new()).expect("empty");
    let empty_b = ModelRouterRegistry::empty();
    assert!(empty_a.is_empty());
    assert_eq!(empty_a.len(), 0);
    assert_eq!(
        empty_a.virtual_model_ids().collect::<Vec<_>>(),
        Vec::<&str>::new()
    );

    let mut config = Config::default();
    config.model_routers.insert(
        "gpt-4".into(),
        router_config([("default", "gpt-4-real", "route")], "gpt-4-real"),
    );
    config.validate().expect("structurally valid");
    let registry = ModelRouterRegistry::from_config(&config.model_routers).expect("registry");
    assert!(registry.is_virtual("gpt-4"));
    assert!(registry.get("gpt-4").is_some());
    assert!(registry.get("gpt-4/provider-a").is_none());
    assert!(!empty_b.is_virtual("gpt-4"));
}

#[test]
fn validation_is_structural_and_uses_utf8_byte_bounds() {
    let mut config = Config::default();
    config.model_routers.insert(
        "future".into(),
        router_config([("default", "missing-yet", "route")], "missing-yet"),
    );
    config
        .validate()
        .expect("catalog availability is not config validation");

    let mut invalid = config.clone();
    invalid.model_routers.insert(
        "second".into(),
        router_config([("default", "future", "virtual target")], "future"),
    );
    assert!(invalid.validate().is_err());

    let mut byte_bound = Config::default();
    let mut byte_bound_router = router_config([("default", "model", "route")], "model");
    byte_bound_router
        .routes
        .get_mut("default")
        .expect("route")
        .description = "é".repeat(257);
    byte_bound
        .model_routers
        .insert("virtual".into(), byte_bound_router);
    assert!(byte_bound.validate().is_err());
}

#[test]
fn explicit_and_automatic_identities_are_hashed_and_surface_scoped() {
    let identity = session_identity_from_header(Some("fixture-session")).expect("identity");
    assert_eq!(identity.source, SessionSource::ExplicitSession);
    assert_eq!(
        identity.digest,
        [
            0xd6, 0x44, 0x09, 0x83, 0xc4, 0x54, 0xc2, 0xe5, 0x99, 0x9f, 0xdb, 0x66, 0xbb, 0xe9,
            0xcf, 0x5f, 0x89, 0xa8, 0xf5, 0x84, 0x7d, 0x6c, 0x95, 0x78, 0xef, 0x14, 0xaf, 0xd2,
            0x6e, 0xee, 0x12, 0x2c,
        ]
    );
    assert!(session_identity_from_header(None).is_none());
    assert!(session_identity_from_header(Some("bad\nvalue")).is_none());
    assert!(session_identity_from_header(Some(&"x".repeat(513))).is_none());
    assert!(!format!("{identity:?}").contains("fixture-session"));

    let prefix = ConversationPrefix::new(
        vec![ConversationTextFragment::new(
            "system",
            "stable instruction",
        )],
        Some("first question".into()),
    );
    let automatic = automatic_session_identity(&prefix, "chat_completions").expect("automatic");
    assert_eq!(automatic.source, SessionSource::AutomaticSession);
    assert!(automatic_session_identity(&prefix, "responses").is_none());
    assert!(!format!("{automatic:?}").contains("first question"));

    let long_system = "shared system prefix ".repeat(2_000);
    let first = automatic_session_identity(
        &ConversationPrefix::new(
            vec![ConversationTextFragment::new("system", long_system.clone())],
            Some("first request".into()),
        ),
        "chat_completions",
    );
    let second = automatic_session_identity(
        &ConversationPrefix::new(
            vec![ConversationTextFragment::new("system", long_system)],
            Some("second request".into()),
        ),
        "chat_completions",
    );
    assert_ne!(first, second);
}

#[tokio::test]
async fn affinity_is_ttl_lru_bounded_and_sticky_false_bypasses_cache() {
    let now = Arc::new(std::sync::Mutex::new(10.0));
    let clock_now = now.clone();
    let cache = ModelRouterAffinity::with_clock(2, move || *clock_now.lock().unwrap());
    let router = compile_model_router(
        "virtual",
        &router_config(
            [
                ("default", "model-default", "Default"),
                ("fast", "model-fast", "Fast"),
            ],
            "model-default",
        ),
    )
    .expect("router");
    let a = session_identity_from_header(Some("a")).unwrap();
    let b = session_identity_from_header(Some("b")).unwrap();
    let c = session_identity_from_header(Some("c")).unwrap();

    for (identity, route_id) in [(&a, "0"), (&b, "0")] {
        let result = cache
            .resolve(&router, identity, || {
                let chosen = selection(&router, route_id);
                async move { Ok(chosen) }
            })
            .await
            .expect("selection");
        assert!(!result.cache_hit);
    }
    let hit = cache
        .resolve(&router, &a, || async {
            unreachable!("hit must skip selector")
        })
        .await
        .expect("hit");
    assert!(hit.cache_hit);
    let evicted = cache
        .resolve(&router, &c, || {
            let chosen = selection(&router, "1");
            async move { Ok(chosen) }
        })
        .await
        .expect("selection");
    assert!(!evicted.cache_hit);
    assert_eq!(cache.stats().entry_count, 2);
    assert_eq!(cache.stats().evictions, 1);
    assert_eq!(cache.stats().hits, 1);
    assert_eq!(
        cache.get(&router, &a).expect("cache hit").concrete_model,
        "model-default"
    );

    *now.lock().unwrap() = 71.0;
    let expired = cache
        .resolve(&router, &a, || {
            let chosen = selection(&router, "0");
            async move { Ok(chosen) }
        })
        .await
        .expect("expired selection");
    assert!(!expired.cache_hit);
    assert!(cache.stats().expirations >= 1);

    let mut sticky_router = router.clone();
    sticky_router.sticky = false;
    let calls = Arc::new(AtomicUsize::new(0));
    for _ in 0..2 {
        let calls = calls.clone();
        let selector_router = sticky_router.clone();
        cache
            .resolve(&sticky_router, &a, move || {
                calls.fetch_add(1, Ordering::SeqCst);
                let chosen = selection(&selector_router, "0");
                async move { Ok(chosen) }
            })
            .await
            .expect("non-sticky selection");
    }
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn concurrent_misses_single_flight_and_cancelled_leader_recovers() {
    let cache = Arc::new(ModelRouterAffinity::new());
    let router = Arc::new(
        compile_model_router(
            "virtual",
            &router_config(
                [
                    ("default", "model-default", "Default"),
                    ("fast", "model-fast", "Fast"),
                ],
                "model-default",
            ),
        )
        .expect("router"),
    );
    let identity = session_identity_from_header(Some("same")).unwrap();
    let started = Arc::new(Notify::new());
    let release = Arc::new(Notify::new());
    let calls = Arc::new(AtomicUsize::new(0));
    let mut tasks = Vec::new();
    for _ in 0..8 {
        let cache = cache.clone();
        let router = router.clone();
        let started = started.clone();
        let release = release.clone();
        let calls = calls.clone();
        let identity = identity.clone();
        let selector_router = router.clone();
        tasks.push(tokio::spawn(async move {
            cache
                .resolve(&router, &identity, move || {
                    calls.fetch_add(1, Ordering::SeqCst);
                    started.notify_one();
                    async move {
                        release.notified().await;
                        Ok(selection(&selector_router, "1"))
                    }
                })
                .await
        }));
    }
    started.notified().await;
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(cache.stats().inflight_key_count, 1);
    release.notify_waiters();
    for task in tasks {
        assert_eq!(
            task.await.unwrap().unwrap().decision.concrete_model,
            "model-fast"
        );
    }
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(cache.stats().inflight_key_count, 0);
    assert_eq!(cache.stats().single_flight_joins, 7);

    let cancel_identity = session_identity_from_header(Some("cancel")).unwrap();
    let cancel_started = Arc::new(Notify::new());
    let cancel_cache = cache.clone();
    let cancel_router = router.clone();
    let cancel_identity_for_leader = cancel_identity.clone();
    let cancel_started_for_leader = cancel_started.clone();
    let leader = tokio::spawn(async move {
        cancel_cache
            .resolve(
                &cancel_router,
                &cancel_identity_for_leader,
                move || async move {
                    cancel_started_for_leader.notify_one();
                    std::future::pending::<Result<AffinitySelection, AffinityError>>().await
                },
            )
            .await
    });
    cancel_started.notified().await;
    let follower_cache = cache.clone();
    let follower_router = router.clone();
    let follower_identity = cancel_identity.clone();
    let follower_calls = calls.clone();
    let follower_selector_router = follower_router.clone();
    let follower = tokio::spawn(async move {
        follower_cache
            .resolve(&follower_router, &follower_identity, move || {
                follower_calls.fetch_add(1, Ordering::SeqCst);
                async move { Ok(selection(&follower_selector_router, "0")) }
            })
            .await
    });
    leader.abort();
    assert!(leader.await.unwrap_err().is_cancelled());
    assert_eq!(
        follower.await.unwrap().unwrap().decision.concrete_model,
        "model-default"
    );
    assert_eq!(cache.stats().inflight_key_count, 0);
}

#[tokio::test]
async fn invalid_selection_and_selector_errors_are_not_cached() {
    let cache = ModelRouterAffinity::new();
    let router = compile_model_router(
        "virtual",
        &router_config([("default", "model-default", "Default")], "model-default"),
    )
    .expect("router");
    let identity = session_identity_from_header(Some("error")).unwrap();

    let invalid = AffinitySelection {
        virtual_model: router.virtual_model.clone(),
        route_id: "9".into(),
        route_label: "missing".into(),
        concrete_model: "missing".into(),
        source: AffinityDecisionSource::Selector,
    };
    assert_eq!(
        cache
            .resolve(&router, &identity, || async move { Ok(invalid) })
            .await
            .unwrap_err(),
        AffinityError::InvalidSelection
    );
    assert_eq!(cache.stats().entry_count, 0);
    assert_eq!(cache.stats().inflight_key_count, 0);
    assert_eq!(
        cache
            .resolve(&router, &identity, || async {
                Err(AffinityError::SelectorFailed)
            })
            .await
            .unwrap_err(),
        AffinityError::SelectorFailed
    );
    assert_eq!(cache.stats().entry_count, 0);
    let recovered = cache
        .resolve(&router, &identity, || {
            let chosen = selection(&router, "0");
            async move { Ok(chosen) }
        })
        .await
        .expect("recovery after selector error");
    assert_eq!(recovered.decision.concrete_model, "model-default");
}
