//! Bounded, content-free authentication and authorization event recording.
//!
//! The request path only performs a bounded `try_send` of closed metadata. A
//! process-owned task drains the queue and writes aggregates through the
//! existing SQLite database handle.

use std::{
    collections::BTreeMap,
    fmt,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use thiserror::Error;
use tokio::sync::{Mutex, mpsc};

use crate::db::{Database, DatabaseError};

pub(crate) const CHANNEL_CAPACITY: usize = 1024;
pub(crate) const MAX_RETAINED_ROWS: usize = 10_000;
const MAX_BATCH_EVENTS: usize = CHANNEL_CAPACITY;
const RETENTION_SECONDS: i64 = 30 * 24 * 60 * 60;
const RETENTION_CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);
const SQLITE_MAX: i64 = i64::MAX;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) enum SecurityEventCode {
    SignInSucceeded,
    SignInFailed,
    SignInThrottled,
    SessionRejected,
    LegacySharedKeyRejected,
    AgentTokenRejected,
    AgentTokenThrottled,
    CsrfRejected,
    OriginRejected,
    TransportRejected,
    AdminAccessDenied,
    ScopeDenied,
}

impl SecurityEventCode {
    pub(crate) const ALL: [Self; 12] = [
        Self::SignInSucceeded,
        Self::SignInFailed,
        Self::SignInThrottled,
        Self::SessionRejected,
        Self::LegacySharedKeyRejected,
        Self::AgentTokenRejected,
        Self::AgentTokenThrottled,
        Self::CsrfRejected,
        Self::OriginRejected,
        Self::TransportRejected,
        Self::AdminAccessDenied,
        Self::ScopeDenied,
    ];

    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::SignInSucceeded => "sign_in_succeeded",
            Self::SignInFailed => "sign_in_failed",
            Self::SignInThrottled => "sign_in_throttled",
            Self::SessionRejected => "session_rejected",
            Self::LegacySharedKeyRejected => "legacy_shared_key_rejected",
            Self::AgentTokenRejected => "agent_token_rejected",
            Self::AgentTokenThrottled => "agent_token_throttled",
            Self::CsrfRejected => "csrf_rejected",
            Self::OriginRejected => "origin_rejected",
            Self::TransportRejected => "transport_rejected",
            Self::AdminAccessDenied => "admin_access_denied",
            Self::ScopeDenied => "scope_denied",
        }
    }

    pub(crate) fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|code| code.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) enum SecurityEventOutcome {
    Success,
    Refused,
    Throttled,
    Denied,
}

impl SecurityEventOutcome {
    pub(crate) const ALL: [Self; 4] = [Self::Success, Self::Refused, Self::Throttled, Self::Denied];

    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Refused => "refused",
            Self::Throttled => "throttled",
            Self::Denied => "denied",
        }
    }

    pub(crate) fn parse(value: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|outcome| outcome.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) enum SecurityPrincipalKind {
    Anonymous,
    AgentToken,
    BrowserSession,
    LegacySharedKey,
}

impl SecurityPrincipalKind {
    pub(crate) const ALL: [Self; 4] = [
        Self::Anonymous,
        Self::AgentToken,
        Self::BrowserSession,
        Self::LegacySharedKey,
    ];

    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Anonymous => "anonymous",
            Self::AgentToken => "agent_token",
            Self::BrowserSession => "browser_session",
            Self::LegacySharedKey => "legacy_shared_key",
        }
    }

    pub(crate) fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|kind| kind.as_str() == value)
    }
}

struct SecurityEvent {
    window_start: i64,
    code: SecurityEventCode,
    outcome: SecurityEventOutcome,
    principal_kind: SecurityPrincipalKind,
    user_id: Option<String>,
}

impl fmt::Debug for SecurityEvent {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SecurityEvent")
            .field("window_start", &self.window_start)
            .field("code", &self.code)
            .field("outcome", &self.outcome)
            .field("principal_kind", &self.principal_kind)
            .field("has_user_id", &self.user_id.is_some())
            .finish()
    }
}

#[derive(Clone)]
pub(crate) struct SecurityEventSink {
    sender: mpsc::Sender<SecurityEvent>,
    dropped: Arc<AtomicU64>,
}

impl fmt::Debug for SecurityEventSink {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SecurityEventSink")
            .field("capacity", &CHANNEL_CAPACITY)
            .field("dropped_events", &self.dropped_count())
            .finish()
    }
}

