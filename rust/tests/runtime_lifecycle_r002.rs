use std::{sync::Arc, time::Instant};

use eggpool::{
    Config,
    config::{ModelRouterConfig, ProviderAuthConfig, ProviderConfig, ProviderStaticHeaderConfig},
    coordinator::WireCandidate,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{CandidateOwnership, ProcessRuntime, RuntimeGenerationFactory},
    wire::{ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireSurface},
};

async fn process_runtime() -> ProcessRuntime {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    ProcessRuntime::new(database)
}

fn provider_config() -> ProviderConfig {
    ProviderConfig {
        id: "provider".to_owned(),
        base_url: "https://provider.example".to_owned(),
        protocols: vec!["openai".to_owned()],
        auth: ProviderAuthConfig {
            mode: "none".to_owned(),
            ..Default::default()
        },
        headers: vec![ProviderStaticHeaderConfig {
            name: "x-secret".to_owned(),
            value: Some("super-secret-api-key".to_owned()),
            value_env: None,
        }],
        accounts: Vec::new(),
        ..Default::default()
    }
}

fn profiles() -> Vec<WireCandidate> {
    vec![
        WireCandidate::new(
            ConfiguredWireProfile {
                definition: WireProfileDefinition {
                    surface: WireSurface::OpenaiChatCompletions,
                    request_codec: WireCodecId::OpenaiChat,
                    response_codec: WireCodecId::OpenaiChat,
                    stream_codec: WireCodecId::OpenaiChatSse,
                },
                path_template: "/v1/chat/completions".to_owned(),
                stream_path_template: None,
                priority: 0,
            },
            "chat-structure",
        ),
        WireCandidate::new(
            ConfiguredWireProfile {
                definition: WireProfileDefinition {
                    surface: WireSurface::AnthropicMessages,
                    request_codec: WireCodecId::AnthropicMessages,
                    response_codec: WireCodecId::AnthropicMessages,
                    stream_codec: WireCodecId::AnthropicMessagesSse,
                },
                path_template: "/v1/messages".to_owned(),
                stream_path_template: None,
                priority: 1,
            },
            "messages-structure",
        ),
    ]
}

