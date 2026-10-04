//! Test-only transaction-shape and replay model for persistence M010.
//!
//! These schemas are deliberately isolated, file-backed models of the current
//! publication/finalization write families and the proposed control/outbox
//! shape. They are not production migrations or timing qualification.

use std::path::Path;

use rusqlite::{Connection, OptionalExtension, params};
use tempfile::TempDir;
use tokio_rusqlite::rusqlite;

const BASELINE_SCHEMA: &str = r#"
PRAGMA journal_mode = WAL;
CREATE TABLE requests (
  id INTEGER PRIMARY KEY,
  proxy_request_id TEXT UNIQUE,
  account_id INTEGER NOT NULL,
  model_id TEXT NOT NULL,
  status TEXT NOT NULL,
  reserved_microdollars INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_microdollars INTEGER NOT NULL DEFAULT 0,
  error_class TEXT,
  latency_ms REAL
);
CREATE INDEX idx_requests_status ON requests(status);
CREATE INDEX idx_requests_account ON requests(account_id);
CREATE TABLE reservations (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  model_id TEXT NOT NULL,
  reserved_microdollars INTEGER NOT NULL,
  status TEXT NOT NULL
);
CREATE INDEX idx_reservations_request ON reservations(request_id);
CREATE INDEX idx_reservations_account ON reservations(account_id);
CREATE INDEX idx_reservations_model ON reservations(model_id);
CREATE INDEX idx_reservations_status ON reservations(status);
CREATE TABLE request_attempts (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL,
  attempt_number INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  status_code INTEGER,
  error_class TEXT,
  latency_ms REAL
);
CREATE INDEX idx_attempts_request ON request_attempts(request_id);
CREATE INDEX idx_attempts_account ON request_attempts(account_id);
CREATE TABLE routing_decisions (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL,
  attempt_number INTEGER NOT NULL,
  selected_account_id INTEGER,
  score_components_json TEXT NOT NULL
);
CREATE INDEX idx_routing_request ON routing_decisions(request_id);
CREATE INDEX idx_routing_model_time ON routing_decisions(attempt_number);
CREATE INDEX idx_routing_provider_time ON routing_decisions(selected_account_id);
CREATE INDEX idx_routing_selected_time ON routing_decisions(selected_account_id, attempt_number);
"#;

const CONTROL_SCHEMA: &str = r#"
PRAGMA journal_mode = WAL;
CREATE TABLE requests (
  id INTEGER PRIMARY KEY,
  proxy_request_id TEXT UNIQUE,
  account_id INTEGER NOT NULL,
  model_id TEXT NOT NULL,
  status TEXT NOT NULL,
  reserved_microdollars INTEGER NOT NULL
);
CREATE INDEX idx_control_request_status ON requests(status);
CREATE TABLE reservations (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  model_id TEXT NOT NULL,
  reserved_microdollars INTEGER NOT NULL,
  status TEXT NOT NULL
);
CREATE INDEX idx_control_reservation_status ON reservations(status);
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL,
  attempt_number INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  state TEXT NOT NULL,
  UNIQUE(request_id, attempt_number)
);
CREATE TABLE outbox (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  schema_version INTEGER NOT NULL,
  event_kind TEXT NOT NULL,
  request_id INTEGER NOT NULL,
  attempt_number INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);
"#;

const ANALYTICS_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS applied_cursor (singleton INTEGER PRIMARY KEY CHECK(singleton=1), event_id INTEGER NOT NULL);
INSERT OR IGNORE INTO applied_cursor VALUES (1, 0);
CREATE TABLE IF NOT EXISTS projection_events (
  event_id INTEGER PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  event_kind TEXT NOT NULL,
  request_id INTEGER NOT NULL,
  attempt_number INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);
"#;

fn db(dir: &TempDir, name: &str, schema: &str) -> Connection {
    let path = dir.path().join(name);
    let connection = Connection::open(path).expect("open isolated file-backed database");
    connection
        .execute_batch(schema)
        .expect("initialize test-only schema");
    connection
}

