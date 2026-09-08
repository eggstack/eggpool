//! R004 generation retirement, retained finalization, close ordering, and
//! bounded manager-state tests.

use std::sync::Arc;

use eggpool::{
    Config,
    coordinator::{
        FinalizationCommand, FinalizationData, FinalizationIdentity, FinalizationOutcome,
    },
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{
        GenerationCloseStep, GenerationSlotState, ProcessRuntime, RetirementFailure,
        RuntimeGenerationFactory, RuntimeManager,
    },
};
use tokio::time::{Duration, sleep, timeout};

async fn fixture() -> (
    ProcessRuntime,
    Arc<eggpool::runtime_lifecycle::RuntimeGeneration>,
) {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let process = ProcessRuntime::new(database);
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        Config::default(),
        "digest-r004-a".to_owned(),
        1,
    )
    .await
    .expect("generation A prepares");
    (
        process,
        candidate.transfer().expect("generation A transfers"),
    )
}

async fn candidate(
    process: &ProcessRuntime,
    generation_id: u64,
) -> eggpool::runtime_lifecycle::PreparedGeneration {
    RuntimeGenerationFactory::prepare(
        process,
        Config::default(),
        format!("digest-r004-{generation_id}"),
        generation_id,
    )
    .await
    .expect("candidate prepares")
}

async fn publish(
    process: &ProcessRuntime,
    manager: &RuntimeManager,
    expected_generation: u64,
    generation_id: u64,
) -> eggpool::runtime_lifecycle::AcceptedGenerationPublication {
    let candidate = candidate(process, generation_id).await;
    let mut staged = manager
        .stage(expected_generation, &candidate)
        .expect("stage succeeds");
    staged.commit_pointer().expect("pointer commits");
    staged.accept().expect("publication accepts")
}

#[tokio::test]
async fn lease_drain_precedes_close_and_reaps_the_slot() {
    let (process, generation) = fixture().await;
    let manager = RuntimeManager::new(generation).with_close_timeout(Duration::from_millis(100));
    let lease = manager.acquire().await.expect("request lease");
    let publication = publish(&process, &manager, 1, 2).await;

    sleep(Duration::from_millis(5)).await;
    assert_eq!(publication.old_slot.state(), GenerationSlotState::Retiring);
    assert!(
        !publication
            .old_slot
            .generation()
            .provider_client_pool()
            .is_closed()
    );

    drop(lease);
    manager.drain_retirements().await;
    assert_eq!(publication.old_slot.state(), GenerationSlotState::Closed);
    assert_eq!(
        publication
            .old_slot
            .generation()
            .provider_client_pool()
            .close_count(),
        1
    );
    assert_eq!(manager.retiring_slot_count(), 0);
    assert_eq!(manager.retirement_task_count(), 0);

    manager.active_generation().close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn retained_finalization_reference_blocks_provider_close() {
    let (process, generation) = fixture().await;
    let manager = RuntimeManager::new(generation).with_close_timeout(Duration::from_millis(100));
    let lease = manager.acquire().await.expect("request lease");
    let publication = publish(&process, &manager, 1, 2).await;
    let retained = publication
        .old_slot
        .try_retain_finalization()
        .expect("terminal reference accepted before drain");
    drop(lease);

    timeout(Duration::from_secs(1), async {
        loop {
            if publication.old_slot.state() == GenerationSlotState::DrainingFinalization {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("retirement reaches finalization drain");
    assert!(
        !publication
            .old_slot
            .generation()
            .provider_client_pool()
            .is_closed()
    );

    drop(retained);
    manager.drain_retirements().await;
    assert_eq!(publication.old_slot.state(), GenerationSlotState::Closed);
    assert_eq!(publication.old_slot.terminal_reference_count(), 0);
    assert_eq!(
        publication
            .old_slot
            .generation()
            .provider_client_pool()
            .close_count(),
        1
    );

    manager.active_generation().close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn close_order_is_deterministic_and_duplicate_retirement_is_idempotent() {
    let (process, generation) = fixture().await;
    let manager = RuntimeManager::new(generation).with_close_timeout(Duration::from_millis(100));
    let publication = publish(&process, &manager, 1, 2).await;
    assert!(!manager.schedule_retirement(Arc::clone(&publication.old_slot)));
    manager.drain_retirements().await;

    let report = publication.old_slot.generation().close().await;
    assert_eq!(
        report.close_order,
        vec![
            GenerationCloseStep::GenerationTasksClosed,
            GenerationCloseStep::FinalizationDrained,
            GenerationCloseStep::ProviderClientsClosed,
            GenerationCloseStep::GenerationHandlesReleased,
        ]
    );
    assert!(report.failure.is_none());
    assert_eq!(report.provider_clients.close_count, 1);

    manager.active_generation().close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn failed_finalization_keeps_old_transport_open_and_isolated() {
    let (process, generation) = fixture().await;
    let supervisor = generation.finalization_supervisor();
    let command = FinalizationCommand::Request {
        identity: FinalizationIdentity {
            proxy_request_id: "r004-failure".into(),
            db_request_id: 999_991,
            attempt_id: 999_992,
            reservation_id: 999_993,
            account_id: 999_994,
            account_name: "account".into(),
            provider_id: "provider".into(),
            model_id: "model".into(),
            upstream_model_id: "model".into(),
            client_protocol: "openai".into(),
            upstream_protocol: "openai".into(),
            attempt_number: 1,
        },
        data: FinalizationData {
            outcome: FinalizationOutcome::Completed,
            ..FinalizationData::default()
        },
        claim: None,
    };
    let manager = RuntimeManager::new(generation).with_close_timeout(Duration::from_millis(100));
    let lease = manager.acquire().await.expect("request lease");
    let publication = publish(&process, &manager, 1, 2).await;
    supervisor
        .register(command)
        .expect("retained command registers");
    assert_eq!(publication.old_slot.terminal_reference_count(), 1);
    drop(lease);
    manager.drain_retirements().await;

    assert_eq!(
        publication.old_slot.state(),
        GenerationSlotState::FailedClose
    );
    assert!(
        !publication
            .old_slot
            .generation()
            .provider_client_pool()
            .is_closed()
    );
    let diagnostics = manager.retirement_diagnostics();
    assert!(matches!(
        diagnostics
            .last()
            .and_then(|diagnostic| diagnostic.failure.as_ref()),
        Some(RetirementFailure::GenerationClose(_))
    ));
    assert!(!format!("{diagnostics:?}").contains("r004-failure"));

    let new_lease = manager
        .acquire()
        .await
        .expect("active generation remains healthy");
    assert_eq!(new_lease.generation_id(), 2);
    drop(new_lease);
    manager.active_generation().close().await;
    process.database().close().await.expect("database closes");
}
