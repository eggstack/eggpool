//! Bounded in-memory reservation registry for one-time enrollment tickets.

use std::{
    collections::HashMap,
    fmt,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use sha2::{Digest, Sha256};
use thiserror::Error;

pub const MAX_PENDING_ENROLLMENTS: usize = 64;
pub const MAX_PENDING_TTL: Duration = Duration::from_secs(10 * 60);

static NEXT_RESERVATION_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum EnrollmentRegistryError {
    #[error("enrollment ticket was already consumed")]
    Replay,
    #[error("pending enrollment capacity is full")]
    Capacity,
    #[error("enrollment owner or ticket lifetime is invalid")]
    Invalid,
}

struct EnrollmentOwner {
    session_id: String,
    user_id: String,
    expires_at: Instant,
    reservation_id: u64,
    consumed: bool,
}

#[derive(Clone, Default)]
pub struct PendingEnrollmentRegistry {
    entries: Arc<Mutex<HashMap<[u8; 32], EnrollmentOwner>>>,
}

impl fmt::Debug for PendingEnrollmentRegistry {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let count = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .len();
        formatter
            .debug_struct("PendingEnrollmentRegistry")
            .field("entry_count", &count)
            .field("entries", &"[redacted]")
            .finish()
    }
}

impl PendingEnrollmentRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Atomically reserve a digest of the bearer ticket for this initiating
    /// browser session and user. The raw ticket is never retained.
    pub fn reserve(
        &self,
        ticket: &str,
        session_id: &str,
        user_id: &str,
        ttl: Duration,
    ) -> Result<EnrollmentReservation, EnrollmentRegistryError> {
        self.reserve_at(ticket, session_id, user_id, ttl, Instant::now())
    }

    fn reserve_at(
        &self,
        ticket: &str,
        session_id: &str,
        user_id: &str,
        ttl: Duration,
        now: Instant,
    ) -> Result<EnrollmentReservation, EnrollmentRegistryError> {
        if ticket.is_empty()
            || session_id.is_empty()
            || user_id.is_empty()
            || ttl.is_zero()
            || ttl > MAX_PENDING_TTL
        {
            return Err(EnrollmentRegistryError::Invalid);
        }
        let digest: [u8; 32] = Sha256::digest(ticket.as_bytes()).into();
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        entries.retain(|_, entry| entry.expires_at > now);
        if let Some(existing) = entries.get(&digest) {
            let _same_initiator = existing.session_id == session_id && existing.user_id == user_id;
            return Err(EnrollmentRegistryError::Replay);
        }
        if entries.len() >= MAX_PENDING_ENROLLMENTS {
            return Err(EnrollmentRegistryError::Capacity);
        }
        let reservation_id = NEXT_RESERVATION_ID.fetch_add(1, Ordering::Relaxed);
        entries.insert(
            digest,
            EnrollmentOwner {
                session_id: session_id.to_owned(),
                user_id: user_id.to_owned(),
                expires_at: now + ttl,
                reservation_id,
                consumed: false,
            },
        );
        Ok(EnrollmentReservation {
            registry: self.clone(),
            digest,
            reservation_id,
            active: true,
        })
    }

    fn finish_reservation(&self, digest: &[u8; 32], reservation_id: u64) {
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(entry) = entries.get_mut(digest)
            && entry.reservation_id == reservation_id
        {
            entry.consumed = true;
        }
    }

    fn release_reservation(&self, digest: &[u8; 32], reservation_id: u64) {
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if entries
            .get(digest)
            .is_some_and(|entry| entry.reservation_id == reservation_id && !entry.consumed)
        {
            entries.remove(digest);
        }
    }

    #[cfg(test)]
    fn consume(
        &self,
        enrollment_secret: &str,
        session_id: &str,
        user_id: &str,
        ttl: Duration,
    ) -> Result<(), EnrollmentRegistryError> {
        let reservation = self.reserve(enrollment_secret, session_id, user_id, ttl)?;
        reservation.commit();
        Ok(())
    }

    #[cfg(test)]
    fn consume_at(
        &self,
        enrollment_secret: &str,
        session_id: &str,
        user_id: &str,
        ttl: Duration,
        now: Instant,
    ) -> Result<(), EnrollmentRegistryError> {
        let reservation = self.reserve_at(enrollment_secret, session_id, user_id, ttl, now)?;
        reservation.commit();
        Ok(())
    }

    #[cfg(test)]
    fn len_at(&self, now: Instant) -> usize {
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        entries.retain(|_, entry| entry.expires_at > now);
        entries.len()
    }
}

pub struct EnrollmentReservation {
    registry: PendingEnrollmentRegistry,
    digest: [u8; 32],
    reservation_id: u64,
    active: bool,
}

impl EnrollmentReservation {
    /// Permanently consume the ticket after its enrollment has been published.
    pub fn commit(mut self) {
        self.registry
            .finish_reservation(&self.digest, self.reservation_id);
        self.active = false;
    }

    /// Keep an ambiguous, non-rollback result reserved until ticket expiry.
    pub fn retain_until_expiry(mut self) {
        self.active = false;
    }
}