fn publish_baseline(connection: &mut Connection, id: i64) {
    let transaction = connection
        .transaction()
        .expect("begin baseline publication");
    transaction
        .execute(
            "INSERT INTO requests (id, proxy_request_id, account_id, model_id, status, reserved_microdollars) VALUES (?1, ?2, 7, 'model-x', 'pending', 100)",
            params![id, format!("proxy-{id}")],
        )
        .expect("insert baseline request");
    transaction
        .execute(
            "INSERT INTO reservations (request_id, account_id, model_id, reserved_microdollars, status) VALUES (?1, 7, 'model-x', 100, 'active')",
            [id],
        )
        .expect("insert baseline reservation");
    transaction
        .execute(
            "INSERT INTO request_attempts (request_id, attempt_number, account_id) VALUES (?1, 1, 7)",
            [id],
        )
        .expect("insert baseline attempt");
    transaction
        .execute(
            "INSERT INTO routing_decisions (request_id, attempt_number, selected_account_id, score_components_json) VALUES (?1, 1, 7, '{}')",
            [id],
        )
        .expect("insert baseline routing decision");
    transaction.commit().expect("commit baseline publication");
}

fn finalize_baseline(connection: &mut Connection, id: i64) {
    let transaction = connection
        .transaction()
        .expect("begin baseline finalization");
    transaction
        .execute(
            "UPDATE requests SET status='completed', input_tokens=11, output_tokens=13, cost_microdollars=17, latency_ms=19 WHERE id=?1",
            [id],
        )
        .expect("finalize baseline request");
    transaction
        .execute(
            "UPDATE request_attempts SET status_code=200, latency_ms=19 WHERE request_id=?1",
            [id],
        )
        .expect("finalize baseline attempt");
    transaction
        .execute(
            "UPDATE reservations SET status='released' WHERE request_id=?1 AND status='active'",
            [id],
        )
        .expect("release baseline reservation");
    transaction.commit().expect("commit baseline finalization");
}

fn publish_control(connection: &mut Connection, id: i64) {
    let transaction = connection.transaction().expect("begin control publication");
    transaction
        .execute(
            "INSERT INTO requests (id, proxy_request_id, account_id, model_id, status, reserved_microdollars) VALUES (?1, ?2, 7, 'model-x', 'pending', 100)",
            params![id, format!("proxy-{id}")],
        )
        .expect("insert control request");
    transaction
        .execute(
            "INSERT INTO reservations (request_id, account_id, model_id, reserved_microdollars, status) VALUES (?1, 7, 'model-x', 100, 'active')",
            [id],
        )
        .expect("insert control reservation");
    transaction
        .execute(
            "INSERT INTO attempts (request_id, attempt_number, account_id, state) VALUES (?1, 1, 7, 'open')",
            [id],
        )
        .expect("insert control attempt");
    transaction
        .execute(
            "INSERT INTO outbox (schema_version, event_kind, request_id, attempt_number, payload_json) VALUES (1, 'attempt_published', ?1, 1, '{\"account\":7,\"route_score\":1}')",
            [id],
        )
        .expect("append publication event atomically");
    transaction.commit().expect("commit control publication");
}

fn finalize_control(connection: &mut Connection, id: i64) {
    let transaction = connection
        .transaction()
        .expect("begin control finalization");
    transaction
        .execute(
            "UPDATE requests SET status='completed' WHERE id=?1 AND status='pending'",
            [id],
        )
        .expect("terminalize control request");
    transaction
        .execute(
            "UPDATE attempts SET state='completed' WHERE request_id=?1 AND state='open'",
            [id],
        )
        .expect("terminalize control attempt");
    transaction
        .execute(
            "UPDATE reservations SET status='released' WHERE request_id=?1 AND status='active'",
            [id],
        )
        .expect("release control reservation");
    transaction
        .execute(
            "INSERT INTO outbox (schema_version, event_kind, request_id, attempt_number, payload_json) VALUES (1, 'request_completed', ?1, 1, '{\"input_tokens\":11,\"output_tokens\":13,\"cost_microdollars\":17,\"latency_ms\":19}')",
            [id],
        )
        .expect("append terminal facts atomically");
    transaction.commit().expect("commit control finalization");
}

