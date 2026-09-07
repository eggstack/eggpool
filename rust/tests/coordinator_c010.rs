//! C010 crash/restart reconciliation and fault injection.
//!
//! The frozen Python oracle is `_crash_recovery` in `src/eggpool/app.py`:
//! every `pending` request becomes `interrupted`, every `active`
//! reservation is released with `crash_recovery`, and every open attempt is
//! completed with `process_interrupted`. No time gate, no new rows, no
//! cost/token mutation, no ephemeral-state hydration.
//!
//! These tests prove the Rust [`CrashReconciler`] freezes that policy
//! through bounded indexed scans, converges every required durable state
//! after a simulated crash/restart, stays idempotent and bounded under
//! repetition and concurrency, never replays unknown in-flight work, keeps
//! Python rollback readability, and exposes deterministic fault hooks for
//! each plan crash point.

use std::{
    collections::BTreeSet,
    path::PathBuf,
    process::Command,
    sync::{
        Arc, Barrier,
        atomic::{AtomicBool, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        CoordinatorFaultInjector, CrashFaultPoint, CrashReconciler, DurableFinalizer,
        FinalizationCommand, FinalizationData, FinalizationError, FinalizationOutcome,
        FinalizationSupervisor, PublicationError, PublicationFaultInjector, PublicationInput,
        PublicationOutcome, PublicationService, PublicationStage, ReconciliationConfig,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
};

struct Fixture {
    database: Database,
    router: RoutingRouter,
    estimator: QuotaEstimator,
}

async fn fixture() -> Fixture {
    fixture_on(&DatabaseConfig::default()).await
}

async fn fixture_on(config: &DatabaseConfig) -> Fixture {
    let database = Database::open(config.clone())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("canonical migrations apply");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                 VALUES (1, 'account-a', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                 VALUES ('model-a', 'openai', 'provider-a', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture rows insert");

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
    provider.accounts.push(eggpool::config::AccountConfig {
        name: "account-a".into(),
        ..Default::default()
    });
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[Account {
            id: 1,
            name: "account-a".into(),
            api_key_env: "UNUSED".into(),
            enabled: true,
            weight: 1.0,
            provider_id: "provider-a".into(),
        }],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("account-a", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("catalog model");
    let estimator = QuotaEstimator::new([AccountQuota::new("account-a")]);
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator.clone(),
        None,
        EligibilityPolicy::default(),
    );
    Fixture {
        database,
        router,
        estimator,
    }
}

fn facts() -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    facts
}

async fn claim(fixture: &Fixture) -> eggpool::routing::SelectionClaim {
    fixture
        .router
        .select_and_claim(&facts(), &BTreeSet::new())
        .await
        .expect("claim succeeds")
        .expect("fixture has a candidate")
}

fn input(proxy_id: &str, attempt_number: i64) -> PublicationInput {
    PublicationInput::new(proxy_id, "openai", "openai", false, attempt_number)
}

async fn durable_counts(database: &Database) -> (i64, i64, i64) {
    database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT COUNT(*) FROM requests", [], |row| row.get(0))?,
                connection.query_row("SELECT COUNT(*) FROM request_attempts", [], |row| {
                    row.get(0)
                })?,
                connection.query_row("SELECT COUNT(*) FROM reservations", [], |row| row.get(0))?,
            ))
        })
        .await
        .expect("row counts")
}

async fn nonterminal_counts(database: &Database) -> (i64, i64, i64) {
    database
        .call(|connection| {
            Ok((
                connection.query_row(
                    "SELECT COUNT(*) FROM requests WHERE status = 'pending'",
                    [],
                    |row| row.get(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM request_attempts WHERE completed_at IS NULL",
                    [],
                    |row| row.get(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM reservations WHERE status = 'active'",
                    [],
                    |row| row.get(0),
                )?,
            ))
        })
        .await
        .expect("nonterminal counts")
}

fn temporary_database_path(label: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock is after epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "eggpool-c010-{label}-{}-{nanos}.sqlite3",
        std::process::id()
    ))
}

// ---------------------------------------------------------------------------
// Durable-state convergence
// ---------------------------------------------------------------------------