#[tokio::test]
async fn factory_builds_one_shared_m7_graph_for_finite_and_streaming() {
    let process = process_runtime().await;
    let candidate =
        RuntimeGenerationFactory::prepare(&process, Config::default(), "digest-r002".to_owned(), 1)
            .await
            .expect("candidate prepares");
    let generation = candidate.transfer().expect("candidate transfers");

    let finite = generation.inference().finite_coordinator();
    let streaming = generation.inference().streaming_coordinator();
    assert!(
        finite
            .finalization_supervisor()
            .same_as(&streaming.finalization_supervisor())
    );
    assert!(
        finite
            .wire_resolver()
            .same_as(&process.wire_profile_resolver())
    );
    assert!(Arc::ptr_eq(
        &generation.inference().affinity_handle(),
        &process.model_router_affinity()
    ));
    assert_eq!(generation.generation_id(), 1);
    assert_eq!(generation.content_digest(), "digest-r002");

    generation.close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn process_wire_learning_survives_candidates_but_fingerprint_changes_partition_it() {
    let process = process_runtime().await;
    let mut config = Config::default();
    config
        .providers
        .insert("provider".to_owned(), provider_config());
    let first =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "digest-one".to_owned(), 1)
            .await
            .expect("first candidate prepares");
    let second = RuntimeGenerationFactory::prepare(&process, config, "digest-two".to_owned(), 2)
        .await
        .expect("second candidate prepares");
    let first_generation = first.transfer().expect("first transfer");
    assert!(Arc::ptr_eq(
        &first_generation.inference().affinity_handle(),
        &process.model_router_affinity()
    ));
    let second_generation = second.transfer().expect("second transfer");

    let resolver = process.wire_profile_resolver();
    let now = Instant::now();
    let same = resolver.resolve("provider", "model", profiles(), now);
    resolver.accept(
        "provider",
        "model",
        &same.fingerprint,
        WireSurface::AnthropicMessages,
        now,
    );
    let learned = resolver.resolve("provider", "model", profiles(), now);
    assert_eq!(
        learned.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    let changed = resolver.resolve(
        "provider",
        "model",
        vec![WireCandidate::new(
            profiles()[0].profile.clone(),
            "changed-structure",
        )],
        now,
    );
    assert_eq!(
        changed.candidates[0].surface(),
        WireSurface::OpenaiChatCompletions
    );

    first_generation.close().await;
    second_generation.close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn candidate_abort_is_idempotent_and_does_not_close_process_state() {
    let process = process_runtime().await;
    let mut config = Config::default();
    config
        .providers
        .insert("provider".to_owned(), provider_config());
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config, "digest-abort".to_owned(), 1)
            .await
            .expect("candidate prepares");
    let first = candidate.abort().await;
    let second = candidate.abort().await;
    assert_eq!(first.ownership, CandidateOwnership::Aborted);
    assert_eq!(first.close_report, second.close_report);
    assert_eq!(
        first
            .close_report
            .as_ref()
            .expect("close report")
            .provider_clients
            .close_count,
        1
    );
    assert_eq!(process.wire_profile_resolver().snapshot().entries, 0);
    process
        .database()
        .call(|connection| {
            connection.execute_batch("SELECT 1").map_err(|error| {
                tokio_rusqlite::rusqlite::Error::ToSqlConversionFailure(Box::new(error))
            })
        })
        .await
        .expect("process database remains open");
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn structural_failure_happens_before_client_pool_construction() {
    let process = process_runtime().await;
    let mut config = Config::default();
    config
        .model_routers
        .insert("invalid-router".to_owned(), ModelRouterConfig::default());
    let error = RuntimeGenerationFactory::prepare(&process, config, "digest-invalid".to_owned(), 1)
        .await
        .expect_err("invalid model-router structure is rejected before pool construction");
    assert!(matches!(
        error,
        eggpool::runtime_lifecycle::GenerationBuildError::Config(_)
    ));
    assert_eq!(process.wire_profile_resolver().snapshot().entries, 0);
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn transferred_candidate_is_not_abortable_by_candidate_owner() {
    let process = process_runtime().await;
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        Config::default(),
        "digest-transfer".to_owned(),
        1,
    )
    .await
    .expect("candidate prepares");
    let generation = candidate.transfer().expect("candidate transfers");
    let report = candidate.abort().await;
    assert_eq!(report.ownership, CandidateOwnership::Transferred);
    assert!(report.transferred);
    assert!(!generation.provider_client_pool().is_closed());
    generation.close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn failed_graph_build_closes_candidate_pool_but_not_process_database() {
    let process = process_runtime().await;
    let database = process.database();
    database
        .close()
        .await
        .expect("close database before graph build");
    let mut config = Config::default();
    config
        .providers
        .insert("provider".to_owned(), provider_config());
    let error = RuntimeGenerationFactory::prepare(&process, config, "digest-failure".to_owned(), 1)
        .await
        .expect_err("closed process database rejects graph construction");
    assert!(error.to_string().contains("inference account load failed"));
    let eggpool::runtime_lifecycle::GenerationBuildError::Graph {
        provider_clients, ..
    } = error
    else {
        panic!("expected graph construction failure after pool creation");
    };
    assert!(provider_clients.closed_now);
    assert_eq!(provider_clients.close_count, 1);
    assert!(
        process
            .database()
            .call(|_| Ok::<_, tokio_rusqlite::rusqlite::Error>(()))
            .await
            .is_err()
    );
}

#[tokio::test]
async fn lifecycle_debug_is_secret_free() {
    let process = process_runtime().await;
    let mut config = Config::default();
    config
        .providers
        .insert("provider".to_owned(), provider_config());
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config, "digest-debug".to_owned(), 1)
            .await
            .expect("candidate prepares");
    let generation = candidate.transfer().expect("candidate transfers");
    let debug = format!("{:?} {:?} {:?}", process, generation, candidate);
    assert!(!debug.contains("super-secret-api-key"));
    assert!(!debug.contains("proxy-secret"));
    generation.close().await;
    process.database().close().await.expect("database closes");
}