impl Drop for EnrollmentReservation {
    fn drop(&mut self) {
        if self.active {
            self.registry
                .release_reservation(&self.digest, self.reservation_id);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use super::{
        EnrollmentRegistryError, MAX_PENDING_ENROLLMENTS, MAX_PENDING_TTL,
        PendingEnrollmentRegistry,
    };

    #[test]
    fn consumption_is_single_use_and_session_bound_metadata_is_recorded() {
        let registry = PendingEnrollmentRegistry::new();
        let now = Instant::now();
        assert_eq!(
            registry.consume_at(
                "ticket-enroll-secret",
                "session-a",
                "user-a",
                Duration::from_secs(60),
                now
            ),
            Ok(())
        );
        assert_eq!(
            registry.consume_at(
                "ticket-enroll-secret",
                "session-a",
                "user-a",
                Duration::from_secs(60),
                now
            ),
            Err(EnrollmentRegistryError::Replay)
        );
        assert_eq!(
            registry.consume_at(
                "ticket-enroll-secret",
                "session-b",
                "user-b",
                Duration::from_secs(60),
                now
            ),
            Err(EnrollmentRegistryError::Replay)
        );
        assert_eq!(registry.len_at(now), 1);
        assert!(!format!("{registry:?}").contains("ticket-enroll-secret"));
    }

    #[test]
    fn a_rolled_back_reservation_allows_the_same_ticket_to_retry() {
        let registry = PendingEnrollmentRegistry::new();
        let ttl = Duration::from_secs(60);
        let reservation = registry
            .reserve("retry-ticket", "session", "user", ttl)
            .expect("first request reserves ticket");
        assert_eq!(
            registry
                .reserve("retry-ticket", "session", "user", ttl)
                .err(),
            Some(EnrollmentRegistryError::Replay),
            "concurrent duplicate is rejected while the reservation is active"
        );
        drop(reservation);
        registry
            .reserve("retry-ticket", "session", "user", ttl)
            .expect("rollback releases ticket for same-session retry")
            .commit();
        assert_eq!(
            registry
                .reserve("retry-ticket", "session", "user", ttl)
                .err(),
            Some(EnrollmentRegistryError::Replay),
            "committed ticket remains consumed"
        );
    }

    #[test]
    fn expired_registry_entries_are_reaped_and_capacity_is_bounded() {
        let registry = PendingEnrollmentRegistry::new();
        let now = Instant::now();
        for index in 0..MAX_PENDING_ENROLLMENTS {
            let token = format!("enroll-{index}");
            assert_eq!(
                registry.consume_at(&token, "session", "user", MAX_PENDING_TTL, now),
                Ok(())
            );
        }
        assert_eq!(registry.len_at(now), MAX_PENDING_ENROLLMENTS);
        assert_eq!(
            registry.consume_at("one-too-many", "session", "user", MAX_PENDING_TTL, now),
            Err(EnrollmentRegistryError::Capacity)
        );
        assert_eq!(
            registry.consume_at(
                "after-expiry",
                "session",
                "user",
                MAX_PENDING_TTL,
                now + MAX_PENDING_TTL,
            ),
            Ok(())
        );
        assert_eq!(registry.len_at(now + MAX_PENDING_TTL), 1);
    }

    #[test]
    fn rejects_unbounded_lifetimes_and_empty_owners() {
        let registry = PendingEnrollmentRegistry::new();
        assert_eq!(
            registry.consume("secret", "", "user", Duration::from_secs(1)),
            Err(EnrollmentRegistryError::Invalid)
        );
        assert_eq!(
            registry.consume(
                "secret",
                "session",
                "user",
                MAX_PENDING_TTL + Duration::from_secs(1)
            ),
            Err(EnrollmentRegistryError::Invalid)
        );
    }

    #[test]
    fn a_new_process_registry_does_not_retain_consumed_ticket_state() {
        let old_process = PendingEnrollmentRegistry::new();
        assert_eq!(
            old_process.consume("restart-ticket", "session", "user", Duration::from_secs(60)),
            Ok(())
        );
        drop(old_process);

        let restarted_process = PendingEnrollmentRegistry::new();
        assert_eq!(
            restarted_process.consume("restart-ticket", "session", "user", Duration::from_secs(60)),
            Ok(()),
            "replay state is intentionally memory-only and reset on restart"
        );
    }

    #[test]
    fn concurrent_consumers_have_exactly_one_winner() {
        use std::{sync::Barrier, thread};

        const CONSUMERS: usize = 8;
        let registry = PendingEnrollmentRegistry::new();
        let barrier = std::sync::Arc::new(Barrier::new(CONSUMERS));
        let results = thread::scope(|scope| {
            let handles = (0..CONSUMERS)
                .map(|index| {
                    let registry = registry.clone();
                    let barrier = std::sync::Arc::clone(&barrier);
                    scope.spawn(move || {
                        barrier.wait();
                        registry.consume(
                            "concurrent-ticket",
                            &format!("session-{index}"),
                            &format!("user-{index}"),
                            Duration::from_secs(60),
                        )
                    })
                })
                .collect::<Vec<_>>();
            handles
                .into_iter()
                .map(|handle| handle.join().expect("consumer thread joins"))
                .collect::<Vec<_>>()
        });
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            results
                .iter()
                .filter(|result| **result == Err(EnrollmentRegistryError::Replay))
                .count(),
            CONSUMERS - 1
        );
    }
}
