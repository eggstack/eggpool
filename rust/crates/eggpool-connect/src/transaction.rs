//! Explicit transaction state machine.
//!
//! ```text
//! Decoded -> RemoteProfileValidated -> ClientDetected -> MutationPlanned
//!   -> BackupCommitted -> ConfigWritten -> LocalParseValidated
//!   -> ClientNativeValidated -> Committed
//!
//! No-op shortcut (already-installed plan, no backup/write):
//!   MutationPlanned -> LocalParseValidated
//! ```
//!
//! Before `BackupCommitted` no target client file may change. On failure
//! after `BackupCommitted` the helper attempts automatic restoration and
//! reports both the original failure and the rollback result. When rollback
//! itself fails, recovery metadata (backup ID/path, never contents or
//! secrets) is preserved in a distinct high-severity error.

use crate::outcome::ConnectError;

/// Required mutation phases in order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Phase {
    Decoded,
    RemoteProfileValidated,
    ClientDetected,
    MutationPlanned,
    BackupCommitted,
    ConfigWritten,
    LocalParseValidated,
    ClientNativeValidated,
    Committed,
}

impl Phase {
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Decoded => "decoded",
            Self::RemoteProfileValidated => "remote-profile-validated",
            Self::ClientDetected => "client-detected",
            Self::MutationPlanned => "mutation-planned",
            Self::BackupCommitted => "backup-committed",
            Self::ConfigWritten => "config-written",
            Self::LocalParseValidated => "local-parse-validated",
            Self::ClientNativeValidated => "client-native-validated",
            Self::Committed => "committed",
        }
    }

    /// True once a backup exists and rollback is required on failure.
    #[must_use]
    pub const fn rollback_required(self) -> bool {
        matches!(
            self,
            Self::BackupCommitted
                | Self::ConfigWritten
                | Self::LocalParseValidated
                | Self::ClientNativeValidated
        )
    }
}

/// Tracks the current phase so failures map to refusal vs. rollback.
#[derive(Debug)]
pub struct Transaction {
    phase: Phase,
}

impl Transaction {
    #[must_use]
    pub const fn new() -> Self {
        Self {
            phase: Phase::Decoded,
        }
    }

    pub fn advance(&mut self, next: Phase) -> Result<(), ConnectError> {
        // Strict succession: skipping phases (e.g. Decoded->Committed
        // bypassing backup) is rejected. The sole allowed skip is the
        // no-op shortcut MutationPlanned -> LocalParseValidated when the
        // plan is already installed (no backup/write occurs).
        if self.phase == Phase::Committed {
            return Err(ConnectError::Io {
                detail: "transaction is already committed".to_owned(),
            });
        }
        let allowed = matches!(
            (self.phase, next),
            (Phase::Decoded, Phase::RemoteProfileValidated)
                | (Phase::RemoteProfileValidated, Phase::ClientDetected)
                | (Phase::ClientDetected, Phase::MutationPlanned)
                | (Phase::MutationPlanned, Phase::BackupCommitted)
                | (Phase::MutationPlanned, Phase::LocalParseValidated)
                | (Phase::BackupCommitted, Phase::ConfigWritten)
                | (Phase::ConfigWritten, Phase::LocalParseValidated)
                | (Phase::LocalParseValidated, Phase::ClientNativeValidated)
                | (Phase::ClientNativeValidated, Phase::Committed)
        );
        if !allowed {
            let expected = match self.phase {
                Phase::Decoded => "remote-profile-validated",
                Phase::RemoteProfileValidated => "client-detected",
                Phase::ClientDetected => "mutation-planned",
                Phase::MutationPlanned => "backup-committed (or local-parse-validated for no-op)",
                Phase::BackupCommitted => "config-written",
                Phase::ConfigWritten => "local-parse-validated",
                Phase::LocalParseValidated => "client-native-validated",
                Phase::ClientNativeValidated => "committed",
                Phase::Committed => "committed",
            };
            return Err(ConnectError::Io {
                detail: format!(
                    "transaction must advance from {} to {}, not {}",
                    self.phase.name(),
                    expected,
                    next.name()
                ),
            });
        }
        self.phase = next;
        Ok(())
    }

    #[must_use]
    pub const fn phase(&self) -> Phase {
        self.phase
    }
}

impl Default for Transaction {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phases_advance_in_order_and_gate_rollback() {
        // Mutating path: full strict chain.
        let mut transaction = Transaction::new();
        assert!(!transaction.phase().rollback_required());
        for next in [
            Phase::RemoteProfileValidated,
            Phase::ClientDetected,
            Phase::MutationPlanned,
            Phase::BackupCommitted,
        ] {
            transaction.advance(next).expect("advance");
        }
        assert!(transaction.phase().rollback_required());
        // Backwards moves and validation-skipping jumps stay rejected.
        assert!(transaction.advance(Phase::Decoded).is_err());
        assert!(transaction.advance(Phase::Committed).is_err());
        for next in [
            Phase::ConfigWritten,
            Phase::LocalParseValidated,
            Phase::ClientNativeValidated,
            Phase::Committed,
        ] {
            transaction.advance(next).expect("advance");
        }
        assert_eq!(transaction.phase(), Phase::Committed);
        assert!(!transaction.phase().rollback_required());
        assert!(transaction.advance(Phase::Committed).is_err());

        // No-op path: MutationPlanned -> LocalParseValidated shortcut.
        let mut noop = Transaction::new();
        for next in [
            Phase::RemoteProfileValidated,
            Phase::ClientDetected,
            Phase::MutationPlanned,
        ] {
            noop.advance(next).expect("advance");
        }
        noop.advance(Phase::LocalParseValidated)
            .expect("no-op shortcut");
        noop.advance(Phase::ClientNativeValidated).expect("advance");
        noop.advance(Phase::Committed).expect("commit");
        assert_eq!(noop.phase(), Phase::Committed);

        // Other skips stay rejected.
        let mut bad = Transaction::new();
        assert!(bad.advance(Phase::Committed).is_err());
        bad.advance(Phase::RemoteProfileValidated).expect("advance");
        assert!(bad.advance(Phase::MutationPlanned).is_err());
    }
}
