//! Feature-gated, bounded SQLite phase evidence for physical qualification.
//!
//! This module intentionally has no production configuration or persistence
//! surface.  It retains only bounded scalar timing and pragma facts in memory.

use std::collections::VecDeque;
use std::sync::Mutex;

use serde::Serialize;

use super::connection::TransactionKind;

pub(crate) const RECORD_CAPACITY: usize = 256;
pub(crate) const SCHEMA_VERSION: &str = "sqlite-db-phase.v1";

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
    pub checkpoint_maintenance: QualificationCheckpointMaintenance,
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
}

#[derive(Debug)]
pub(crate) struct QualificationCollector {
    state: Mutex<CollectorState>,
}

impl QualificationCollector {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(CollectorState {
                effective: None,
                next_record_seq: 0,
                records: VecDeque::with_capacity(RECORD_CAPACITY),
            }),
        }
    }

    pub(crate) fn set_effective(&self, effective: QualificationEffectivePragmas) {
        self.state
            .lock()
            .expect("qualification collector lock")
            .effective = Some(effective);
    }

    pub(crate) fn record(&self, input: QualificationRecordInput) {
        // Recording is deliberately best-effort: a concurrent read of the
        // authenticated projection must never make a transaction wait.
        let Ok(mut state) = self.state.try_lock() else {
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

    pub(crate) fn snapshot(&self) -> QualificationDbSnapshot {
        let state = self.state.lock().expect("qualification collector lock");
        QualificationDbSnapshot {
            schema_version: SCHEMA_VERSION.to_owned(),
            collector_capacity: RECORD_CAPACITY,
            effective: state
                .effective
                .clone()
                .expect("qualification pragmas captured during database configure"),
            latest_record_seq: state.next_record_seq,
            records: state.records.iter().cloned().collect(),
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
        }
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
        let snapshot = collector.snapshot();
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
        let record = &collector.snapshot().records[0];
        assert_eq!(record.kind, "finalization");
        assert_eq!(record.commit_us, None);
        assert!(!record.success);
    }
}
