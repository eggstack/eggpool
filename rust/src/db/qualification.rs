//! Feature-gated, bounded SQLite phase evidence for physical qualification.
//!
//! This module intentionally has no production configuration or persistence
//! surface.  It retains only bounded scalar timing and pragma facts in memory.

use std::collections::VecDeque;
use std::sync::{
    Mutex,
    atomic::{AtomicU64, Ordering},
};

use serde::Serialize;

use super::connection::TransactionKind;

pub(crate) const RECORD_CAPACITY: usize = 256;
pub(crate) const SCHEMA_VERSION: &str = "sqlite-db-phase.v1";
#[cfg(feature = "qualification-persist-journal")]
pub(crate) const WORKER_IO_HISTOGRAM_UPPER_BOUNDS_BYTES: [Option<u64>; 8] = [
    Some(0),
    Some(4096),
    Some(16_384),
    Some(65_536),
    Some(262_144),
    Some(1_048_576),
    Some(4_194_304),
    None,
];

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct QualificationEffectivePragmas {
    pub journal_mode: String,
    pub synchronous: String,
    pub page_size: u64,
    pub wal_autocheckpoint_pages: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct QualificationTransactionRecord {
    pub record_seq: u64,
    pub kind: String,
    pub gate_wait_us: u64,
    pub worker_queue_us: u64,
    pub begin_us: u64,
    pub body_us: u64,
    pub commit_us: Option<u64>,
    pub worker_return_us: u64,
    pub total_us: u64,
    pub success: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct QualificationDbSnapshot {
    pub schema_version: String,
    pub collector_capacity: usize,
    pub effective: QualificationEffectivePragmas,
    pub latest_record_seq: u64,
    pub records: Vec<QualificationTransactionRecord>,
    pub dropped_records: u64,
    #[cfg(feature = "qualification-persist-journal")]
    pub worker_io_attribution: QualificationWorkerIoAttribution,
    pub checkpoint_maintenance: QualificationCheckpointMaintenance,
    #[cfg(feature = "qualification-dedicated-checkpointer")]
    pub dedicated_checkpointer: QualificationDedicatedCheckpointer,
}

#[cfg(feature = "qualification-persist-journal")]
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub struct QualificationWorkerIoBucket {
    pub count: u64,
    pub sum_write_bytes: u64,
    pub max_write_bytes: u64,
    pub histogram: [u64; 8],
}

#[cfg(feature = "qualification-persist-journal")]
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct QualificationWorkerIoAttribution {
    pub enabled: bool,
    pub failed: bool,
    pub histogram_upper_bounds_bytes: [Option<u64>; 8],
    pub publication: QualificationWorkerIoBucket,
    pub finalization: QualificationWorkerIoBucket,
    pub other: QualificationWorkerIoBucket,
}

#[cfg(feature = "qualification-dedicated-checkpointer")]
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct QualificationDedicatedCheckpointer {
    pub enabled: bool,
    pub primary_wal_autocheckpoint_pages: u64,
    pub dedicated_journal_mode: Option<String>,
    pub dedicated_synchronous: Option<String>,
    pub dedicated_wal_autocheckpoint_pages: Option<u64>,
    pub event_wakes: u64,
    pub noop_observations: u64,
    pub passive_attempts: u64,
    pub passive_progress: u64,
    pub passive_completed: u64,
    pub busy_or_incomplete: u64,
    pub failures: u64,
    pub max_log_frames: u64,
    pub max_checkpointed_frames: u64,
    pub last_log_frames: u64,
    pub last_checkpointed_frames: u64,
    pub elapsed_count: u64,
    pub elapsed_max_us: u64,
    pub elapsed_p95_upper_bound_us: Option<u64>,
    pub close_result: Option<String>,
    pub process_thread_count: Option<u64>,
}

/// Bounded, scalar-only maintenance checkpoint evidence (persistence M001).
/// No SQL text, path, or request detail ever crosses this boundary.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct QualificationCheckpointMaintenance {
    pub soft_threshold_frames: u32,
    pub not_due: u64,
    pub gate_busy: u64,
    pub below_threshold: u64,
    pub checkpointed: u64,
    pub failures: u64,
    pub last_log_frames: u64,
    pub last_checkpointed_frames: u64,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct QualificationRecordInput {
    pub(crate) kind: TransactionKind,
    pub(crate) gate_wait_us: u64,
    pub(crate) worker_queue_us: u64,
    pub(crate) begin_us: u64,
    pub(crate) body_us: u64,
    pub(crate) commit_us: Option<u64>,
    pub(crate) worker_return_us: u64,
    pub(crate) total_us: u64,
    pub(crate) success: bool,
}

#[derive(Debug)]
struct CollectorState {
    effective: Option<QualificationEffectivePragmas>,
    next_record_seq: u64,
    records: VecDeque<QualificationTransactionRecord>,
    #[cfg(feature = "qualification-persist-journal")]
    worker_io_attribution: QualificationWorkerIoAttribution,
}

#[derive(Debug)]
pub(crate) struct QualificationCollector {
    state: Mutex<CollectorState>,
    dropped_records: AtomicU64,
    #[cfg(feature = "qualification-persist-journal")]
    worker_io_failure: AtomicU64,
}

impl QualificationCollector {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(CollectorState {
                effective: None,
                next_record_seq: 0,
                records: VecDeque::with_capacity(RECORD_CAPACITY),
                #[cfg(feature = "qualification-persist-journal")]
                worker_io_attribution: QualificationWorkerIoAttribution {
                    histogram_upper_bounds_bytes: WORKER_IO_HISTOGRAM_UPPER_BOUNDS_BYTES,
                    ..QualificationWorkerIoAttribution::default()
                },
            }),
            dropped_records: AtomicU64::new(0),
            #[cfg(feature = "qualification-persist-journal")]
            worker_io_failure: AtomicU64::new(0),
        }
    }

    pub(crate) fn set_effective(&self, effective: QualificationEffectivePragmas) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .effective = Some(effective);
    }

    pub(crate) fn record(&self, input: QualificationRecordInput) {
        // Recording is deliberately best-effort: a concurrent read of the
        // authenticated projection must never make a transaction wait.
        // Contended drops are counted so sequence gaps are explainable.
        let Ok(mut state) = self.state.try_lock() else {
            self.dropped_records.fetch_add(1, Ordering::Relaxed);
            return;
        };
        state.next_record_seq = state.next_record_seq.saturating_add(1);
        let record_seq = state.next_record_seq;
        if state.records.len() == RECORD_CAPACITY {
            state.records.pop_front();
        }
        state.records.push_back(QualificationTransactionRecord {
            record_seq,
            kind: input.kind.as_str().to_owned(),
            gate_wait_us: input.gate_wait_us,
            worker_queue_us: input.worker_queue_us,
            begin_us: input.begin_us,
            body_us: input.body_us,
            commit_us: input.commit_us,
            worker_return_us: input.worker_return_us,
            total_us: input.total_us,
            success: input.success,
        });
    }

    #[cfg(feature = "qualification-persist-journal")]
    pub(crate) fn enable_worker_io_attribution(&self) {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .worker_io_attribution
            .enabled = true;
    }

    #[cfg(feature = "qualification-persist-journal")]
    pub(crate) fn record_worker_io(&self, kind: TransactionKind, delta: u64) {
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let bucket = match kind {
            TransactionKind::Publication => &mut state.worker_io_attribution.publication,
            TransactionKind::Finalization => &mut state.worker_io_attribution.finalization,
            TransactionKind::Other => &mut state.worker_io_attribution.other,
        };
        bucket.count = bucket.count.saturating_add(1);
        bucket.sum_write_bytes = bucket.sum_write_bytes.saturating_add(delta);
        bucket.max_write_bytes = bucket.max_write_bytes.max(delta);
        let index = WORKER_IO_HISTOGRAM_UPPER_BOUNDS_BYTES
            .iter()
            .position(|bound| bound.is_some_and(|bound| delta <= bound))
            .unwrap_or(7);
        bucket.histogram[index] = bucket.histogram[index].saturating_add(1);
    }

    #[cfg(feature = "qualification-persist-journal")]
    #[cfg(feature = "qualification-persist-journal")]
    pub(crate) fn record_worker_io_failure(&self) {
        self.worker_io_failure.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn snapshot(&self) -> Option<QualificationDbSnapshot> {
        // Poison indicates a prior holder panicked: rebuild rather than hide
        // effective pragmas. `None` is reserved for "pragmas not captured yet".
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        Some(QualificationDbSnapshot {
            schema_version: SCHEMA_VERSION.to_owned(),
            collector_capacity: RECORD_CAPACITY,
            effective: state.effective.clone()?,
            latest_record_seq: state.next_record_seq,
            records: state.records.iter().cloned().collect(),
            dropped_records: self.dropped_records.load(Ordering::Relaxed),
            #[cfg(feature = "qualification-persist-journal")]
            #[cfg(feature = "qualification-persist-journal")]
            worker_io_attribution: QualificationWorkerIoAttribution {
                failed: self.worker_io_failure.load(Ordering::Relaxed) > 0,
                ..state.worker_io_attribution.clone()
            },
            checkpoint_maintenance: QualificationCheckpointMaintenance {
                soft_threshold_frames: 0,
                not_due: 0,
                gate_busy: 0,
                below_threshold: 0,
                checkpointed: 0,
                failures: 0,
                last_log_frames: 0,
                last_checkpointed_frames: 0,
            },
            #[cfg(feature = "qualification-dedicated-checkpointer")]
            dedicated_checkpointer: QualificationDedicatedCheckpointer {
                enabled: false,
                primary_wal_autocheckpoint_pages: 0,
                dedicated_journal_mode: None,
                dedicated_synchronous: None,
                dedicated_wal_autocheckpoint_pages: None,
                event_wakes: 0,
                noop_observations: 0,
                passive_attempts: 0,
                passive_progress: 0,
                passive_completed: 0,
                busy_or_incomplete: 0,
                failures: 0,
                max_log_frames: 0,
                max_checkpointed_frames: 0,
                last_log_frames: 0,
                last_checkpointed_frames: 0,
                elapsed_count: 0,
                elapsed_max_us: 0,
                elapsed_p95_upper_bound_us: None,
                close_result: None,
                process_thread_count: None,
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_records_evict_oldest_and_keep_monotonic_sequence() {
        let collector = QualificationCollector::new();
        collector.set_effective(QualificationEffectivePragmas {
            journal_mode: "wal".to_owned(),
            synchronous: "NORMAL".to_owned(),
            page_size: 4096,
            wal_autocheckpoint_pages: 1000,
        });
        for _ in 0..(RECORD_CAPACITY + 2) {
            collector.record(QualificationRecordInput {
                kind: TransactionKind::Other,
                gate_wait_us: 1,
                worker_queue_us: 2,
                begin_us: 3,
                body_us: 4,
                commit_us: Some(5),
                worker_return_us: 6,
                total_us: 7,
                success: true,
            });
        }
        let snapshot = collector
            .snapshot()
            .expect("pragmas captured before snapshot");
        assert_eq!(snapshot.collector_capacity, RECORD_CAPACITY);
        assert_eq!(snapshot.latest_record_seq, (RECORD_CAPACITY + 2) as u64);
        assert_eq!(snapshot.records.len(), RECORD_CAPACITY);
        assert_eq!(snapshot.records[0].record_seq, 3);
        assert_eq!(snapshot.records[RECORD_CAPACITY - 1].record_seq, 258);
    }

    #[test]
    fn rollback_records_do_not_fabricate_commit_duration() {
        let collector = QualificationCollector::new();
        collector.set_effective(QualificationEffectivePragmas {
            journal_mode: "wal".to_owned(),
            synchronous: "NORMAL".to_owned(),
            page_size: 4096,
            wal_autocheckpoint_pages: 1000,
        });
        collector.record(QualificationRecordInput {
            kind: TransactionKind::Finalization,
            gate_wait_us: 1,
            worker_queue_us: 2,
            begin_us: 3,
            body_us: 4,
            commit_us: None,
            worker_return_us: 5,
            total_us: 6,
            success: false,
        });
        let record = &collector
            .snapshot()
            .expect("pragmas captured before snapshot")
            .records[0];
        assert_eq!(record.kind, "finalization");
        assert_eq!(record.commit_us, None);
        assert!(!record.success);
    }

    #[cfg(feature = "qualification-persist-journal")]
    #[test]
    fn worker_io_aggregates_are_bounded_by_transaction_kind() {
        let collector = QualificationCollector::new();
        collector.enable_worker_io_attribution();
        collector.record_worker_io(TransactionKind::Publication, 12);
        collector.record_worker_io(TransactionKind::Publication, 4096);
        collector.record_worker_io(TransactionKind::Finalization, 20);
        collector.set_effective(QualificationEffectivePragmas {
            journal_mode: "persist".to_owned(),
            synchronous: "EXTRA".to_owned(),
            page_size: 4096,
            wal_autocheckpoint_pages: 1000,
        });
        let snapshot = collector.snapshot().expect("effective pragmas exist");
        assert_eq!(snapshot.worker_io_attribution.publication.count, 2);
        assert_eq!(
            snapshot.worker_io_attribution.publication.sum_write_bytes,
            4108
        );
        assert_eq!(
            snapshot.worker_io_attribution.publication.max_write_bytes,
            4096
        );
        assert_eq!(snapshot.worker_io_attribution.publication.histogram[1], 2);
        assert_eq!(snapshot.worker_io_attribution.finalization.count, 1);
        assert!(snapshot.records.is_empty());
    }
}
