//! Bounded process-owned metrics aggregation for the O007 flush task.
//!
//! The request path only hands this boundary scalar, already-redacted facts.
//! It never stores bodies, headers, credentials, or arbitrary diagnostic text.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

use thiserror::Error;

use crate::db::{Database, DatabaseError};

const SQLITE_MAX: i64 = i64::MAX;
type MetricKey = (String, i64, String, String, i64, String, bool, String);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageMetricEvent {
    pub bucket_start: String,
    pub bucket_size_s: i64,
    pub provider_id: String,
    pub model_id: String,
    pub account_id: i64,
    pub protocol: String,
    pub streamed: bool,
    pub status: String,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read_tokens: i64,
    pub cache_write_tokens: i64,
    pub reasoning_tokens: i64,
    pub thinking_characters: i64,
    pub cost_microdollars: i64,
    pub bytes_received: i64,
    pub bytes_emitted: i64,
    pub latency_ms: i64,
    pub first_byte_ms: Option<i64>,
    pub retry_count: i64,
}

impl UsageMetricEvent {
    pub fn now(
        provider_id: impl Into<String>,
        model_id: impl Into<String>,
        account_id: i64,
        protocol: impl Into<String>,
        status: impl Into<String>,
    ) -> Self {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |value| value.as_secs());
        let bucket_size_s = 300_u64;
        let bucket_start = format_utc_timestamp(
            i64::try_from(timestamp / bucket_size_s * bucket_size_s).unwrap_or(i64::MAX),
        );
        Self {
            bucket_start,
            bucket_size_s: bucket_size_s as i64,
            provider_id: provider_id.into(),
            model_id: model_id.into(),
            account_id,
            protocol: protocol.into(),
            streamed: false,
            status: status.into(),
            input_tokens: 0,
            output_tokens: 0,
            cache_read_tokens: 0,
            cache_write_tokens: 0,
            reasoning_tokens: 0,
            thinking_characters: 0,
            cost_microdollars: 0,
            bytes_received: 0,
            bytes_emitted: 0,
            latency_ms: 0,
            first_byte_ms: None,
            retry_count: 0,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct Aggregate {
    request_count: i64,
    error_count: i64,
    retry_count: i64,
    input_tokens: i64,
    output_tokens: i64,
    cache_read_tokens: i64,
    cache_write_tokens: i64,
    reasoning_tokens: i64,
    thinking_characters: i64,
    cost_microdollars: i64,
    bytes_received: i64,
    bytes_emitted: i64,
    latency_ms_sum: i64,
    latency_ms_min: Option<i64>,
    latency_ms_max: Option<i64>,
    first_byte_ms_sum: i64,
    first_byte_ms_count: i64,
}

impl Aggregate {
    fn add(&mut self, event: &UsageMetricEvent) {
        self.request_count = add(self.request_count, 1);
        if event.status == "error" {
            self.error_count = add(self.error_count, 1);
        }
        self.retry_count = add(self.retry_count, non_negative(event.retry_count));
        self.input_tokens = add(self.input_tokens, non_negative(event.input_tokens));
        self.output_tokens = add(self.output_tokens, non_negative(event.output_tokens));
        self.cache_read_tokens = add(
            self.cache_read_tokens,
            non_negative(event.cache_read_tokens),
        );
        self.cache_write_tokens = add(
            self.cache_write_tokens,
            non_negative(event.cache_write_tokens),
        );
        self.reasoning_tokens = add(self.reasoning_tokens, non_negative(event.reasoning_tokens));
        self.thinking_characters = add(
            self.thinking_characters,
            non_negative(event.thinking_characters),
        );
        self.cost_microdollars = add(
            self.cost_microdollars,
            non_negative(event.cost_microdollars),
        );
        self.bytes_received = add(self.bytes_received, non_negative(event.bytes_received));
        self.bytes_emitted = add(self.bytes_emitted, non_negative(event.bytes_emitted));
        self.latency_ms_sum = add(self.latency_ms_sum, non_negative(event.latency_ms));
        self.latency_ms_min = Some(
            self.latency_ms_min
                .map_or(non_negative(event.latency_ms), |v| {
                    v.min(non_negative(event.latency_ms))
                }),
        );
        self.latency_ms_max = Some(
            self.latency_ms_max
                .map_or(non_negative(event.latency_ms), |v| {
                    v.max(non_negative(event.latency_ms))
                }),
        );
        if let Some(first_byte_ms) = event.first_byte_ms {
            self.first_byte_ms_sum = add(self.first_byte_ms_sum, non_negative(first_byte_ms));
            self.first_byte_ms_count = add(self.first_byte_ms_count, 1);
        }
    }

    fn merge(&mut self, other: Self) {
        self.request_count = add(self.request_count, other.request_count);
        self.error_count = add(self.error_count, other.error_count);
        self.retry_count = add(self.retry_count, other.retry_count);
        self.input_tokens = add(self.input_tokens, other.input_tokens);
        self.output_tokens = add(self.output_tokens, other.output_tokens);
        self.cache_read_tokens = add(self.cache_read_tokens, other.cache_read_tokens);
        self.cache_write_tokens = add(self.cache_write_tokens, other.cache_write_tokens);
        self.reasoning_tokens = add(self.reasoning_tokens, other.reasoning_tokens);
        self.thinking_characters = add(self.thinking_characters, other.thinking_characters);
        self.cost_microdollars = add(self.cost_microdollars, other.cost_microdollars);
        self.bytes_received = add(self.bytes_received, other.bytes_received);
        self.bytes_emitted = add(self.bytes_emitted, other.bytes_emitted);
        self.latency_ms_sum = add(self.latency_ms_sum, other.latency_ms_sum);
        self.latency_ms_min = match (self.latency_ms_min, other.latency_ms_min) {
            (Some(left), Some(right)) => Some(left.min(right)),
            (left, right) => left.or(right),
        };
        self.latency_ms_max = match (self.latency_ms_max, other.latency_ms_max) {
            (Some(left), Some(right)) => Some(left.max(right)),
            (left, right) => left.or(right),
        };
        self.first_byte_ms_sum = add(self.first_byte_ms_sum, other.first_byte_ms_sum);
        self.first_byte_ms_count = add(self.first_byte_ms_count, other.first_byte_ms_count);
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, serde::Serialize)]
pub struct MetricsSnapshot {
    pub buffered_events: usize,
    pub buffered_rows: usize,
    pub total_received: u64,
    pub total_flushed: u64,
    pub total_dropped: u64,
    pub flush_failures: u64,
    pub last_flush_rows: usize,
}

#[derive(Debug, Error)]
pub enum MetricsFlushError {
    #[error("metrics flush database operation failed: {0}")]
    Database(#[from] DatabaseError),
}

#[derive(Debug)]
struct State {
    buffer: BTreeMap<MetricKey, Aggregate>,
    pending_events: usize,
    total_received: u64,
    total_flushed: u64,
    total_dropped: u64,
    flush_failures: u64,
    last_flush_rows: usize,
}

#[derive(Clone)]
pub struct MetricsWriteCoalescer {
    inner: Arc<Mutex<State>>,
    flush_lock: Arc<tokio::sync::Mutex<()>>,
    database: Database,
    max_rows: usize,
    max_pending_events: usize,
    write_mode: String,
    bucket_size_s: i64,
}

impl std::fmt::Debug for MetricsWriteCoalescer {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("MetricsWriteCoalescer")
            .field("snapshot", &self.snapshot())
            .finish()
    }
}

impl MetricsWriteCoalescer {
    pub fn new(config: &crate::config::MetricsConfig, database: Database) -> Self {
        Self {
            inner: Arc::new(Mutex::new(State {
                buffer: BTreeMap::new(),
                pending_events: 0,
                total_received: 0,
                total_flushed: 0,
                total_dropped: 0,
                flush_failures: 0,
                last_flush_rows: 0,
            })),
            flush_lock: Arc::new(tokio::sync::Mutex::new(())),
            database,
            max_rows: usize::try_from(config.max_buffered_events.max(1)).unwrap_or(usize::MAX),
            max_pending_events: usize::try_from(
                config.max_buffered_events.max(1).saturating_mul(64),
            )
            .unwrap_or(usize::MAX),
            write_mode: config.write_mode.clone(),
            bucket_size_s: i64::try_from(config.timeseries_bucket_s.max(1)).unwrap_or(i64::MAX),
        }
    }

    pub fn write_mode(&self) -> &str {
        &self.write_mode
    }

    /// Record one already-redacted event according to the configured write
    /// mode. Immediate mode uses the same coalescer and transaction boundary,
    /// but flushes before returning; buffered modes only enqueue the event.
    pub async fn record_usage_async(
        &self,
        event: UsageMetricEvent,
    ) -> Result<bool, MetricsFlushError> {
        if !self.record_buffered(event) {
            return Ok(false);
        }
        if self.write_mode == "immediate" {
            self.flush().await?;
        }
        Ok(true)
    }

    pub fn record_usage(&self, event: UsageMetricEvent) -> bool {
        if self.write_mode == "immediate" {
            return false;
        }
        self.record_buffered(event)
    }

    fn record_buffered(&self, mut event: UsageMetricEvent) -> bool {
        if let Ok(timestamp) = event.bucket_start.parse::<i64>() {
            let bucket_size_s = self.bucket_size_s;
            event.bucket_start =
                format_utc_timestamp(timestamp.div_euclid(bucket_size_s) * bucket_size_s);
            event.bucket_size_s = bucket_size_s;
        } else if let Some(canonical) = canonical_bucket_start(&event.bucket_start) {
            event.bucket_start = canonical;
            event.bucket_size_s = self.bucket_size_s;
        }
        let mut state = self.inner.lock().expect("metrics state lock");
        let key = (
            event.bucket_start.clone(),
            event.bucket_size_s,
            event.provider_id.clone(),
            event.model_id.clone(),
            event.account_id,
            event.protocol.clone(),
            event.streamed,
            event.status.clone(),
        );
        if state.pending_events >= self.max_pending_events
            || (state.buffer.len() >= self.max_rows && !state.buffer.contains_key(&key))
        {
            state.total_dropped = state.total_dropped.saturating_add(1);
            return false;
        }
        state.buffer.entry(key).or_default().add(&event);
        state.pending_events = state.pending_events.saturating_add(1);
        state.total_received = state.total_received.saturating_add(1);
        true
    }

    pub fn snapshot(&self) -> MetricsSnapshot {
        let state = self.inner.lock().expect("metrics state lock");
        MetricsSnapshot {
            buffered_events: state.pending_events,
            buffered_rows: state.buffer.len(),
            total_received: state.total_received,
            total_flushed: state.total_flushed,
            total_dropped: state.total_dropped,
            flush_failures: state.flush_failures,
            last_flush_rows: state.last_flush_rows,
        }
    }

    pub async fn flush(&self) -> Result<usize, MetricsFlushError> {
        let _guard = self.flush_lock.lock().await;
        let batch = {
            let mut state = self.inner.lock().expect("metrics state lock");
            let batch = std::mem::take(&mut state.buffer);
            state.pending_events = 0;
            batch
        };
        if batch.is_empty() {
            return Ok(0);
        }
        let rows = batch
            .iter()
            .map(
                |(
                    (
                        bucket_start,
                        bucket_size_s,
                        provider_id,
                        model_id,
                        account_id,
                        protocol,
                        streamed,
                        status,
                    ),
                    aggregate,
                )| {
                    (
                        bucket_start.clone(),
                        *bucket_size_s,
                        provider_id.clone(),
                        model_id.clone(),
                        *account_id,
                        protocol.clone(),
                        i64::from(*streamed),
                        status.clone(),
                        *aggregate,
                    )
                },
            )
            .collect::<Vec<_>>();
        let row_count = rows.len();
        let flushed_events = rows
            .iter()
            .map(|(_, _, _, _, _, _, _, _, row)| row.request_count as u64)
            .sum::<u64>();
        let transaction_rows = rows.clone();
        let result = self.database.with_transaction(move |connection| {
            for (bucket_start, bucket_size_s, provider_id, model_id, account_id, protocol, streamed, status, row) in &transaction_rows {
                connection.execute(
                    "INSERT INTO usage_rollups (bucket_start,bucket_size_s,provider_id,model_id,account_id,protocol,streamed,status,request_count,error_count,retry_count,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,thinking_characters,cost_microdollars,bytes_received,bytes_emitted,latency_ms_sum,latency_ms_min,latency_ms_max,first_byte_ms_sum,first_byte_ms_count) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25) ON CONFLICT(bucket_start,bucket_size_s,provider_id,model_id,account_id,protocol,streamed,status) DO UPDATE SET request_count=request_count+excluded.request_count,error_count=error_count+excluded.error_count,retry_count=retry_count+excluded.retry_count,input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens,reasoning_tokens=reasoning_tokens+excluded.reasoning_tokens,thinking_characters=thinking_characters+excluded.thinking_characters,cost_microdollars=MIN(?,cost_microdollars+excluded.cost_microdollars),bytes_received=bytes_received+excluded.bytes_received,bytes_emitted=bytes_emitted+excluded.bytes_emitted,latency_ms_sum=latency_ms_sum+excluded.latency_ms_sum,latency_ms_min=CASE WHEN excluded.latency_ms_min IS NULL THEN latency_ms_min WHEN latency_ms_min IS NULL THEN excluded.latency_ms_min ELSE MIN(latency_ms_min,excluded.latency_ms_min) END,latency_ms_max=CASE WHEN excluded.latency_ms_max IS NULL THEN latency_ms_max WHEN latency_ms_max IS NULL THEN excluded.latency_ms_max ELSE MAX(latency_ms_max,excluded.latency_ms_max) END,first_byte_ms_sum=first_byte_ms_sum+excluded.first_byte_ms_sum,first_byte_ms_count=first_byte_ms_count+excluded.first_byte_ms_count,updated_at=CURRENT_TIMESTAMP",
                    tokio_rusqlite::rusqlite::params![bucket_start,bucket_size_s,provider_id,model_id,account_id,protocol,streamed,status,row.request_count,row.error_count,row.retry_count,row.input_tokens,row.output_tokens,row.cache_read_tokens,row.cache_write_tokens,row.reasoning_tokens,row.thinking_characters,row.cost_microdollars,row.bytes_received,row.bytes_emitted,row.latency_ms_sum,row.latency_ms_min,row.latency_ms_max,row.first_byte_ms_sum,row.first_byte_ms_count,SQLITE_MAX],
                )?;
            }
            Ok(())
        }).await;
        match result {
            Ok(()) => {
                let mut state = self.inner.lock().expect("metrics state lock");
                let count = row_count;
                state.total_flushed = state.total_flushed.saturating_add(flushed_events);
                state.last_flush_rows = count;
                Ok(count)
            }
            Err(error) => {
                let mut state = self.inner.lock().expect("metrics state lock");
                state.flush_failures = state.flush_failures.saturating_add(1);
                for (
                    bucket_start,
                    bucket_size_s,
                    provider_id,
                    model_id,
                    account_id,
                    protocol,
                    streamed,
                    status,
                    aggregate,
                ) in rows
                {
                    let event_count = aggregate.request_count.max(0) as usize;
                    let key = (
                        bucket_start.clone(),
                        bucket_size_s,
                        provider_id.clone(),
                        model_id.clone(),
                        account_id,
                        protocol.clone(),
                        streamed != 0,
                        status.clone(),
                    );
                    if state.pending_events.saturating_add(event_count) > self.max_pending_events
                        || (state.buffer.len() >= self.max_rows && !state.buffer.contains_key(&key))
                    {
                        state.total_dropped =
                            state.total_dropped.saturating_add(event_count as u64);
                        continue;
                    }
                    state.buffer.entry(key).or_default().merge(aggregate);
                    state.pending_events = state.pending_events.saturating_add(event_count);
                }
                Err(MetricsFlushError::Database(error))
            }
        }
    }
}

fn non_negative(value: i64) -> i64 {
    value.max(0)
}
fn add(left: i64, right: i64) -> i64 {
    left.saturating_add(right)
}

fn canonical_bucket_start(value: &str) -> Option<String> {
    let value = value.trim();
    let (date, time) = value.split_once('T')?;
    let time = time.strip_suffix('Z').unwrap_or(time);
    if date.len() == 10 && time.len() >= 8 {
        Some(format!("{date} {}", &time[..8]))
    } else {
        None
    }
}

/// Format a Unix timestamp as the UTC shape used by the canonical SQLite
/// schema. This keeps rollup keys comparable with `started_at` filters without
/// adding a date/time dependency to the Rust candidate.
fn format_utc_timestamp(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    let day_seconds = seconds.rem_euclid(86_400);
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 }.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096).div_euclid(365);
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2).div_euclid(153);
    let day = doy - (153 * mp + 2).div_euclid(5) + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = year + i64::from(month <= 2);
    let hour = day_seconds / 3_600;
    let minute = day_seconds % 3_600 / 60;
    let second = day_seconds % 60;
    format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}")
}
