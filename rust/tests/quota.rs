use std::{
    collections::BTreeMap,
    fs,
    path::PathBuf,
    sync::{Arc, Barrier},
    thread,
    time::{SystemTime, UNIX_EPOCH},
};

use eggpool::{
    db::{Database, DatabaseConfig, MigrationRunner, UsageWindowRepository},
    quota::{
        AccountQuota, DEFAULT_REQUEST_CAPACITY_5H, PersistedWindowSnapshot, QuotaEstimator,
        QuotaFairScorer, QuotaPolicy, QuotaWindow,
    },
};

fn policy() -> QuotaPolicy {
    QuotaPolicy {
        capacity_5h_requests: Some(20),
        capacity_7d_requests: Some(100),
        capacity_30d_requests: Some(200),
        capacity_5h_tokens: Some(2_000),
        capacity_7d_tokens: Some(10_000),
        capacity_30d_tokens: Some(20_000),
        ..QuotaPolicy::default()
    }
}

#[test]
fn d001_quota_score_uses_request_and_token_pressure_not_cost() {
    let estimator = QuotaEstimator::new([
        AccountQuota {
            account_name: "account-a".into(),
            weight: 2.0,
            policy: policy(),
            persisted_snapshot: Some(PersistedWindowSnapshot {
                account_id: 1,
                cost_5h: 999_999,
                cost_7d: 0,
                cost_30d: 0,
                request_count_5h: 10,
                request_count_7d: 20,
                request_count_30d: 30,
                token_count_5h: 1_000,
                token_count_7d: 2_000,
                token_count_30d: 3_000,
                loaded_at: 0.0,
            }),
            ..AccountQuota::new("account-a")
        },
        AccountQuota {
            account_name: "account-b".into(),
            policy: policy(),
            persisted_snapshot: Some(PersistedWindowSnapshot {
                account_id: 2,
                cost_5h: 1,
                cost_7d: 0,
                cost_30d: 0,
                request_count_5h: 1,
                request_count_7d: 2,
                request_count_30d: 3,
                token_count_5h: 100,
                token_count_7d: 200,
                token_count_30d: 300,
                loaded_at: 0.0,
            }),
            ..AccountQuota::new("account-b")
        },
    ]);
    estimator
        .add_pending_claim("account-a", 50, 123)
        .expect("pending claim publishes");
    let names = ["account-a".to_owned(), "account-b".to_owned()];
    let scores = QuotaFairScorer::default().score_accounts(
        &estimator,
        &names,
        &BTreeMap::new(),
        &BTreeMap::from([("account-a".to_owned(), 200), ("account-b".to_owned(), 200)]),
        &BTreeMap::new(),
    );
    assert!((scores[0].quota_score - 0.3378125).abs() < 1e-12);
    assert!((scores[1].quota_score - 0.16075).abs() < 1e-12);
    assert_eq!(scores[0].cost_5h_microdollars, 999_999);
    assert_eq!(
        QuotaFairScorer::default().rank_accounts(scores)[0].account_name,
        "account-b"
    );
}

#[test]
fn defaults_weight_offsets_and_hard_cap_boundaries_are_explicit() {
    let estimator = QuotaEstimator::default();
    estimator
        .configure_policy(
            "account",
            1.0,
            QuotaPolicy {
                capacity_5h_requests: Some(1),
                request_offset_5h: 0,
                ..QuotaPolicy::default()
            },
        )
        .expect("valid policy");
    estimator.set_persisted_snapshot(
        "account",
        PersistedWindowSnapshot {
            account_id: 1,
            request_count_5h: 1,
            loaded_at: 0.0,
            ..PersistedWindowSnapshot::empty(1, 0.0)
        },
    );
    let names = vec!["account".to_owned()];
    let score = QuotaFairScorer::default().score_accounts(
        &estimator,
        &names,
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
    )[0]
    .clone();
    assert_eq!(score.capacity_5h_requests, 1);
    assert_eq!(score.capacity_7d_requests, DEFAULT_REQUEST_CAPACITY_5H * 14);
    assert!(score.is_eligible); // local quota is score-only
    assert!(
        !estimator
            .get_account_quota("account")
            .expect("account")
            .is_within_limits(0.0)
    ); // exact capacity is exhausted for hard-cap callers
}