fn index_count(connection: &Connection) -> i64 {
    connection
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex%'",
            [],
            |row| row.get(0),
        )
        .expect("count modeled indexes")
}

fn table_rows(connection: &Connection, table: &str) -> i64 {
    connection
        .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
            row.get(0)
        })
        .expect("count model rows")
}

fn apply_next(
    control: &mut Connection,
    analytics: &mut Connection,
    stop_before_commit: bool,
) -> bool {
    let durable_cursor: i64 = analytics
        .query_row(
            "SELECT event_id FROM applied_cursor WHERE singleton=1",
            [],
            |row| row.get(0),
        )
        .expect("read durable analytics cursor before bounded fetch");
    let next: Option<(i64, i64, i64, String, String)> = control
        .query_row(
            "SELECT event_id, schema_version, request_id, event_kind, payload_json FROM outbox WHERE event_id > ?1 ORDER BY event_id LIMIT 1",
            [durable_cursor],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
        )
        .optional()
        .expect("read next bounded outbox event");
    let Some((event_id, version, request_id, kind, payload)) = next else {
        return false;
    };
    if version != 1 || !matches!(kind.as_str(), "attempt_published" | "request_completed") {
        return false;
    }
    let tx = analytics.transaction().expect("begin analytics apply");
    let cursor: i64 = tx
        .query_row(
            "SELECT event_id FROM applied_cursor WHERE singleton=1",
            [],
            |row| row.get(0),
        )
        .expect("read analytics cursor");
    if cursor != durable_cursor {
        return false;
    }
    tx.execute(
        "INSERT INTO projection_events (event_id, schema_version, event_kind, request_id, attempt_number, payload_json) VALUES (?1, ?2, ?3, ?4, 1, ?5) ON CONFLICT(event_id) DO NOTHING",
        params![event_id, version, kind, request_id, payload],
    )
    .expect("idempotently apply event");
    if stop_before_commit {
        drop(tx);
        return false;
    }
    tx.execute(
        "UPDATE applied_cursor SET event_id=?1 WHERE singleton=1 AND event_id < ?1",
        [event_id],
    )
    .expect("advance cursor in projection transaction");
    tx.commit().expect("commit projection and cursor together");
    true
}

fn database_len(path: &Path) -> u64 {
    std::fs::metadata(path).map(|m| m.len()).unwrap_or(0)
}

#[test]
fn transaction_shape_compares_index_fanout_and_file_growth() {
    let dir = tempfile::tempdir().expect("temporary prototype directory");
    let mut baseline = db(&dir, "baseline.sqlite3", BASELINE_SCHEMA);
    let mut control = db(&dir, "control.sqlite3", CONTROL_SCHEMA);
    let analytics = db(&dir, "analytics.sqlite3", ANALYTICS_SCHEMA);
    let baseline_indexes = index_count(&baseline);
    let control_indexes = index_count(&control);
    assert_eq!(baseline_indexes, 12);
    assert_eq!(control_indexes, 2);

    let baseline_path = dir.path().join("baseline.sqlite3");
    let control_path = dir.path().join("control.sqlite3");
    let baseline_before = database_len(&baseline_path);
    let control_before = database_len(&control_path);
    for id in 1..=100 {
        publish_baseline(&mut baseline, id);
        finalize_baseline(&mut baseline, id);
        publish_control(&mut control, id);
        finalize_control(&mut control, id);
    }
    let baseline_after = database_len(&baseline_path);
    let control_after = database_len(&control_path);
    let baseline_wal = database_len(&dir.path().join("baseline.sqlite3-wal"));
    let control_wal = database_len(&dir.path().join("control.sqlite3-wal"));

    assert_eq!(table_rows(&baseline, "requests"), 100);
    assert_eq!(table_rows(&baseline, "request_attempts"), 100);
    assert_eq!(table_rows(&baseline, "routing_decisions"), 100);
    assert_eq!(table_rows(&control, "outbox"), 200);
    assert_eq!(table_rows(&analytics, "projection_events"), 0);
    eprintln!(
        "M010 synthetic 100-request model: baseline_explicit_indexes={baseline_indexes}, control_explicit_indexes={control_indexes}, baseline_foreground_statements=700, candidate_foreground_statements=800, baseline_row_mutations=700, candidate_row_mutations=800, baseline_db_delta_bytes={}, baseline_wal_bytes={baseline_wal}, control_db_delta_bytes={}, control_wal_bytes={control_wal}, outbox_events=200, analytics_apply_separate=true",
        baseline_after.saturating_sub(baseline_before),
        control_after.saturating_sub(control_before),
    );
}

