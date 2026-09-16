//! Explicit transaction state machine.
//!
//! ```text
//! Decoded -> RemoteProfileValidated -> ClientDetected -> MutationPlanned
//!   -> BackupCommitted -> ConfigWritten -> LocalParseValidated
//!   -> ClientNativeValidated -> Committed
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
        if next <= self.phase {
            return Err(ConnectError::Io {
                detail: format!(
                    "transaction moved backwards from {} to {}",
                    self.phase.name(),
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
        assert!(transaction.advance(Phase::Decoded).is_err());
        transaction.advance(Phase::Committed).expect("commit");
        assert_eq!(transaction.phase(), Phase::Committed);
    }
}
