//! R012 corrective qualification for live wire authority and reload ownership.

use std::{sync::Arc, time::Duration};

use axum::body::Body;
use eggpool::{
    Config,
    coordinator::{
        NegotiationResult, NegotiationRole, WireCandidate, WireResolver, WireResolverConfig,
    },
    db::{Database, DatabaseConfig, MigrationRunner},
    reload::{ReloadResultCategory, ReloadService},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{AppState, build_router},
    wire::{ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireSurface},
};
use http::{Request, StatusCode};
use tempfile::TempDir;
use tokio::time::sleep;
use tower::ServiceExt;

struct Fixture {
    _directory: TempDir,
    database: Database,
    process: ProcessRuntime,
    manager: RuntimeManager,
    reload: ReloadService,
}

async fn fixture(config: Config) -> Fixture {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("runtime.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let process = ProcessRuntime::new_with_config(database.clone(), &config);
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config, "r012-initial-digest".to_owned(), 1)
            .await
            .expect("generation prepares");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation transfers"));
    let reload = process.reload_service(manager.clone());
    Fixture {
        _directory: directory,
        database,
        process,
        manager,
        reload,
    }
}

async fn close_fixture(fixture: Fixture) {
    let _ = fixture
        .process
        .task_supervisor()
        .shutdown_with_timeout(Duration::from_secs(1))
        .await;
    let _ = fixture
        .manager
        .close_for_shutdown(Duration::from_secs(1), false)
        .await;
    fixture.database.close().await.expect("database closes");
}

fn profile(surface: WireSurface, priority: u32) -> ConfiguredWireProfile {
    let (request_codec, response_codec, stream_codec) = match surface {
        WireSurface::OpenaiChatCompletions => (
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChatSse,
        ),
        WireSurface::AnthropicMessages => (
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessagesSse,
        ),
        _ => panic!("R012 test profile only needs chat/messages"),
    };
    ConfiguredWireProfile {
        definition: WireProfileDefinition {
            surface,
            request_codec,
            response_codec,
            stream_codec,
        },
        path_template: "/v1/{model}/dispatch".to_owned(),
        stream_path_template: None,
        priority,
    }
}

fn candidates(tag: &str) -> Vec<WireCandidate> {
    vec![
        WireCandidate::new(
            profile(WireSurface::OpenaiChatCompletions, 0),
            format!("{tag}-chat"),
        ),
        WireCandidate::new(
            profile(WireSurface::AnthropicMessages, 1),
            format!("{tag}-messages"),
        ),
    ]
}

fn policy(
    enabled: bool,
    capacity: usize,
    learned_ttl: Duration,
    rejection_ttl: Duration,
    interval: Duration,
    concurrency: usize,
) -> WireResolverConfig {
    WireResolverConfig {
        enabled,
        cache_capacity: capacity,
        learned_ttl,
        rejection_ttl,
        min_negotiation_interval: interval,
        max_concurrent_per_provider: concurrency,
        ..WireResolverConfig::default()
    }
}

#[tokio::test]
async fn startup_and_accepted_reload_install_non_default_wire_authority() {
    let mut config = Config::default();
    config.routing.wire_negotiation.enabled = false;
    config.routing.wire_negotiation.cache_max_entries = 7;
    config.routing.wire_negotiation.learned_preference_ttl_s = 11.0;
    config.routing.wire_negotiation.rejection_cooldown_s = 13.0;
    config.routing.wire_negotiation.min_negotiation_interval_s = 17.0;
    config.routing.wire_negotiation.max_concurrent_per_provider = 3;
    let fixture = fixture(config.clone()).await;
    let resolver = fixture.process.wire_profile_resolver();
    assert!(!resolver.config().enabled);
    assert_eq!(resolver.config().cache_capacity, 7);
    assert_eq!(resolver.config().learned_ttl, Duration::from_secs(11));
    assert_eq!(resolver.config().rejection_ttl, Duration::from_secs(13));
    assert_eq!(
        resolver.config().min_negotiation_interval,
        Duration::from_secs(17)
    );
    assert_eq!(resolver.config().max_concurrent_per_provider, 3);

    let mut next = config;
    next.routing.wire_negotiation.enabled = true;
    next.routing.wire_negotiation.cache_max_entries = 9;
    let result = fixture
        .reload
        .reload_bytes(
            "config.toml",
            toml::to_string(&next).unwrap().into_bytes(),
            None,
        )
        .await;
    assert_eq!(result.category, ReloadResultCategory::Applied);
    assert_eq!(fixture.manager.publication_epoch(), 1);
    assert!(resolver.config().enabled);
    assert_eq!(resolver.config().cache_capacity, 9);

    let app = build_router(AppState::from_runtime(
        next,
        fixture.database.clone(),
        Arc::new(fixture.manager.clone()),
    ));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    close_fixture(fixture).await;
}