#[test]
fn projection_apply_is_idempotent_atomic_restartable_and_reclaimable() {
    let dir = tempfile::tempdir().expect("temporary prototype directory");
    let mut control = db(&dir, "control.sqlite3", CONTROL_SCHEMA);
    let mut analytics = db(&dir, "analytics.sqlite3", ANALYTICS_SCHEMA);
    publish_control(&mut control, 1);

    assert!(!apply_next(&mut control, &mut analytics, true));
    assert_eq!(table_rows(&analytics, "projection_events"), 0);
    assert_eq!(
        analytics
            .query_row(
                "SELECT event_id FROM applied_cursor WHERE singleton=1",
                [],
                |row| row.get::<_, i64>(0)
            )
            .expect("cursor after simulated crash"),
        0
    );

    drop(analytics);
    let mut analytics = db(&dir, "analytics.sqlite3", ANALYTICS_SCHEMA);
    assert!(apply_next(&mut control, &mut analytics, false));
    assert!(!apply_next(&mut control, &mut analytics, false));
    assert_eq!(table_rows(&analytics, "projection_events"), 1);
    let applied: i64 = analytics
        .query_row(
            "SELECT event_id FROM applied_cursor WHERE singleton=1",
            [],
            |row| row.get(0),
        )
        .expect("durable cursor after restart");
    control
        .execute("DELETE FROM outbox WHERE event_id <= ?1", [applied])
        .expect("reclaim only through durable cursor");
    assert_eq!(table_rows(&control, "outbox"), 0);
}

#[test]
fn control_correctness_survives_missing_analytics_and_poison_event_stalls_cursor() {
    let dir = tempfile::tempdir().expect("temporary prototype directory");
    let mut control = db(&dir, "control.sqlite3", CONTROL_SCHEMA);
    publish_control(&mut control, 1);
    finalize_control(&mut control, 1);
    assert_eq!(table_rows(&control, "requests"), 1);
    assert_eq!(table_rows(&control, "attempts"), 1);
    assert_eq!(table_rows(&control, "outbox"), 2);

    let mut analytics = db(&dir, "analytics.sqlite3", ANALYTICS_SCHEMA);
    control
        .execute(
            "INSERT INTO outbox (schema_version, event_kind, request_id, attempt_number, payload_json) VALUES (99, 'unsupported', 2, 1, '{}')",
            [],
        )
        .expect("inject poison event");
    assert!(apply_next(&mut control, &mut analytics, false));
    assert!(apply_next(&mut control, &mut analytics, false));
    assert!(!apply_next(&mut control, &mut analytics, false));
    // The projector stops at an unsupported event and does not move the cursor;
    // later ordered events remain pending for operator recovery.
    assert_eq!(
        analytics
            .query_row(
                "SELECT event_id FROM applied_cursor WHERE singleton=1",
                [],
                |row| row.get::<_, i64>(0)
            )
            .expect("poison does not advance cursor"),
        2
    );
    assert_eq!(table_rows(&control, "requests"), 1);
}