#[test]
fn pending_claim_conversion_and_underflow_preserve_ownership() {
    let estimator = QuotaEstimator::default();
    estimator
        .add_pending_claim("account", 128, 500)
        .expect("claim");
    let snapshot = estimator.snapshot(&["account".into()])["account"].clone();
    assert_eq!(snapshot.pending_requests, 1);
    assert_eq!(snapshot.pending_tokens, 128);
    assert_eq!(snapshot.pending_cost, 500);
    assert_eq!(snapshot.quota.reserved_requests, 1);
    estimator
        .convert_pending_claim("account", 128, 500)
        .expect("conversion");
    let converted = estimator.snapshot(&["account".into()])["account"].clone();
    assert_eq!(converted.pending_requests, 0);
    assert_eq!(converted.reserved_requests, 1);
    assert_eq!(converted.quota.reserved_tokens, 128);
    assert!(
        estimator
            .release_pending_claim("account", 128, 500)
            .is_err()
    );
    estimator
        .remove_reservation("account", 1, 128, 500)
        .expect("durable mirror release clamps");
    let released = estimator.snapshot(&["account".into()])["account"].clone();
    assert_eq!(released.quota.reserved_requests, 0);
}

#[test]
fn concurrent_claim_publication_is_visible_at_the_snapshot_boundary() {
    let estimator = QuotaEstimator::default();
    let barrier = Arc::new(Barrier::new(2));
    let publisher = estimator.clone();
    let publisher_barrier = barrier.clone();
    let handle = thread::spawn(move || {
        publisher
            .add_pending_claim("account", 128, 500)
            .expect("claim publishes");
        publisher_barrier.wait();
    });
    barrier.wait();
    let snapshot = estimator.snapshot(&["account".into()])["account"].clone();
    handle.join().expect("publisher joins");
    assert_eq!(snapshot.pending_requests, 1);
    assert_eq!(snapshot.pending_tokens, 128);
    assert_eq!(snapshot.quota.reserved_requests, 1);
}

#[test]
fn malformed_capacity_is_ineligible_and_window_backfill_is_ordered() {
    let estimator = QuotaEstimator::new([AccountQuota {
        account_name: "bad".into(),
        policy: QuotaPolicy {
            capacity_5h_requests: Some(0),
            ..QuotaPolicy::default()
        },
        ..AccountQuota::new("bad")
    }]);
    let score = QuotaFairScorer::default().score_accounts(
        &estimator,
        &["bad".into()],
        &BTreeMap::new(),
        &BTreeMap::new(),
        &BTreeMap::new(),
    )[0]
    .clone();
    assert!(!score.is_eligible);

    let mut window = QuotaWindow::new(100);
    window.add_observation(20.0, 2, 3);
    window.add_observation(10.0, 5, 7);
    assert_eq!(window.usage(20.0), (7, 10));
    assert_eq!(window.usage(121.0), (0, 0));
}

#[test]
fn ewma_estimates_are_lru_bounded_and_hierarchy_is_safe() {
    let estimator = QuotaEstimator::default();
    for _ in 0..5 {
        estimator.record_usage("account", 100, 100, Some("model"), 1.0);
    }
    assert_eq!(estimator.estimate_cost("account", "model", 100, 1.0), 114);
    estimator.set_model_override("override", 2.0, 4.0);
    assert_eq!(
        estimator.estimate_cost("account", "override", 100, 1.0),
        345
    );
    assert!(estimator.estimate_cost("account", "unknown", 100, 1.0) > 0);

    let names = (0..4_200).map(|index| format!("account-{index}"));
    for name in names {
        estimator.record_usage(&name, 1, 1, Some(&name), 1.0);
    }
    // The public cap is the effective account-bucket bound; churn cannot
    // make the estimator grow without limit.
    let (account_cap, bucket_cap, global_cap) = estimator.ewma_sizes();
    assert!(account_cap <= 4_096);
    assert!(bucket_cap <= 4_096);
    assert!(global_cap <= 1_024);
}

#[tokio::test(flavor = "current_thread")]
async fn usage_hydration_reads_schema54_rows_in_one_batch() {
    let path = std::env::temp_dir().join(format!(
        "eggpool-d004-{}-{}.sqlite3",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    let database = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../migration-rs/fixtures/routing-domain/schema54-routing-domain-seed.sql"
            ))
        })
        .await
        .expect("seed");
    let before = database.stats().calls;
    let rows = UsageWindowRepository::new(&database)
        .get_all_usage_windows("2026-09-04 00:00:00")
        .await
        .expect("usage query");
    assert_eq!(rows[&1].request_count_5h, 1);
    assert_eq!(rows[&1].token_count_5h, 150);
    assert!(!rows.contains_key(&2)); // the pending request is excluded
    assert_eq!(database.stats().calls - before, 1);
    database.close().await.expect("database closes");
    fs::remove_file(PathBuf::from(&path)).expect("temporary database removed");
}