#[tokio::test]
async fn policy_toggle_preserves_one_resolver_and_controls_negotiation() {
    let resolver = WireResolver::new(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    let now = std::time::Instant::now();
    let initial = resolver.resolve("p", "m", candidates("toggle"), now);
    resolver.accept(
        "p",
        "m",
        &initial.fingerprint,
        WireSurface::AnthropicMessages,
        now,
    );
    let leader = resolver
        .begin_negotiation("p", "m", &initial.fingerprint, now)
        .await;
    assert_eq!(leader.role(), NegotiationRole::Leader);
    leader.finish(
        NegotiationResult::Accepted(WireSurface::AnthropicMessages),
        now,
    );

    let mut disabled = resolver.stage_config(policy(
        false,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    disabled.commit();
    disabled.finalize();
    let disabled_lease = resolver
        .begin_negotiation("p", "m", &initial.fingerprint, now)
        .await;
    assert_eq!(disabled_lease.role(), NegotiationRole::Throttled);
    assert_eq!(resolver.snapshot().flights, 0);

    let mut enabled = resolver.stage_config(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    enabled.commit();
    enabled.finalize();
    assert!(resolver.same_as(&resolver.clone()));
    assert_eq!(
        resolver
            .resolve("p", "m", candidates("toggle"), now)
            .candidates[0]
            .surface(),
        WireSurface::AnthropicMessages
    );
}

#[test]
fn policy_capacity_and_time_changes_are_immediate_without_flushing_compatible_state() {
    let base = std::time::Instant::now();
    let resolver = WireResolver::new(policy(
        true,
        8,
        Duration::from_secs(10),
        Duration::from_secs(10),
        Duration::ZERO,
        1,
    ));
    for index in 0..4 {
        let resolved = resolver.resolve(
            "p",
            &format!("m{index}"),
            candidates(&format!("entry{index}")),
            base,
        );
        resolver.accept(
            "p",
            &format!("m{index}"),
            &resolved.fingerprint,
            WireSurface::AnthropicMessages,
            base,
        );
    }
    let mut smaller = resolver.stage_config(policy(
        true,
        2,
        Duration::from_secs(10),
        Duration::from_secs(10),
        Duration::from_secs(5),
        1,
    ));
    smaller.commit();
    smaller.finalize();
    assert_eq!(resolver.snapshot().entries, 2);
    assert_eq!(
        resolver.config().min_negotiation_interval,
        Duration::from_secs(5)
    );

    let resolved = resolver.resolve("ttl", "m", candidates("ttl"), base);
    resolver.accept(
        "ttl",
        "m",
        &resolved.fingerprint,
        WireSurface::AnthropicMessages,
        base,
    );
    assert_eq!(
        resolver
            .resolve(
                "ttl",
                "m",
                candidates("ttl"),
                base + Duration::from_secs(20)
            )
            .candidates[0]
            .surface(),
        WireSurface::OpenaiChatCompletions
    );
    let mut lengthened = resolver.stage_config(policy(
        true,
        2,
        Duration::from_secs(100),
        Duration::from_secs(10),
        Duration::ZERO,
        1,
    ));
    lengthened.commit();
    lengthened.finalize();
    resolver.accept(
        "ttl",
        "m",
        &resolved.fingerprint,
        WireSurface::AnthropicMessages,
        base,
    );
    assert_eq!(
        resolver
            .resolve(
                "ttl",
                "m",
                candidates("ttl"),
                base + Duration::from_secs(20)
            )
            .candidates[0]
            .surface(),
        WireSurface::AnthropicMessages
    );

    let rejected = resolver.resolve("reject", "m", candidates("reject"), base);
    resolver.accept(
        "reject",
        "m",
        &rejected.fingerprint,
        WireSurface::AnthropicMessages,
        base,
    );
    resolver.reject(
        "reject",
        "m",
        &rejected.fingerprint,
        WireSurface::AnthropicMessages,
        base,
    );
    assert_eq!(
        resolver
            .resolve(
                "reject",
                "m",
                candidates("reject"),
                base + Duration::from_secs(20),
            )
            .candidates[0]
            .surface(),
        WireSurface::AnthropicMessages
    );
}

#[tokio::test]
async fn minimum_interval_reconfiguration_changes_leader_eligibility() {
    let resolver = WireResolver::new(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    let base = std::time::Instant::now();
    let leader = resolver.begin_negotiation("p", "m", "f", base).await;
    leader.finish(NegotiationResult::Rejected, base);
    let mut throttled = resolver.stage_config(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::from_secs(10),
        1,
    ));
    throttled.commit();
    throttled.finalize();
    assert_eq!(
        resolver
            .begin_negotiation("p", "m2", "f2", base + Duration::from_secs(1))
            .await
            .role(),
        NegotiationRole::Throttled
    );
    let mut available = resolver.stage_config(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    available.commit();
    available.finalize();
    let leader = resolver
        .begin_negotiation("p", "m2", "f2", base + Duration::from_secs(1))
        .await;
    assert_eq!(leader.role(), NegotiationRole::Leader);
    leader.finish(NegotiationResult::Rejected, base + Duration::from_secs(1));
}

#[tokio::test]
async fn concurrency_limit_changes_converge_without_killing_old_leaders() {
    let resolver = WireResolver::new(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    let now = std::time::Instant::now();
    let first = resolver.begin_negotiation("p", "m1", "f1", now).await;
    assert_eq!(first.role(), NegotiationRole::Leader);
    assert_eq!(
        resolver
            .begin_negotiation("p", "m2", "f2", now)
            .await
            .role(),
        NegotiationRole::Throttled
    );
    let mut increased = resolver.stage_config(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        2,
    ));
    increased.commit();
    increased.finalize();
    let second = resolver.begin_negotiation("p", "m2", "f2", now).await;
    assert_eq!(second.role(), NegotiationRole::Leader);
    let mut reduced = resolver.stage_config(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    reduced.commit();
    reduced.finalize();
    assert_eq!(
        resolver
            .begin_negotiation("p", "m3", "f3", now)
            .await
            .role(),
        NegotiationRole::Throttled
    );
    first.finish(NegotiationResult::Rejected, now);
    second.finish(NegotiationResult::Rejected, now);
    assert_eq!(resolver.snapshot().flights, 0);
    let third = resolver.begin_negotiation("p", "m3", "f3", now).await;
    assert_eq!(third.role(), NegotiationRole::Leader);
    third.finish(NegotiationResult::Rejected, now);
    assert!(resolver.snapshot().provider_gates <= 1);
}

#[tokio::test]
async fn failed_policy_stage_restores_authority_and_compatible_state() {
    let resolver = WireResolver::new(policy(
        true,
        8,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    let now = std::time::Instant::now();
    let resolved = resolver.resolve("p", "m", candidates("rollback"), now);
    resolver.accept(
        "p",
        "m",
        &resolved.fingerprint,
        WireSurface::AnthropicMessages,
        now,
    );
    let mut stage = resolver.stage_config(policy(
        false,
        1,
        Duration::from_secs(1),
        Duration::from_secs(1),
        Duration::from_secs(1),
        1,
    ));
    stage.commit();
    stage.rollback();
    assert!(resolver.config().enabled);
    assert_eq!(resolver.config().cache_capacity, 8);
    assert_eq!(
        resolver
            .resolve("p", "m", candidates("rollback"), now)
            .candidates[0]
            .surface(),
        WireSurface::AnthropicMessages
    );
}

#[tokio::test]
async fn repeated_policy_reloads_keep_all_resolver_state_bounded() {
    let resolver = WireResolver::new(policy(
        true,
        4,
        Duration::from_secs(100),
        Duration::from_secs(100),
        Duration::ZERO,
        1,
    ));
    let base = std::time::Instant::now();
    for index in 0..100 {
        let capacity = if index % 2 == 0 { 2 } else { 4 };
        let mut stage = resolver.stage_config(policy(
            index % 3 != 0,
            capacity,
            Duration::from_secs(20 + index),
            Duration::from_secs(20 + index),
            Duration::ZERO,
            if index % 2 == 0 { 1 } else { 2 },
        ));
        stage.commit();
        stage.finalize();
        let resolved = resolver.resolve(
            "provider",
            &format!("model-{index}"),
            candidates(&format!("reload-{index}")),
            base,
        );
        resolver.accept(
            "provider",
            &format!("model-{index}"),
            &resolved.fingerprint,
            WireSurface::AnthropicMessages,
            base,
        );
        assert!(resolver.snapshot().entries <= capacity);
        assert!(resolver.snapshot().provider_gates <= 1);
    }
}

#[tokio::test]
async fn rejected_reload_categories_leave_wire_policy_unchanged() {
    let fixture = fixture(Config::default()).await;
    let resolver = fixture.process.wire_profile_resolver();
    let before = resolver.config();
    let mut restart = Config::default();
    restart.server.port += 1;
    assert_eq!(
        fixture
            .reload
            .reload_bytes(
                "config.toml",
                toml::to_string(&restart).unwrap().into_bytes(),
                None,
            )
            .await
            .category,
        ReloadResultCategory::RestartRequired
    );
    assert_eq!(resolver.config().enabled, before.enabled);
    assert_eq!(resolver.config().cache_capacity, before.cache_capacity);
    assert_eq!(
        fixture
            .reload
            .reload_bytes("config.toml", b"[routing.wire_negotiation\n".to_vec(), None,)
            .await
            .category,
        ReloadResultCategory::ValidationFailed
    );
    assert_eq!(resolver.config().learned_ttl, before.learned_ttl);
    close_fixture(fixture).await;
}

async fn wait_for_terminal(process: &ProcessRuntime, manager: &RuntimeManager) {
    for _ in 0..100 {
        if !process.diagnostics(manager).reload.in_progress {
            return;
        }
        sleep(Duration::from_millis(10)).await;
    }
    panic!("reload diagnostics remained in progress");
}

#[tokio::test]
async fn caller_cancellation_and_busy_do_not_corrupt_reload_diagnostics() {
    let fixture = fixture(Config::default()).await;
    let mut changed = Config::default();
    changed.server.max_request_body_bytes += 1;
    let blocker_database = fixture.database.clone();
    let blocker = tokio::spawn(async move {
        blocker_database
            .call(|_| {
                std::thread::sleep(Duration::from_millis(150));
                Ok::<_, tokio_rusqlite::rusqlite::Error>(())
            })
            .await
    });
    sleep(Duration::from_millis(10)).await;
    let reload = fixture.reload.clone();
    let caller = tokio::spawn(async move {
        reload
            .reload_bytes(
                "config.toml",
                toml::to_string(&changed).unwrap().into_bytes(),
                None,
            )
            .await
    });
    sleep(Duration::from_millis(20)).await;
    let busy = fixture
        .reload
        .reload_bytes(
            "config.toml",
            toml::to_string(&Config::default()).unwrap().into_bytes(),
            None,
        )
        .await;
    assert_eq!(busy.category, ReloadResultCategory::Busy);
    assert!(
        fixture
            .process
            .diagnostics(&fixture.manager)
            .reload
            .in_progress
    );
    for _ in 0..20 {
        let busy = fixture
            .reload
            .reload_bytes(
                "config.toml",
                toml::to_string(&Config::default()).unwrap().into_bytes(),
                None,
            )
            .await;
        assert_eq!(busy.category, ReloadResultCategory::Busy);
        assert!(
            fixture
                .process
                .diagnostics(&fixture.manager)
                .reload
                .in_progress
        );
    }
    caller.abort();
    let _ = blocker.await;
    wait_for_terminal(&fixture.process, &fixture.manager).await;
    let snapshot = fixture.process.diagnostics(&fixture.manager);
    assert!(!snapshot.reload.in_progress);
    assert_eq!(snapshot.counters.reload_attempts, 1);
    assert_eq!(snapshot.counters.reload_accepted, 1);
    close_fixture(fixture).await;
}

#[tokio::test]
async fn shutdown_during_retained_reload_clears_diagnostics() {
    let fixture = fixture(Config::default()).await;
    let blocker_database = fixture.database.clone();
    let blocker = tokio::spawn(async move {
        blocker_database
            .call(|_| {
                std::thread::sleep(Duration::from_millis(100));
                Ok::<_, tokio_rusqlite::rusqlite::Error>(())
            })
            .await
    });
    sleep(Duration::from_millis(10)).await;
    let reload = fixture.reload.clone();
    let caller = tokio::spawn(async move {
        let mut changed = Config::default();
        changed.server.max_request_body_bytes += 1;
        reload
            .reload_bytes(
                "config.toml",
                toml::to_string(&changed).unwrap().into_bytes(),
                None,
            )
            .await
    });
    sleep(Duration::from_millis(20)).await;
    fixture.manager.shutdown();
    caller.abort();
    let _ = blocker.await;
    wait_for_terminal(&fixture.process, &fixture.manager).await;
    assert!(
        !fixture
            .process
            .diagnostics(&fixture.manager)
            .reload
            .in_progress
    );
    close_fixture(fixture).await;
}
