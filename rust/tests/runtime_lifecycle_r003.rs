//! R003 active-generation publication, linearizable leases, and gate tests.

use std::sync::Arc;

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{
        CandidateOwnership, GenerationAcquireError, GenerationSlotState, GenerationStageError,
        GenerationSwapError, ProcessRuntime, RuntimeGenerationFactory, RuntimeManager,
    },
};
use tokio::time::{Duration, timeout};

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
        "digest-r003-a".to_owned(),
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
    digest: &str,
    generation_id: u64,
) -> eggpool::runtime_lifecycle::PreparedGeneration {
    RuntimeGenerationFactory::prepare(process, Config::default(), digest.to_owned(), generation_id)
        .await
        .expect("candidate prepares")
}

#[tokio::test]
async fn acquire_and_publication_have_one_linearization_point() {
    let (process, generation_a) = fixture().await;
    let manager = RuntimeManager::new(Arc::clone(&generation_a));

    let old_leases = (0..1_000).map(|_| manager.acquire()).collect::<Vec<_>>();
    let mut old_leases = futures_join_all(old_leases).await;
    assert!(
        old_leases
            .iter()
            .all(|lease| lease.as_ref().is_ok_and(|lease| lease.generation_id() == 1))
    );
    assert_eq!(manager.active_slot().active_lease_count(), 1_000);

    let staged_candidate = candidate(&process, "digest-r003-b", 2).await;
    let mut staged = manager
        .stage(1, &staged_candidate)
        .expect("stage closes admission");
    assert!(manager.admission_closed());
    assert_eq!(manager.publication_epoch(), 0);

    let waiter = tokio::spawn({
        let manager = manager.clone();
        async move { manager.acquire().await }
    });
    assert!(
        timeout(Duration::from_millis(20), async { waiter.is_finished() })
            .await
            .is_ok()
    );
    assert!(!waiter.is_finished(), "gate must block acquisition");

    staged.commit_pointer().expect("pointer commits");
    assert!(staged.pointer_committed());
    assert_eq!(manager.active_generation().generation_id(), 2);
    assert!(
        manager.admission_closed(),
        "pointer commit keeps gate closed"
    );
    assert!(!manager.active_slot().accepting());

    for lease in &old_leases {
        assert_eq!(lease.as_ref().expect("old lease").generation_id(), 1);
    }
    let publication = staged.accept().expect("accept reopens admission");
    assert_eq!(publication.epoch, 1);
    assert_eq!(manager.publication_epoch(), 1);
    assert!(!publication.old_slot.accepting());
    assert_eq!(publication.old_slot.state(), GenerationSlotState::Retiring);

    let new_lease = timeout(Duration::from_secs(1), waiter)
        .await
        .expect("waiter wakes")
        .expect("waiter task joins")
        .expect("new lease acquires");
    assert_eq!(new_lease.generation_id(), 2);
    assert_eq!(manager.active_slot().active_lease_count(), 1);
    drop(new_lease);
    for lease in old_leases.drain(..) {
        drop(lease);
    }
    publication.old_slot.wait_for_drain().await;
    assert_eq!(publication.old_slot.active_lease_count(), 0);

    publication.new_slot.generation().close().await;
    publication.old_slot.generation().close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn rollback_restores_old_generation_without_epoch_increment() {
    let (process, generation_a) = fixture().await;
    let manager = RuntimeManager::new(Arc::clone(&generation_a));
    let staged_candidate = candidate(&process, "digest-r003-b", 2).await;
    let mut staged = manager.stage(1, &staged_candidate).expect("stage succeeds");
    staged.commit_pointer().expect("pointer commits");
    staged
        .rollback_pointer()
        .expect("pointer rollback succeeds");
    assert_eq!(manager.active_generation().generation_id(), 1);
    assert!(manager.active_slot().accepting());
    assert!(
        manager.admission_closed(),
        "rollback pointer keeps the gate closed for compensation"
    );
    let first_rolled_back = staged.rollback().expect("rollback finalizes");
    assert_eq!(first_rolled_back.generation_id(), 2);
    first_rolled_back.close().await;

    // A second staged object proves the explicit rollback path reopens the
    // gate and returns candidate ownership for asynchronous cleanup.
    let staged_candidate = candidate(&process, "digest-r003-c", 3).await;
    let mut staged = manager
        .stage(1, &staged_candidate)
        .expect("stage after pointer rollback succeeds");
    let rolled_back = staged.rollback().expect("rollback succeeds");
    assert_eq!(rolled_back.generation_id(), 3);
    assert_eq!(manager.publication_epoch(), 0);
    assert_eq!(manager.active_generation().generation_id(), 1);
    assert!(!manager.admission_closed());
    assert_eq!(rolled_back.close().await.generation_id, 3);
    generation_a.close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn cancelled_waiter_and_shutdown_leave_no_lease_or_gate_leak() {
    let (process, generation_a) = fixture().await;
    let manager = RuntimeManager::new(generation_a.clone());
    let staged_candidate = candidate(&process, "digest-r003-b", 2).await;
    let mut staged = manager.stage(1, &staged_candidate).expect("stage succeeds");
    let waiter = tokio::spawn({
        let manager = manager.clone();
        async move { manager.acquire().await }
    });
    tokio::task::yield_now().await;
    waiter.abort();
    let _ = waiter.await;
    let rolled_back = staged.rollback().expect("rollback succeeds");
    assert_eq!(manager.active_slot().active_lease_count(), 0);
    assert!(!manager.admission_closed());
    rolled_back.close().await;

    manager.shutdown();
    assert!(manager.admission_closed());
    assert!(matches!(
        manager.acquire().await,
        Err(GenerationAcquireError::ShuttingDown)
    ));
    let rejected = candidate(&process, "digest-r003-c", 3).await;
    assert!(matches!(
        manager.stage(1, &rejected),
        Err(GenerationStageError::ShuttingDown)
    ));
    assert_eq!(generation_a.close().await.generation_id, 1);
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn accepted_old_and_new_leases_pin_their_generation() {
    let (process, generation_a) = fixture().await;
    let manager = RuntimeManager::new(generation_a.clone());
    let finite_lease = manager.acquire().await.expect("finite lease");
    let stream_lease = manager.acquire().await.expect("stream lease");
    let staged_candidate = candidate(&process, "digest-r003-b", 2).await;
    let mut staged = manager.stage(1, &staged_candidate).expect("stage succeeds");
    staged.commit_pointer().expect("pointer commits");
    let publication = staged.accept().expect("publication accepts");

    assert_eq!(finite_lease.generation_id(), 1);
    assert_eq!(stream_lease.generation_id(), 1);
    assert_eq!(
        manager
            .acquire()
            .await
            .expect("new request")
            .generation_id(),
        2
    );
    drop(finite_lease);
    drop(stream_lease);
    publication.old_slot.wait_for_drain().await;
    publication.new_slot.generation().close().await;
    publication.old_slot.generation().close().await;
    process.database().close().await.expect("database closes");
}

#[tokio::test]
async fn retiring_placeholder_is_bounded_until_r004_reaps_it() {
    let (process, generation_a) = fixture().await;
    let manager = RuntimeManager::new(generation_a.clone());
    let mut publications = Vec::new();
    let mut leases = Vec::new();
    for generation_id in 2..=5 {
        leases.push(manager.acquire().await.expect("generation lease"));
        let staged_candidate = candidate(
            &process,
            &format!("digest-r003-{generation_id}"),
            generation_id,
        )
        .await;
        let mut staged = manager
            .stage(generation_id - 1, &staged_candidate)
            .expect("stage succeeds before backlog limit");
        staged.commit_pointer().expect("pointer commits");
        publications.push(staged.accept().expect("publication accepts"));
    }
    assert_eq!(manager.retiring_slot_count(), 4);

    let rejected = candidate(&process, "digest-r003-overflow", 6).await;
    assert!(matches!(
        manager.stage(5, &rejected),
        Err(GenerationStageError::RetirementBacklog)
    ));
    rejected.abort().await;

    drop(leases);
    manager.drain_retirements().await;
    assert_eq!(manager.retiring_slot_count(), 0);
    for publication in publications {
        publication.old_slot.generation().close().await;
    }
    manager.active_generation().close().await;
    process.database().close().await.expect("database closes");
}

#[test]
fn swap_errors_are_secret_free_and_explicit() {
    let stage = format!("{:?}", GenerationSwapError::ActivePointerChanged);
    assert_eq!(stage, "ActivePointerChanged");
    assert!(!format!("{:?}", CandidateOwnership::Prepared).contains("secret"));
}

// Keep the test's high-cardinality acquisition loop deterministic without
// adding a futures dependency to the production crate.
async fn futures_join_all<T>(futures: Vec<T>) -> Vec<T::Output>
where
    T: std::future::Future,
{
    let mut outputs = Vec::with_capacity(futures.len());
    for future in futures {
        outputs.push(future.await);
    }
    outputs
}