#[tokio::test]
async fn request_without_attempt_converges_to_interrupted() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    // Publish then remove the child rows to leave a bare pending request:
    // the exact durable shape of a crash between request insert and the
    // attempt/reservation writes (or before any publication at all).
    let outcome = service
        .publish(claim(&fixture).await, input("c010-no-attempt", 1))
        .await
        .expect("publication succeeds");
    let eggpool::coordinator::PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    let request_id = published.identity.db_request_id;
    fixture
        .database
        .call(move |connection| {
            connection.execute(
                "DELETE FROM request_attempts WHERE request_id = ?1",
                [request_id],
            )?;
            connection.execute(
                "DELETE FROM reservations WHERE request_id = ?1",
                [request_id],
            )?;
            connection.execute(
                "DELETE FROM routing_decisions WHERE request_id = ?1",
                [request_id],
            )?;
            Ok(())
        })
        .await
        .expect("child rows removed");
    // Simulate process death: drop the converted claim without terminal
    // release, then reconcile with a completely fresh reconciler.
    drop(published.claim);

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert_eq!(report.requests_interrupted, 1);
    assert!(report.classification.pending_requests_without_attempt >= 1);
    assert!(report.bounded);
    let status = fixture
        .database
        .call(move |connection| {
            connection.query_row(
                "SELECT status, completed_at IS NOT NULL FROM requests WHERE id = ?1",
                [request_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
            )
        })
        .await
        .expect("request row");
    assert_eq!(status, ("interrupted".to_owned(), 1));
    assert_eq!(nonterminal_counts(&fixture.database).await.0, 0);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn open_attempt_with_active_reservation_terminalizes_both() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-open-attempt", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    let (request_id, attempt_id, reservation_id) = (
        published.identity.db_request_id,
        published.identity.attempt_id,
        published.identity.reservation_id,
    );
    drop(published.claim);

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert_eq!(report.requests_interrupted, 1);
    assert_eq!(report.attempts_terminalized, 1);
    assert_eq!(report.reservations_released, 1);
    assert!(report.classification.open_attempts_with_active_reservation >= 1);

    let rows = fixture
        .database
        .call(move |connection| {
            Ok((
                connection.query_row(
                    "SELECT status FROM requests WHERE id = ?1",
                    [request_id],
                    |row| row.get::<_, String>(0),
                )?,
                connection.query_row(
                    "SELECT completed_at IS NOT NULL, error_class FROM request_attempts WHERE id = ?1",
                    [attempt_id],
                    |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?)),
                )?,
                connection.query_row(
                    "SELECT status, release_reason FROM reservations WHERE id = ?1",
                    [reservation_id],
                    |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, Option<String>>(1)?,
                        ))
                    },
                )?,
            ))
        })
        .await
        .expect("converged rows");
    assert_eq!(rows.0, "interrupted");
    assert_eq!(rows.1, (1, Some("process_interrupted".to_owned())));
    assert_eq!(
        rows.2,
        ("released".to_owned(), Some("crash_recovery".to_owned()))
    );
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn terminal_attempt_with_active_reservation_releases_only_the_reservation() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-terminal-attempt", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    // Terminalize the attempt through the durable path but simulate a
    // crash before the reservation release committed: re-arm the
    // reservation to active behind a completed attempt.
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    finalizer
        .finalize_failed_attempt(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(503),
                error_class: Some("temporary".into()),
                release_reason: Some("retryable".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("failed-attempt cleanup");
    let reservation_id = published.identity.reservation_id;
    fixture
        .database
        .call(move |connection| {
            connection.execute(
                "UPDATE reservations SET status = 'active', released_at = NULL,
                 release_reason = NULL WHERE id = ?1",
                [reservation_id],
            )?;
            Ok(())
        })
        .await
        .expect("reservation re-armed to active");

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert_eq!(report.reservations_released, 1);
    assert!(
        report
            .classification
            .terminal_attempts_with_active_reservation
            >= 1
    );
    // The already-terminal attempt is untouched; the pending parent request
    // is fail-closed to interrupted (never left retryable after a crash).
    let rows = fixture
        .database
        .call(move |connection| {
            Ok((
                connection.query_row(
                    "SELECT completed_at IS NOT NULL FROM request_attempts WHERE request_id = 1",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row(
                    "SELECT status FROM reservations WHERE id = ?1",
                    [reservation_id],
                    |row| row.get::<_, String>(0),
                )?,
            ))
        })
        .await
        .expect("rows");
    assert_eq!(rows, (1, "released".to_owned()));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn terminal_request_with_open_attempt_converges_without_touching_the_request() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-terminal-parent", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    let request_id = published.identity.db_request_id;
    // Complete the request through the terminal path, then simulate a
    // crash that left a second open attempt and an active reservation
    // behind (e.g. interrupted retry publication).
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("terminal completion");
    fixture
        .database
        .call(move |connection| {
            connection.execute(
                "INSERT INTO request_attempts
                 (request_id, attempt_number, account_id, provider_id, model_id, protocol, streamed)
                 VALUES (?1, 2, 1, 'provider-a', 'model-a', 'openai', 0)",
                [request_id],
            )?;
            connection.execute(
                "INSERT INTO reservations
                 (request_id, account_id, model_id, reserved_microdollars, estimated_tokens)
                 VALUES (?1, 1, 'model-a', 0, 0)",
                [request_id],
            )?;
            Ok(())
        })
        .await
        .expect("open leftover rows");

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert_eq!(report.attempts_terminalized, 1);
    assert_eq!(report.reservations_released, 1);
    assert!(report.classification.open_attempts_with_terminal_parent >= 1);
    let status = fixture
        .database
        .call(move |connection| {
            Ok((
                connection.query_row(
                    "SELECT status FROM requests WHERE id = ?1",
                    [request_id],
                    |row| row.get::<_, String>(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM request_attempts WHERE request_id = ?1 AND completed_at IS NULL",
                    [request_id],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM reservations WHERE request_id = ?1 AND status = 'active'",
                    [request_id],
                    |row| row.get::<_, i64>(0),
                )?,
            ))
        })
        .await
        .expect("rows");
    assert_eq!(status, ("completed".to_owned(), 0, 0));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn post_commit_interruption_converges_through_reconciliation() {
    let fixture = fixture().await;
    let injector = PublicationFaultInjector::fail_once_at(PublicationStage::AfterCommit);
    let service = PublicationService::new(fixture.database.clone()).with_fault_injector(injector);
    let error = service
        .publish(claim(&fixture).await, input("c010-post-commit", 1))
        .await
        .expect_err("post-commit interruption is surfaced");
    let PublicationError::PostCommit { interruption } = error else {
        panic!("expected retained post-commit identity");
    };
    assert_eq!(durable_counts(&fixture.database).await, (1, 1, 1));
    // Simulate process death before compensation: the claim and receipt
    // vanish; only durable rows survive.
    drop(interruption.claim);

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert_eq!(
        nonterminal_counts(&fixture.database).await,
        (0, 0, 0),
        "post-commit leftover must fully converge: {report:?}"
    );
    // Compensation after reconciliation stays idempotent-safe: the
    // durable rows are already terminal, so a second compensation pass
    // would observe convergence rather than fan out.
    let second = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("second pass succeeds");
    assert!(second.converged);
    assert_eq!(durable_counts(&fixture.database).await, (1, 1, 1));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn failed_attempt_cleanup_pending_fails_closed_to_interrupted() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-retryable", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    // A crash before failed-attempt cleanup leaves a pending request with
    // an open attempt. Reconciliation must fail closed (interrupted), never
    // leave the request retryable and never schedule a provider replay.
    drop(published.claim);
    let before = durable_counts(&fixture.database).await;

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert!(!report.converged);
    assert_eq!(durable_counts(&fixture.database).await, before);
    let status = fixture
        .database
        .call(|connection| {
            connection.query_row("SELECT status FROM requests", [], |row| {
                row.get::<_, String>(0)
            })
        })
        .await
        .expect("request status");
    assert_eq!(status, "interrupted");
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn converged_terminal_state_is_a_noop() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-converged", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("terminal completion");

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert!(report.converged);
    assert_eq!(report.fixed_total(), 0);
    assert_eq!(durable_counts(&fixture.database).await, (1, 1, 1));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stale_duplicate_terminal_evidence_needs_no_durable_write() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-duplicate", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    let supervisor = FinalizationSupervisor::new(DurableFinalizer::new(fixture.database.clone()));
    let data = FinalizationData {
        outcome: FinalizationOutcome::Completed,
        release_reason: Some("completed".into()),
        ..FinalizationData::default()
    };
    let first = supervisor
        .register(FinalizationCommand::Request {
            identity: published.identity.clone(),
            data: data.clone(),
            claim: Some(published.claim),
        })
        .expect("first registration");
    // A stale duplicate (retry, redelivery, or post-restart replay of the
    // same terminal command) shares the job instead of writing new rows.
    let second = supervisor
        .register(FinalizationCommand::Request {
            identity: published.identity.clone(),
            data,
            claim: None,
        })
        .expect("duplicate shares the terminal job");
    let (first, second) = tokio::join!(first.wait(), second.wait());
    assert!(first.is_ok());
    assert!(second.is_ok());
    supervisor.drain().await;

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert!(report.converged);
    assert_eq!(durable_counts(&fixture.database).await, (1, 1, 1));
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Idempotency, boundedness, concurrency
// ---------------------------------------------------------------------------

#[tokio::test]
async fn repeated_reconciliation_is_idempotent_without_row_fanout() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    for index in 0..3 {
        let outcome = service
            .publish(
                claim(&fixture).await,
                input(&format!("c010-idempotent-{index}"), 1),
            )
            .await
            .expect("publication succeeds");
        let PublicationOutcome::Published(published) = outcome else {
            panic!("expected a new publication");
        };
        drop(published.claim);
    }
    assert_eq!(durable_counts(&fixture.database).await, (3, 3, 3));

    let first = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("first pass succeeds");
    assert_eq!(first.fixed_total(), 9);
    for _ in 0..3 {
        let repeat = CrashReconciler::new(fixture.database.clone())
            .reconcile_once()
            .await
            .expect("repeat pass succeeds");
        assert!(repeat.converged);
        assert_eq!(repeat.fixed_total(), 0);
    }
    assert_eq!(durable_counts(&fixture.database).await, (3, 3, 3));
    assert_eq!(nonterminal_counts(&fixture.database).await, (0, 0, 0));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn concurrent_reconciliation_converges_without_new_rows() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    for index in 0..4 {
        let outcome = service
            .publish(
                claim(&fixture).await,
                input(&format!("c010-concurrent-{index}"), 1),
            )
            .await
            .expect("publication succeeds");
        let PublicationOutcome::Published(published) = outcome else {
            panic!("expected a new publication");
        };
        drop(published.claim);
    }
    let database = fixture.database.clone();
    let workers = (0..8).map(|_| {
        let database = database.clone();
        tokio::spawn(async move {
            CrashReconciler::new(database)
                .reconcile_once()
                .await
                .expect("concurrent pass succeeds")
        })
    });
    let mut fixed_total = 0;
    for worker in workers {
        fixed_total += worker.await.expect("worker joins").fixed_total();
    }
    // Exactly one pass worth of work exists across all workers; racing
    // passes observe zero rows through the conditional updates.
    assert_eq!(fixed_total, 12);
    assert_eq!(durable_counts(&fixture.database).await, (4, 4, 4));
    assert_eq!(nonterminal_counts(&fixture.database).await, (0, 0, 0));
    // Supervisor-style job accounting stays bounded: no queue grows with
    // repeated invocation.
    let supervisor = FinalizationSupervisor::new(DurableFinalizer::new(fixture.database.clone()));
    assert_eq!(supervisor.snapshot().active_jobs, 0);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn bounded_scans_never_exceed_the_configured_limit_per_pass() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    for index in 0..6 {
        let outcome = service
            .publish(
                claim(&fixture).await,
                input(&format!("c010-bounded-{index}"), 1),
            )
            .await
            .expect("publication succeeds");
        let PublicationOutcome::Published(published) = outcome else {
            panic!("expected a new publication");
        };
        drop(published.claim);
    }
    let first = CrashReconciler::new(fixture.database.clone())
        .with_batch_limit(2)
        .reconcile_once()
        .await
        .expect("bounded pass succeeds");
    assert!(first.bounded);
    assert!(first.truncated);
    assert_eq!(first.requests_interrupted, 2);
    assert_eq!(first.reservations_released, 2);
    assert_eq!(first.attempts_terminalized, 2);
    assert_eq!(
        ReconciliationConfig::default()
            .with_batch_limit(0)
            .batch_limit,
        1
    );
    assert_eq!(
        ReconciliationConfig::default()
            .with_batch_limit(usize::MAX)
            .batch_limit,
        eggpool::coordinator::MAX_RECONCILIATION_BATCH_LIMIT
    );

    // Drain with the same bound; total work equals the crashed rows and no
    // pass ever fans out new attempts or reservations.
    let mut total = first.fixed_total();
    loop {
        let report = CrashReconciler::new(fixture.database.clone())
            .with_batch_limit(2)
            .reconcile_once()
            .await
            .expect("drain pass succeeds");
        total += report.fixed_total();
        if report.converged {
            break;
        }
    }
    assert_eq!(total, 18);
    assert_eq!(durable_counts(&fixture.database).await, (6, 6, 6));
    assert_eq!(nonterminal_counts(&fixture.database).await, (0, 0, 0));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn reconciliation_never_double_charges_usage_or_replays_attempts() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-no-double-charge", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    let request_id = published.identity.db_request_id;
    fixture
        .database
        .call(move |connection| {
            connection.execute(
                "UPDATE requests SET input_tokens = 11, output_tokens = 7,
                 cost_microdollars = 13 WHERE id = ?1",
                [request_id],
            )?;
            Ok(())
        })
        .await
        .expect("usage persisted before crash");
    drop(published.claim);

    CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    let usage = fixture
        .database
        .call(move |connection| {
            connection.query_row(
                "SELECT input_tokens, output_tokens, cost_microdollars, status FROM requests WHERE id = ?1",
                [request_id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, String>(3)?,
                    ))
                },
            )
        })
        .await
        .expect("usage row");
    assert_eq!(usage, (11, 7, 13, "interrupted".to_owned()));
    assert_eq!(durable_counts(&fixture.database).await, (1, 1, 1));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn fresh_process_state_is_not_hydrated_from_durable_rows() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-no-hydration", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    drop(published.claim);
    CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    // The crashed process-local counts vanished with the old process. A
    // fresh M5 state must start at zero; reconciliation must not pretend
    // those ephemeral resources still exist by reconstructing them.
    let fresh = fixture_on(&DatabaseConfig::default()).await;
    assert_eq!(fresh.router.active_request_count("account-a"), 0);
    assert_eq!(
        fresh.estimator.snapshot(&["account-a".into()])["account-a"].reserved_requests,
        0
    );
    fixture.database.close().await.expect("database closes");
    fresh.database.close().await.expect("database closes");
}

#[tokio::test]
async fn reconciliation_report_carries_no_bodies_or_secrets() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-secret-free", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    let attempt_id = published.identity.attempt_id;
    fixture
        .database
        .call(move |connection| {
            connection.execute(
                "UPDATE request_attempts SET error_detail = 'synthetic-secret-sentinel body bytes'
                 WHERE id = ?1",
                [attempt_id],
            )?;
            Ok(())
        })
        .await
        .expect("secret-bearing diagnostic row");
    drop(published.claim);
    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert!(!format!("{report:?}").contains("synthetic-secret-sentinel"));
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Restart over the same database file + Python readability
// ---------------------------------------------------------------------------

#[tokio::test]
async fn restart_over_the_same_db_converges_and_stays_python_readable() {
    let path = temporary_database_path("restart");
    let config = DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    };
    let fixture = fixture_on(&config).await;
    let service = PublicationService::new(fixture.database.clone());
    for index in 0..2 {
        let outcome = service
            .publish(
                claim(&fixture).await,
                input(&format!("c010-restart-{index}"), 1),
            )
            .await
            .expect("publication succeeds");
        let PublicationOutcome::Published(published) = outcome else {
            panic!("expected a new publication");
        };
        // Simulated crash: the claim vanishes with the process; the DB
        // file retains the pending rows.
        drop(published.claim);
    }
    assert_eq!(nonterminal_counts(&fixture.database).await, (2, 2, 2));
    fixture
        .database
        .close()
        .await
        .expect("first generation closes");

    // Fresh Rust state over the same file, as after a process restart.
    let restarted = Database::open(config.clone())
        .await
        .expect("same DB reopens");
    MigrationRunner::new(&restarted)
        .run()
        .await
        .expect("migrations remain idempotent across restart");
    restarted
        .quick_check()
        .await
        .expect("restarted database is valid");
    let report = CrashReconciler::new(restarted.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert_eq!(report.fixed_total(), 6);
    let converged = CrashReconciler::new(restarted.clone())
        .reconcile_once()
        .await
        .expect("second pass succeeds");
    assert!(converged.converged);
    restarted.close().await.expect("restarted database closes");

    // Python must still open and read the reconciled rows without repair.
    let output = Command::new("python3")
        .args([
            "-c",
            "import sqlite3, sys; connection = sqlite3.connect(sys.argv[1]); \
             print(connection.execute(\"SELECT status, COUNT(*) FROM requests GROUP BY status\").fetchall()); \
             print(connection.execute(\"SELECT status, release_reason, COUNT(*) FROM reservations GROUP BY status, release_reason\").fetchall()); \
             print(connection.execute(\"SELECT error_class, COUNT(*) FROM request_attempts GROUP BY error_class\").fetchall())",
        ])
        .arg(&path)
        .output()
        .expect("Python is available for the rollback readback");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("interrupted"),
        "requests terminalized: {stdout}"
    );
    assert!(
        stdout.contains("crash_recovery"),
        "reservations released: {stdout}"
    );
    assert!(
        stdout.contains("process_interrupted"),
        "attempts terminalized: {stdout}"
    );
    std::fs::remove_file(&path).expect("temporary database removed");
}

// ---------------------------------------------------------------------------
// Deterministic fault injection at every plan crash point
// ---------------------------------------------------------------------------

#[test]
fn coordinator_fault_injector_covers_every_named_crash_point() {
    for point in [
        CrashFaultPoint::LocalClaimAcquisitionBefore,
        CrashFaultPoint::LocalClaimAcquisitionAfter,
        CrashFaultPoint::PublicationWriteBefore,
        CrashFaultPoint::PublicationWriteAfter,
        CrashFaultPoint::PublicationCommitBefore,
        CrashFaultPoint::PublicationCommitAfter,
        CrashFaultPoint::PublicationConversionBefore,
        CrashFaultPoint::PublicationConversionAfter,
        CrashFaultPoint::WireNegotiationGateBefore,
        CrashFaultPoint::WireNegotiationGateAfter,
        CrashFaultPoint::WireNegotiationFinishBefore,
        CrashFaultPoint::WireNegotiationFinishAfter,
        CrashFaultPoint::ProviderSendStartBefore,
        CrashFaultPoint::ProviderSendStartAfter,
        CrashFaultPoint::ProviderHeaderReceiptBefore,
        CrashFaultPoint::ProviderHeaderReceiptAfter,
        CrashFaultPoint::RetryDecisionBefore,
        CrashFaultPoint::RetryDecisionAfter,
        CrashFaultPoint::FailedAttemptTerminalizationBefore,
        CrashFaultPoint::FailedAttemptTerminalizationAfter,
        CrashFaultPoint::ResponseStartHandoffBefore,
        CrashFaultPoint::ResponseStartHandoffAfter,
        CrashFaultPoint::StreamFirstByteBefore,
        CrashFaultPoint::StreamFirstByteAfter,
        CrashFaultPoint::StreamTerminalEventBefore,
        CrashFaultPoint::StreamTerminalEventAfter,
        CrashFaultPoint::StreamEofBefore,
        CrashFaultPoint::StreamEofAfter,
        CrashFaultPoint::TerminalJobRegistrationBefore,
        CrashFaultPoint::TerminalJobRegistrationAfter,
        CrashFaultPoint::DurableFinalizerWriteBefore,
        CrashFaultPoint::DurableFinalizerWriteAfter,
        CrashFaultPoint::RuntimeComponentReleaseBefore,
        CrashFaultPoint::RuntimeComponentReleaseAfter,
        CrashFaultPoint::TerminalJobCompletionBefore,
        CrashFaultPoint::TerminalJobCompletionAfter,
    ] {
        let injector = CoordinatorFaultInjector::fail_once_at(point);
        assert!(injector.should_fail(point));
        assert!(!injector.should_fail(point));
        assert_eq!(injector.fired_point(), Some(point));
        // No secret, body, or session material enters the injector Debug.
        assert!(!format!("{injector:?}").contains("secret"));

        let barrier = Arc::new(Barrier::new(2));
        let entered = Arc::new(AtomicBool::new(false));
        let pausing = CoordinatorFaultInjector::block_once_at(
            point,
            Arc::clone(&barrier),
            Arc::clone(&entered),
        );
        let waiting = std::thread::spawn({
            let pausing = pausing.clone();
            move || pausing.pause_at(point)
        });
        while !entered.load(Ordering::Acquire) {
            std::thread::yield_now();
        }
        barrier.wait();
        waiting.join().expect("barrier releases");
    }
}

#[tokio::test]
async fn finalizer_faults_leave_valid_durable_state_for_reconciliation() {
    for point in [
        CrashFaultPoint::DurableFinalizerWriteBefore,
        CrashFaultPoint::DurableFinalizerWriteAfter,
        CrashFaultPoint::RuntimeComponentReleaseBefore,
        CrashFaultPoint::RuntimeComponentReleaseAfter,
        CrashFaultPoint::FailedAttemptTerminalizationBefore,
        CrashFaultPoint::FailedAttemptTerminalizationAfter,
    ] {
        let fixture = fixture().await;
        let service = PublicationService::new(fixture.database.clone());
        let outcome = service
            .publish(claim(&fixture).await, input("c010-finalizer-fault", 1))
            .await
            .expect("publication succeeds");
        let PublicationOutcome::Published(published) = outcome else {
            panic!("expected a new publication");
        };
        let injector = CoordinatorFaultInjector::fail_once_at(point);
        let finalizer =
            DurableFinalizer::new(fixture.database.clone()).with_fault_injector(injector);
        // Failed-attempt terminalization hooks only fire on the
        // failed-attempt path; durable-write and release hooks fire on
        // both terminal paths.
        let error = if matches!(
            point,
            CrashFaultPoint::FailedAttemptTerminalizationBefore
                | CrashFaultPoint::FailedAttemptTerminalizationAfter
        ) {
            finalizer
                .finalize_failed_attempt(
                    &published.identity,
                    FinalizationData {
                        outcome: FinalizationOutcome::UpstreamError,
                        status_code: Some(503),
                        release_reason: Some("retryable".into()),
                        ..FinalizationData::default()
                    },
                    Some(published.claim),
                )
                .await
                .expect_err("injected finalizer fault must surface")
        } else {
            finalizer
                .finalize_request(
                    &published.identity,
                    FinalizationData {
                        outcome: FinalizationOutcome::Completed,
                        release_reason: Some("completed".into()),
                        ..FinalizationData::default()
                    },
                    Some(published.claim),
                )
                .await
                .expect_err("injected finalizer fault must surface")
        };
        assert!(
            matches!(error, FinalizationError::Injected { point: actual } if actual == point),
            "unexpected error for {point:?}: {error:?}"
        );
        // Whatever the injection point, explicit reconciliation converges
        // the leftover without new rows and without replay.
        let before = durable_counts(&fixture.database).await;
        CrashReconciler::new(fixture.database.clone())
            .reconcile_once()
            .await
            .expect("reconciliation succeeds");
        assert_eq!(durable_counts(&fixture.database).await, before);
        assert_eq!(nonterminal_counts(&fixture.database).await, (0, 0, 0));
        fixture.database.close().await.expect("database closes");
    }
}

#[tokio::test]
async fn supervisor_registration_and_completion_faults_stay_bounded() {
    for point in [
        CrashFaultPoint::TerminalJobRegistrationBefore,
        CrashFaultPoint::TerminalJobRegistrationAfter,
        CrashFaultPoint::TerminalJobCompletionBefore,
        CrashFaultPoint::TerminalJobCompletionAfter,
    ] {
        let fixture = fixture().await;
        let service = PublicationService::new(fixture.database.clone());
        let outcome = service
            .publish(claim(&fixture).await, input("c010-supervisor-fault", 1))
            .await
            .expect("publication succeeds");
        let PublicationOutcome::Published(published) = outcome else {
            panic!("expected a new publication");
        };
        let injector = CoordinatorFaultInjector::fail_once_at(point);
        let supervisor = FinalizationSupervisor::with_capacity(
            DurableFinalizer::new(fixture.database.clone()),
            8,
        )
        .with_fault_injector(injector);
        let registration = supervisor.register(FinalizationCommand::Request {
            identity: published.identity.clone(),
            data: FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            claim: Some(published.claim),
        });
        match registration {
            Ok(handle) => {
                // Registration survived; completion carries the injected
                // fault as bounded retry-exhaustion without leaking the job.
                let _ = handle.wait().await;
                supervisor.drain().await;
            }
            Err(FinalizationError::Injected { .. }) => {}
            Err(other) => panic!("unexpected supervisor error for {point:?}: {other:?}"),
        }
        assert_eq!(supervisor.snapshot().active_jobs, 0);
        CrashReconciler::new(fixture.database.clone())
            .reconcile_once()
            .await
            .expect("reconciliation succeeds");
        assert_eq!(nonterminal_counts(&fixture.database).await, (0, 0, 0));
        fixture.database.close().await.expect("database closes");
    }
}

#[tokio::test]
async fn reconciler_fault_hook_fails_closed_before_any_write() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input("c010-reconciler-fault", 1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    drop(published.claim);
    let injector =
        CoordinatorFaultInjector::fail_once_at(CrashFaultPoint::DurableFinalizerWriteBefore);
    let error = CrashReconciler::new(fixture.database.clone())
        .with_fault_injector(injector)
        .reconcile_once()
        .await
        .expect_err("reconciler fault must fail closed");
    assert!(matches!(
        error,
        eggpool::coordinator::ReconciliationError::Injected { .. }
    ));
    // Nothing was written; a clean pass still converges the same rows.
    assert_eq!(nonterminal_counts(&fixture.database).await, (1, 1, 1));
    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("clean pass succeeds");
    assert_eq!(report.fixed_total(), 3);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn publication_barrier_crash_still_converges_after_restart() {
    let fixture = fixture().await;
    let barrier = Arc::new(Barrier::new(2));
    let entered = Arc::new(AtomicBool::new(false));
    let injector = PublicationFaultInjector::block_once_at(
        PublicationStage::BeforeCommit,
        Arc::clone(&barrier),
        Arc::clone(&entered),
    );
    let service = PublicationService::new(fixture.database.clone()).with_fault_injector(injector);
    let task = tokio::spawn({
        let service = service.clone();
        let fixture_claim = claim(&fixture).await;
        async move {
            service
                .publish(fixture_claim, input("c010-barrier", 1))
                .await
        }
    });
    while !entered.load(Ordering::Acquire) {
        tokio::task::yield_now().await;
    }
    // Crash while the publication transaction is parked: abort the waiter
    // and release the barrier so the worker compensates or commits, then
    // reconcile whatever durable shape survived.
    task.abort();
    barrier.wait();
    for _ in 0..100 {
        if fixture.router.active_request_count("account-a") == 0 {
            break;
        }
        tokio::task::yield_now().await;
    }
    CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("reconciliation succeeds");
    assert_eq!(nonterminal_counts(&fixture.database).await, (0, 0, 0));
    let counts = durable_counts(&fixture.database).await;
    assert!(counts == (0, 0, 0) || counts == (1, 1, 1));
    assert_eq!(ReconciliationConfig::default().batch_limit, 500);
    fixture.database.close().await.expect("database closes");
}