impl SecurityEventSink {
    pub(crate) fn record(
        &self,
        code: SecurityEventCode,
        outcome: SecurityEventOutcome,
        principal_kind: SecurityPrincipalKind,
        user_id: Option<&str>,
    ) -> bool {
        self.record_at(
            code,
            outcome,
            principal_kind,
            user_id,
            current_window_start(),
        )
    }

    fn record_at(
        &self,
        code: SecurityEventCode,
        outcome: SecurityEventOutcome,
        principal_kind: SecurityPrincipalKind,
        user_id: Option<&str>,
        window_start: i64,
    ) -> bool {
        // Keep structured event fields strictly to the closed event code and
        // outcome. Principal and internal user IDs are never logged.
        tracing::info!(
            event_code = code.as_str(),
            outcome = outcome.as_str(),
            "authentication security event"
        );
        let event = SecurityEvent {
            window_start,
            code,
            outcome,
            principal_kind,
            user_id: user_id.map(str::to_owned),
        };
        match self.sender.try_send(event) {
            Ok(()) => true,
            Err(_) => {
                saturating_increment(&self.dropped, 1);
                false
            }
        }
    }

    pub(crate) fn dropped_count(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

#[derive(Debug, Error)]
pub(crate) enum SecurityEventError {
    #[error("security event database write failed")]
    Database(#[from] DatabaseError),
}

struct EventKey {
    window_start: i64,
    code: SecurityEventCode,
    outcome: SecurityEventOutcome,
    principal_kind: SecurityPrincipalKind,
    user_id: Option<String>,
}

impl PartialEq for EventKey {
    fn eq(&self, other: &Self) -> bool {
        self.window_start == other.window_start
            && self.code == other.code
            && self.outcome == other.outcome
            && self.principal_kind == other.principal_kind
            && self.user_id == other.user_id
    }
}

impl Eq for EventKey {}

impl PartialOrd for EventKey {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for EventKey {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        (
            self.window_start,
            self.code,
            self.outcome,
            self.principal_kind,
            &self.user_id,
        )
            .cmp(&(
                other.window_start,
                other.code,
                other.outcome,
                other.principal_kind,
                &other.user_id,
            ))
    }
}

pub(crate) struct SecurityEventWriter {
    receiver: mpsc::Receiver<SecurityEvent>,
    database: Database,
    dropped: Arc<AtomicU64>,
    last_retention_prune: Option<Instant>,
}

pub(crate) fn pipeline(database: Database) -> (SecurityEventSink, Arc<Mutex<SecurityEventWriter>>) {
    let (sender, receiver) = mpsc::channel(CHANNEL_CAPACITY);
    let dropped = Arc::new(AtomicU64::new(0));
    (
        SecurityEventSink {
            sender,
            dropped: Arc::clone(&dropped),
        },
        Arc::new(Mutex::new(SecurityEventWriter {
            receiver,
            database,
            dropped,
            last_retention_prune: None,
        })),
    )
}

impl SecurityEventWriter {
    /// Drain one bounded queue batch, coalesce duplicate minute keys, and
    /// upsert the batch in one transaction. This is called only by supervised
    /// process work or the bounded shutdown flush.
    pub(crate) async fn flush_available(&mut self) -> Result<usize, SecurityEventError> {
        let mut aggregates: BTreeMap<EventKey, i64> = BTreeMap::new();
        let mut drained = 0usize;
        while drained < MAX_BATCH_EVENTS {
            let event = match self.receiver.try_recv() {
                Ok(event) => event,
                Err(mpsc::error::TryRecvError::Empty | mpsc::error::TryRecvError::Disconnected) => {
                    break;
                }
            };
            drained += 1;
            let key = EventKey {
                window_start: event.window_start,
                code: event.code,
                outcome: event.outcome,
                principal_kind: event.principal_kind,
                user_id: event.user_id,
            };
            let count = aggregates.entry(key).or_default();
            *count = count.saturating_add(1);
        }
        let prune_expired = self
            .last_retention_prune
            .is_none_or(|last| last.elapsed() >= RETENTION_CHECK_INTERVAL);
        if drained == 0 && !prune_expired {
            return Ok(0);
        }

        let flushed = drained;
        let rows: Vec<(EventKey, i64)> = aggregates.into_iter().collect();
        let retention_cutoff = unix_now().saturating_sub(RETENTION_SECONDS);
        let result = self
            .database
            .with_transaction(move |connection| {
                for (key, event_count) in rows {
                    connection.execute(
                        "INSERT INTO security_events \
                         (window_start, event_code, outcome, principal_kind, user_id, event_count) \
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6) \
                         ON CONFLICT(window_start, event_code, outcome, principal_kind, COALESCE(user_id, '')) \
                         DO UPDATE SET event_count = CASE \
                             WHEN security_events.event_count > ?7 - excluded.event_count THEN ?7 \
                             ELSE security_events.event_count + excluded.event_count END",
                        (
                            key.window_start,
                            key.code.as_str(),
                            key.outcome.as_str(),
                            key.principal_kind.as_str(),
                            key.user_id,
                            event_count,
                            SQLITE_MAX,
                        ),
                    )?;
                }
                if prune_expired {
                    connection.execute(
                        "DELETE FROM security_events WHERE window_start < ?1",
                        [retention_cutoff],
                    )?;
                }
                let row_count: i64 = connection.query_row(
                    "SELECT COUNT(*) FROM security_events",
                    [],
                    |row| row.get(0),
                )?;
                let excess = row_count.saturating_sub(MAX_RETAINED_ROWS as i64);
                if excess > 0 {
                    connection.execute(
                        "DELETE FROM security_events WHERE id IN (\
                           SELECT id FROM security_events \
                           ORDER BY window_start ASC, id ASC LIMIT ?1\
                         )",
                        [excess],
                    )?;
                }
                Ok(())
            })
            .await;
        match result {
            Ok(()) => {
                if prune_expired {
                    self.last_retention_prune = Some(Instant::now());
                }
                Ok(flushed)
            }
            Err(error) => {
                saturating_increment(&self.dropped, flushed as u64);
                Err(error.into())
            }
        }
    }
}

fn current_window_start() -> i64 {
    unix_now().div_euclid(60).saturating_mul(60)
}

fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| {
            i64::try_from(duration.as_secs()).unwrap_or(i64::MAX)
        })
}

fn saturating_increment(counter: &AtomicU64, amount: u64) {
    let _ = counter.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
        Some(value.saturating_add(amount))
    });
}

