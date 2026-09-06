use std::{
    collections::BTreeSet,
    sync::{
        Arc, Barrier,
        atomic::{AtomicBool, Ordering},
    },
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        DurableFinalizer, EffectLedger, FailureObservation, FinalizationCommand, FinalizationData,
        FinalizationError, FinalizationOutcome, FinalizationSupervisor, ProviderModelPresence,
        PublicationFaultInjector, PublicationInput, PublicationOutcome, PublicationService,
        PublicationStage, RetryPolicy, classify,
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
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                 VALUES (1, 'account-a', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                 VALUES (2, 'account-b', 'UNUSED_B', 1, 'provider-b')",
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
    let mut second_provider = eggpool::config::ProviderConfig {
        id: "provider-b".into(),
        base_url: "https://provider-b.invalid/v1".into(),
        protocols: vec!["openai".into()],
        auth: eggpool::config::ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    second_provider
        .accounts
        .push(eggpool::config::AccountConfig {
            name: "account-b".into(),
            ..Default::default()
        });
    config
        .providers
        .insert("provider-b".into(), second_provider);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[
            Account {
                id: 1,
                name: "account-a".into(),
                api_key_env: "UNUSED".into(),
                enabled: true,
                weight: 1.0,
                provider_id: "provider-a".into(),
            },
            Account {
                id: 2,
                name: "account-b".into(),
                api_key_env: "UNUSED_B".into(),
                enabled: true,
                weight: 1.0,
                provider_id: "provider-b".into(),
            },
        ],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("account-a", "provider-a");
    catalog.set_account_provider("account-b", "provider-b");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("catalog model");
    let mut second_model = ModelInput::new("model-a");
    second_model.protocol = Some("openai".into());
    second_model.protocol_source = Some("fixture".into());
    second_model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-b", "provider-b", &[second_model], true, true)
        .expect("second catalog model");
    let estimator = QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
    ]);
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

async fn published_attempt(
    fixture: &Fixture,
    request_id: &str,
    attempt_number: i64,
) -> eggpool::coordinator::PublishedAttempt {
    published_attempt_for_account(fixture, request_id, attempt_number, "account-a").await
}

async fn published_attempt_for_account(
    fixture: &Fixture,
    request_id: &str,
    attempt_number: i64,
    account_name: &str,
) -> eggpool::coordinator::PublishedAttempt {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    let excluded_accounts = ["account-a", "account-b"]
        .into_iter()
        .filter(|candidate| *candidate != account_name)
        .map(str::to_owned)
        .collect();
    let claim = fixture
        .router
        .select_and_claim(&facts, &excluded_accounts)
        .await
        .expect("claim succeeds")
        .expect("candidate exists");
    let outcome = PublicationService::new(fixture.database.clone())
        .publish(
            claim,
            PublicationInput::new(request_id, "openai", "openai", false, attempt_number),
        )
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(value) = outcome else {
        panic!("expected published attempt");
    };
    *value
}

async fn published(fixture: &Fixture, request_id: &str) -> eggpool::coordinator::PublishedAttempt {
    published_attempt(fixture, request_id, 1).await
}

#[tokio::test]
async fn request_finalization_converges_rows_and_runtime_once() {
    let fixture = fixture().await;
    let published = published(&fixture, "c006-complete").await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let result = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 3,
                output_tokens: 5,
                cost_microdollars: 7,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("finalization succeeds");
    assert!(result.request_transitioned);
    assert!(result.attempt_transitioned);
    assert!(result.reservation_transitioned);
    assert!(result.runtime_released);
    assert!(result.progress.completed);
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    assert_eq!(
        fixture.estimator.snapshot(&["account-a".into()])["account-a"].reserved_requests,
        0
    );

    let observed = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("duplicate compatible finalization observes convergence");
    assert!(!observed.request_transitioned);
    assert!(observed.progress.completed);
    assert!(!observed.progress.runtime_cleanup_required);
    let conflict = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::ClientError,
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect_err("incompatible terminal outcome must fail closed");
    assert!(matches!(
        conflict,
        FinalizationError::TerminalConflict { .. }
    ));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn failed_attempt_cleanup_leaves_request_retryable() {
    let fixture = fixture().await;
    let published = published(&fixture, "c006-retry").await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let result = finalizer
        .finalize_failed_attempt(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(503),
                error_class: Some("temporary".into()),
                error_detail: Some("secret\0provider detail".into()),
                release_reason: Some("retryable".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("failed attempt cleanup succeeds");
    assert!(result.attempt_terminal);
    assert!(!result.request_terminal);
    assert!(result.progress.completed);
    let rows = fixture
        .database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT status FROM requests WHERE id = 1", [], |row| {
                    row.get::<_, String>(0)
                })?,
                connection.query_row(
                    "SELECT completed_at IS NOT NULL FROM request_attempts WHERE id = 1",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row(
                    "SELECT status FROM reservations WHERE id = 1",
                    [],
                    |row| row.get::<_, String>(0),
                )?,
                connection.query_row(
                    "SELECT error_detail FROM request_attempts WHERE id = 1",
                    [],
                    |row| row.get::<_, Option<String>>(0),
                )?,
            ))
        })
        .await
        .expect("lifecycle rows");
    assert_eq!(rows.0, "pending");
    assert_eq!(rows.1, 1);
    assert_eq!(rows.2, "released");
    assert!(!rows.3.unwrap().contains('\0'));
    let duplicate = finalizer
        .finalize_failed_attempt(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(503),
                error_class: Some("temporary".into()),
                release_reason: Some("retryable".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("duplicate failed-attempt observation converges");
    assert!(duplicate.progress.completed);
    assert!(!duplicate.progress.runtime_cleanup_required);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn retained_supervisor_shares_duplicate_job_and_is_bounded() {
    let fixture = fixture().await;
    let published = published(&fixture, "c006-supervisor").await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let supervisor = FinalizationSupervisor::with_capacity(finalizer, 1);
    let command = FinalizationCommand::Request {
        identity: published.identity.clone(),
        data: FinalizationData {
            outcome: FinalizationOutcome::Completed,
            release_reason: Some("completed".into()),
            ..FinalizationData::default()
        },
        claim: Some(published.claim),
    };
    let first = supervisor.register(command.clone()).expect("register job");
    let second = supervisor.register(command).expect("duplicate shares job");
    let (first, second) = tokio::join!(first.wait(), second.wait());
    assert!(first.is_ok());
    assert!(second.is_ok());
    supervisor.drain().await;
    assert_eq!(supervisor.snapshot().active_jobs, 0);
    fixture.database.close().await.expect("database closes");
}

#[test]
fn failure_policy_keeps_wire_account_and_handoff_scopes_distinct() {
    let mut observation =
        eggpool::coordinator::FailureObservation::response(7, 1, http::StatusCode::BAD_REQUEST);
    observation.wire_rejection = true;
    assert_eq!(
        classify(&observation, RetryPolicy::default()).action,
        eggpool::coordinator::NextAction::RetryWire
    );
    observation.wire_rejection = false;
    observation.status = Some(503);
    assert_eq!(
        classify(&observation, RetryPolicy::default()).retry_scope,
        eggpool::coordinator::RetryScope::Account
    );
    observation.response_started = true;
    assert_eq!(
        classify(&observation, RetryPolicy::default()).action,
        eggpool::coordinator::NextAction::Complete
    );
}

#[test]
fn failure_policy_distinguishes_ambiguous_credentials_and_model_evidence() {
    let ambiguous = FailureObservation::response(1, 1, http::StatusCode::UNAUTHORIZED);
    let effects = classify(&ambiguous, RetryPolicy::default());
    assert_eq!(effects.account_effect, "none");
    assert!(!effects.retry);

    let explicit = FailureObservation::response(1, 1, http::StatusCode::UNAUTHORIZED)
        .signal("credential_invalid");
    let effects = classify(&explicit, RetryPolicy::default());
    assert_eq!(effects.account_effect, "disable_auth");
    assert!(effects.retry);

    let mut model =
        FailureObservation::response(1, 1, http::StatusCode::NOT_FOUND).signal("model_absent");
    model.provider_model_presence = ProviderModelPresence::Known;
    let effects = classify(&model, RetryPolicy::default());
    assert_eq!(effects.model_effect, "quarantine");
    assert_eq!(effects.wire_effect, "none");

    model.response_started = true;
    assert!(!classify(&model, RetryPolicy::default()).retry);
}

#[test]
fn effect_ledger_retirement_keeps_capacity_available() {
    let mut ledger = EffectLedger::with_capacity(2);
    assert_eq!(ledger.try_apply_once(1), Ok(true));
    assert_eq!(ledger.try_apply_once(1), Ok(false));
    assert_eq!(ledger.try_apply_once(2), Ok(true));
    assert!(ledger.try_apply_once(3).is_err());
    assert!(ledger.retire(1));
    assert_eq!(ledger.try_apply_once(3), Ok(true));
    assert_eq!(ledger.len(), 2);
}

#[tokio::test]
async fn finalization_rejects_missing_durable_identity_and_incompatible_jobs() {
    let fixture_missing = fixture().await;
    let published_missing = published(&fixture_missing, "c012-missing").await;
    let finalizer = DurableFinalizer::new(fixture_missing.database.clone());
    fixture_missing
        .database
        .call(|connection| {
            connection.execute("DELETE FROM reservations WHERE id = 1", [])?;
            connection.execute("DELETE FROM request_attempts WHERE id = 1", [])?;
            connection.execute("DELETE FROM requests WHERE id = 1", [])?;
            Ok(())
        })
        .await
        .expect("delete fixture request");
    let missing = finalizer
        .finalize_request(
            &published_missing.identity,
            FinalizationData::default(),
            None,
        )
        .await
        .expect_err("missing request is not convergence");
    assert!(matches!(
        missing,
        FinalizationError::Invariant {
            entity: "request",
            ..
        }
    ));
    fixture_missing
        .database
        .close()
        .await
        .expect("database closes");

    let fixture_attempt = fixture().await;
    let published_attempt = published(&fixture_attempt, "c013-missing-attempt").await;
    fixture_attempt
        .database
        .call(|connection| {
            connection.execute("DELETE FROM request_attempts WHERE id = 1", [])?;
            Ok(())
        })
        .await
        .expect("delete attempt");
    let missing_attempt = DurableFinalizer::new(fixture_attempt.database.clone())
        .finalize_request(
            &published_attempt.identity,
            FinalizationData::default(),
            None,
        )
        .await
        .expect_err("missing attempt is not convergence");
    assert!(matches!(
        missing_attempt,
        FinalizationError::Invariant {
            entity: "attempt",
            ..
        }
    ));
    published_attempt
        .claim
        .release_quota_reservation()
        .expect("release attempt fixture quota");
    published_attempt
        .claim
        .release_active_claim()
        .expect("release attempt fixture claim");
    fixture_attempt
        .database
        .close()
        .await
        .expect("database closes");

    let fixture_reservation = fixture().await;
    let published_reservation = published(&fixture_reservation, "c013-missing-reservation").await;
    fixture_reservation
        .database
        .call(|connection| {
            connection.execute("DELETE FROM reservations WHERE id = 1", [])?;
            Ok(())
        })
        .await
        .expect("delete reservation");
    let missing_reservation = DurableFinalizer::new(fixture_reservation.database.clone())
        .finalize_request(
            &published_reservation.identity,
            FinalizationData::default(),
            None,
        )
        .await
        .expect_err("missing reservation is not convergence");
    assert!(matches!(
        missing_reservation,
        FinalizationError::Invariant {
            entity: "reservation",
            ..
        }
    ));
    published_reservation
        .claim
        .release_quota_reservation()
        .expect("release reservation fixture quota");
    published_reservation
        .claim
        .release_active_claim()
        .expect("release reservation fixture claim");
    fixture_reservation
        .database
        .close()
        .await
        .expect("database closes");

    let fixture_conflict = fixture().await;
    let published_conflict = published(&fixture_conflict, "c012-conflict").await;
    let supervisor = FinalizationSupervisor::with_capacity(
        DurableFinalizer::new(fixture_conflict.database.clone()),
        1,
    );
    let base_data = FinalizationData {
        outcome: FinalizationOutcome::Completed,
        status_code: Some(200),
        input_tokens: 1,
        output_tokens: 2,
        cost_microdollars: 3,
        bytes_received: 4,
        bytes_emitted: 5,
        latency_ms: 6,
        upstream_request_id: Some("upstream-base".into()),
        error_class: Some("base-error".into()),
        release_reason: Some("base-release".into()),
        ..FinalizationData::default()
    };
    let first = supervisor
        .register(FinalizationCommand::Request {
            identity: published_conflict.identity.clone(),
            data: base_data.clone(),
            claim: Some(published_conflict.claim),
        })
        .expect("first command");
    let incompatible = supervisor
        .register(FinalizationCommand::Request {
            identity: published_conflict.identity.clone(),
            data: FinalizationData {
                outcome: FinalizationOutcome::ClientError,
                ..FinalizationData::default()
            },
            claim: None,
        })
        .expect_err("incompatible command must fail at registration");
    assert!(matches!(
        incompatible,
        FinalizationError::IncompatibleCommand
    ));
    let mut identity_conflict = published_conflict.identity.clone();
    identity_conflict.account_id = 99;
    let identity_incompatible = supervisor
        .register(FinalizationCommand::Request {
            identity: identity_conflict,
            data: FinalizationData {
                outcome: FinalizationOutcome::Completed,
                ..FinalizationData::default()
            },
            claim: None,
        })
        .expect_err("same key with different durable identity must not share");
    assert!(matches!(
        identity_incompatible,
        FinalizationError::IncompatibleCommand
    ));
    let incompatible_data = |mutate: fn(&mut FinalizationData)| {
        let mut data = base_data.clone();
        mutate(&mut data);
        FinalizationCommand::Request {
            identity: published_conflict.identity.clone(),
            data,
            claim: None,
        }
    };
    for command in [
        incompatible_data(|data| data.status_code = Some(201)),
        incompatible_data(|data| data.error_class = Some("different".into())),
        incompatible_data(|data| data.release_reason = Some("different".into())),
        incompatible_data(|data| data.input_tokens = 11),
        incompatible_data(|data| data.output_tokens = 12),
        incompatible_data(|data| data.cost_microdollars = 13),
        incompatible_data(|data| data.bytes_received = 14),
        incompatible_data(|data| data.bytes_emitted = 15),
        incompatible_data(|data| data.latency_ms = 16),
        incompatible_data(|data| data.upstream_request_id = Some("different".into())),
    ] {
        assert!(matches!(
            supervisor.register(command),
            Err(FinalizationError::IncompatibleCommand)
        ));
    }
    let mut proxy_incompatible = published_conflict.identity.clone();
    proxy_incompatible.proxy_request_id = "different-request".into();
    assert!(matches!(
        supervisor.register(FinalizationCommand::Request {
            identity: proxy_incompatible,
            data: base_data.clone(),
            claim: None,
        }),
        Err(FinalizationError::IncompatibleCommand)
    ));
    let mut attempt_number_incompatible = published_conflict.identity.clone();
    attempt_number_incompatible.attempt_number = 2;
    assert!(matches!(
        supervisor.register(FinalizationCommand::Request {
            identity: attempt_number_incompatible,
            data: base_data.clone(),
            claim: None,
        }),
        Err(FinalizationError::IncompatibleCommand)
    ));
    assert!(matches!(
        supervisor.register(FinalizationCommand::FailedAttempt {
            identity: published_conflict.identity.clone(),
            data: base_data,
            claim: None,
        }),
        Err(FinalizationError::IncompatibleCommand)
    ));
    first.wait().await.expect("first command completes");
    fixture_conflict
        .database
        .close()
        .await
        .expect("database closes");
}

#[tokio::test]
async fn retry_replacement_waits_for_prior_attempt_cleanup_and_converges_once() {
    let fixture = fixture().await;
    let first = published_attempt(&fixture, "c013-retry-order", 1).await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let failed = finalizer
        .finalize_failed_attempt(
            &first.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(503),
                release_reason: Some("retryable".into()),
                ..FinalizationData::default()
            },
            Some(first.claim),
        )
        .await
        .expect("first attempt cleanup");
    assert!(failed.attempt_terminal);
    assert!(!failed.request_terminal);
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    assert_eq!(
        fixture.estimator.snapshot(&["account-a".into()])["account-a"].reserved_requests,
        0
    );

    let second = published_attempt(&fixture, "c013-retry-order", 2).await;
    assert_eq!(second.identity.db_request_id, first.identity.db_request_id);
    assert_ne!(second.identity.attempt_id, first.identity.attempt_id);
    let completed = finalizer
        .finalize_request(
            &second.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            Some(second.claim),
        )
        .await
        .expect("second attempt completion");
    assert!(completed.request_terminal);
    let rows = fixture
        .database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT status FROM requests", [], |row| {
                    row.get::<_, String>(0)
                })?,
                connection.query_row("SELECT COUNT(*) FROM request_attempts", [], |row| {
                    row.get::<_, i64>(0)
                })?,
                connection.query_row(
                    "SELECT COUNT(*) FROM reservations WHERE status = 'released'",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
            ))
        })
        .await
        .expect("terminal rows");
    assert_eq!(rows, ("completed".into(), 2, 2));
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn historical_retry_finalization_uses_attempt_identity_not_parent_selection() {
    let fixture = fixture().await;
    let first = published_attempt_for_account(&fixture, "c014-history", 1, "account-a").await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let first_data = FinalizationData {
        outcome: FinalizationOutcome::UpstreamError,
        status_code: Some(503),
        error_class: Some("temporary".into()),
        bytes_received: 11,
        bytes_emitted: 7,
        latency_ms: 13,
        upstream_request_id: Some("upstream-first".into()),
        release_reason: Some("retryable".into()),
        ..FinalizationData::default()
    };
    finalizer
        .finalize_failed_attempt(&first.identity, first_data.clone(), Some(first.claim))
        .await
        .expect("first retryable attempt finalizes");

    let second = published_attempt_for_account(&fixture, "c014-history", 2, "account-b").await;
    let historical = finalizer
        .finalize_failed_attempt(&first.identity, first_data, None)
        .await
        .expect("historical retry finalization remains idempotent");
    assert!(historical.progress.completed);
    assert!(!historical.request_terminal);

    let request_id = first.identity.db_request_id;
    let second_reservation_id = second.identity.reservation_id;
    let parent = fixture
        .database
        .call(move |connection| {
            Ok((
                connection.query_row(
                    "SELECT account_id, provider_id, status FROM requests WHERE id = ?1",
                    [request_id],
                    |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, String>(2)?,
                        ))
                    },
                )?,
                connection.query_row(
                    "SELECT status FROM reservations WHERE id = ?1",
                    [second_reservation_id],
                    |row| row.get::<_, String>(0),
                )?,
            ))
        })
        .await
        .expect("historical rows");
    assert_eq!(parent.0, (2, "provider-b".into(), "pending".into()));
    assert_eq!(parent.1, "active");

    let completed = finalizer
        .finalize_request(
            &second.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            Some(second.claim),
        )
        .await
        .expect("replacement attempt completes");
    assert!(completed.progress.completed);
    let rows = fixture
        .database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT status FROM requests", [], |row| {
                    row.get::<_, String>(0)
                })?,
                connection.query_row(
                    "SELECT COUNT(*) FROM request_attempts WHERE completed_at IS NOT NULL",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM reservations WHERE status IN ('released', 'expired')",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
            ))
        })
        .await
        .expect("terminal retry rows");
    assert_eq!(rows, ("completed".into(), 2, 2));
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    assert_eq!(fixture.router.active_request_count("account-b"), 0);
    assert_eq!(
        fixture
            .estimator
            .snapshot(&["account-a".into(), "account-b".into()])["account-a"]
            .reserved_requests,
        0
    );
    assert_eq!(
        fixture
            .estimator
            .snapshot(&["account-a".into(), "account-b".into()])["account-b"]
            .reserved_requests,
        0
    );
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn replacement_claim_cannot_bypass_a_blocked_prior_publication() {
    let fixture = fixture().await;
    let barrier = Arc::new(Barrier::new(2));
    let entered = Arc::new(AtomicBool::new(false));
    let injector = PublicationFaultInjector::block_once_at(
        PublicationStage::BeforeCommit,
        Arc::clone(&barrier),
        Arc::clone(&entered),
    );
    let service = PublicationService::new(fixture.database.clone()).with_fault_injector(injector);
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    let only_account_a = BTreeSet::from(["account-b".to_owned()]);
    let first_claim = fixture
        .router
        .select_and_claim(&facts, &only_account_a)
        .await
        .expect("first claim selection")
        .expect("first claim exists");
    let task = tokio::spawn({
        let service = service.clone();
        async move {
            service
                .publish(
                    first_claim,
                    PublicationInput::new("c013-race", "openai", "openai", false, 1),
                )
                .await
        }
    });
    while !entered.load(Ordering::Acquire) {
        tokio::task::yield_now().await;
    }
    barrier.wait();
    let first = task.await.expect("publication task").expect("publication");
    let eggpool::coordinator::PublicationOutcome::Published(first) = first else {
        panic!("expected first publication");
    };
    let replacement = fixture
        .router
        .select_and_claim(&facts, &only_account_a)
        .await
        .expect("replacement selection")
        .expect("replacement claim exists for the publication gate");
    let replacement_error = service
        .publish(
            replacement,
            PublicationInput::new("c013-race", "openai", "openai", false, 2),
        )
        .await
        .expect_err("replacement publication must wait for attempt cleanup");
    assert!(matches!(
        replacement_error,
        eggpool::coordinator::PublicationError::PriorAttemptNotFinalized
    ));
    first
        .claim
        .release_active_claim()
        .expect("release blocked claim");
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    fixture.database.close().await.expect("database closes");
}
