use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use eggpool::{
    config::{ModelRouteConfig, ModelRouterConfig},
    model_router::{
        AffinityDecisionSource, AffinityError, AffinitySelection, ModelRouterAffinity,
        compile_model_router, session_identity_from_header,
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