#[cfg(test)]
mod tests {
    use std::time::{Instant, SystemTime, UNIX_EPOCH};

    use crate::db::{Database, DatabaseConfig, MigrationRunner};

    use super::{
        CHANNEL_CAPACITY, MAX_RETAINED_ROWS, RETENTION_CHECK_INTERVAL, RETENTION_SECONDS,
        SecurityEventCode, SecurityEventOutcome, SecurityPrincipalKind, pipeline,
    };

    async fn database() -> (tempfile::TempDir, Database) {
        let directory = tempfile::tempdir().expect("temporary database directory");
        let database = Database::open(DatabaseConfig {
            path: directory
                .path()
                .join("security-events.sqlite3")
                .to_string_lossy()
                .into_owned(),
            ..DatabaseConfig::default()
        })
        .await
        .expect("database opens");
        MigrationRunner::new(&database)
            .run()
            .await
            .expect("schema migrates");
        (directory, database)
    }

    fn window_start() -> i64 {
        let seconds = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock is after epoch")
            .as_secs() as i64;
        seconds.div_euclid(60) * 60
    }

    #[test]
    fn event_vocabulary_round_trips_every_closed_code() {
        for code in SecurityEventCode::ALL {
            assert_eq!(SecurityEventCode::parse(code.as_str()), Some(code));
        }
        for outcome in SecurityEventOutcome::ALL {
            assert_eq!(SecurityEventOutcome::parse(outcome.as_str()), Some(outcome));
        }
        for kind in SecurityPrincipalKind::ALL {
            assert_eq!(SecurityPrincipalKind::parse(kind.as_str()), Some(kind));
        }
        assert_eq!(SecurityEventCode::parse("free-form"), None);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn duplicate_events_coalesce_by_minute_and_principal_without_debugging_ids() {
        let (_directory, database) = database().await;
        let (sink, writer) = pipeline(database.clone());
        let window = window_start();
        for _ in 0..3 {
            assert!(sink.record_at(
                SecurityEventCode::SignInFailed,
                SecurityEventOutcome::Refused,
                SecurityPrincipalKind::Anonymous,
                Some("internal-user-id-never-logged"),
                window,
            ));
        }
        assert_eq!(writer.lock().await.flush_available().await.unwrap(), 3);
        let rows: Vec<(i64, String, String, String, Option<String>, i64)> = database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT window_start, event_code, outcome, principal_kind, user_id, event_count \
                     FROM security_events",
                )?;
                statement
                    .query_map([], |row| {
                        Ok((
                            row.get(0)?,
                            row.get(1)?,
                            row.get(2)?,
                            row.get(3)?,
                            row.get(4)?,
                            row.get(5)?,
                        ))
                    })?
                    .collect::<Result<Vec<_>, _>>()
            })
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].0, window);
        assert_eq!(rows[0].1, "sign_in_failed");
        assert_eq!(rows[0].4.as_deref(), Some("internal-user-id-never-logged"));
        assert_eq!(rows[0].5, 3);
        assert_eq!(sink.dropped_count(), 0);
        database.close().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn full_channel_drops_without_blocking_and_counts_the_loss() {
        let (_directory, database) = database().await;
        let (sink, _writer) = pipeline(database.clone());
        for _ in 0..CHANNEL_CAPACITY {
            assert!(sink.record(
                SecurityEventCode::AgentTokenRejected,
                SecurityEventOutcome::Refused,
                SecurityPrincipalKind::AgentToken,
                None,
            ));
        }
        assert!(!sink.record(
            SecurityEventCode::AgentTokenRejected,
            SecurityEventOutcome::Refused,
            SecurityPrincipalKind::AgentToken,
            None,
        ));
        assert_eq!(sink.dropped_count(), 1);
        database.close().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn empty_queue_runs_due_retention_pruning() {
        let (_directory, database) = database().await;
        let (_sink, writer) = pipeline(database.clone());
        let expired = window_start() - RETENTION_SECONDS - 60;
        database
            .call(move |connection| {
                connection.execute(
                    "INSERT INTO security_events(window_start, event_code, outcome, principal_kind, user_id, event_count) VALUES (?1, 'sign_in_failed', 'refused', 'anonymous', NULL, 1)",
                    [expired],
                )?;
                Ok(())
            })
            .await
            .expect("seed expired security event");

        {
            let mut writer = writer.lock().await;
            writer.last_retention_prune = Instant::now().checked_sub(RETENTION_CHECK_INTERVAL);
            assert_eq!(writer.flush_available().await.unwrap(), 0);
        }

        let remaining: i64 = database
            .call(|connection| {
                connection.query_row("SELECT COUNT(*) FROM security_events", [], |row| row.get(0))
            })
            .await
            .expect("count retained events");
        assert_eq!(remaining, 0);
        database.close().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn expiry_and_row_cap_are_applied_by_the_coalescing_writer() {
        let (_directory, database) = database().await;
        let (sink, writer) = pipeline(database.clone());
        let now = window_start();
        let expired = now - (30 * 24 * 60 * 60) - 60;
        database
            .call(move |connection| {
                connection.execute(
                    "INSERT INTO security_events(window_start, event_code, outcome, principal_kind, user_id, event_count) \
                     VALUES (?1, 'sign_in_failed', 'refused', 'anonymous', NULL, 1)",
                    [expired],
                )?;
                for index in 0..MAX_RETAINED_ROWS as i64 {
                    connection.execute(
                        "INSERT INTO security_events(window_start, event_code, outcome, principal_kind, user_id, event_count) \
                         VALUES (?1, 'agent_token_rejected', 'refused', 'agent_token', ?2, 1)",
                        (now, format!("user-{index}")),
                    )?;
                }
                Ok(())
            })
            .await
            .unwrap();
        assert!(sink.record_at(
            SecurityEventCode::ScopeDenied,
            SecurityEventOutcome::Denied,
            SecurityPrincipalKind::AgentToken,
            Some("newest-user"),
            now,
        ));
        writer.lock().await.flush_available().await.unwrap();
        let (count, expired_count, newest_count): (i64, i64, i64) = database
            .call(move |connection| {
                Ok((
                    connection
                        .query_row("SELECT COUNT(*) FROM security_events", [], |row| row.get(0))?,
                    connection.query_row(
                        "SELECT COUNT(*) FROM security_events WHERE window_start = ?1",
                        [expired],
                        |row| row.get(0),
                    )?,
                    connection.query_row(
                        "SELECT event_count FROM security_events WHERE user_id = 'newest-user'",
                        [],
                        |row| row.get(0),
                    )?,
                ))
            })
            .await
            .unwrap();
        assert_eq!(count, MAX_RETAINED_ROWS as i64);
        assert_eq!(expired_count, 0);
        assert_eq!(newest_count, 1);
        database.close().await.unwrap();
    }
}
