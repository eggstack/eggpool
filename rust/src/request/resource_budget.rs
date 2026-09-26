//! Process-local admission budget for retained raw inference request bodies.

use std::sync::OnceLock;
use std::sync::atomic::{AtomicUsize, Ordering};

pub(crate) const MIN_RAW_BODY_BUDGET_BYTES: usize = 64 * 1024 * 1024;
static IN_USE_RAW_BODY_BYTES: OnceLock<AtomicUsize> = OnceLock::new();

fn in_use() -> &'static AtomicUsize {
    IN_USE_RAW_BODY_BYTES.get_or_init(|| AtomicUsize::new(0))
}

pub(crate) fn effective_ceiling(live_limit: usize) -> usize {
    MIN_RAW_BODY_BUDGET_BYTES.max(live_limit)
}

pub(crate) struct RawBodyReservation {
    amount: usize,
}

impl RawBodyReservation {
    pub(crate) fn try_acquire(amount: usize, ceiling: usize) -> Option<Self> {
        let counter = in_use();
        let mut current = counter.load(Ordering::Acquire);
        loop {
            let next = current.checked_add(amount)?;
            if next > ceiling {
                return None;
            }
            match counter.compare_exchange_weak(current, next, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => return Some(Self { amount }),
                Err(observed) => current = observed,
            }
        }
    }

    #[cfg(test)]
    fn in_use_for_test() -> usize {
        in_use().load(Ordering::Acquire)
    }
}

impl Drop for RawBodyReservation {
    fn drop(&mut self) {
        let prior = in_use().fetch_sub(self.amount, Ordering::AcqRel);
        debug_assert!(prior >= self.amount);
    }
}

#[cfg(test)]
mod tests {
    use super::{MIN_RAW_BODY_BUDGET_BYTES, RawBodyReservation, effective_ceiling};

    #[test]
    fn effective_budget_has_floor_and_tracks_large_live_limits() {
        assert_eq!(effective_ceiling(0), MIN_RAW_BODY_BUDGET_BYTES);
        assert_eq!(effective_ceiling(1), 64 * 1024 * 1024);
        assert_eq!(effective_ceiling(1024 * 1024 * 1024), 1024 * 1024 * 1024);
    }

    #[test]
    fn reservations_are_bounded_and_drop_releases_exactly_once() {
        let baseline = RawBodyReservation::in_use_for_test();
        let first = RawBodyReservation::try_acquire(40, baseline + 64).expect("reservation");
        let second = RawBodyReservation::try_acquire(24, baseline + 64).expect("reservation");
        assert!(RawBodyReservation::try_acquire(1, baseline + 64).is_none());
        assert_eq!(RawBodyReservation::in_use_for_test(), baseline + 64);
        drop(first);
        assert_eq!(RawBodyReservation::in_use_for_test(), baseline + 24);
        drop(second);
        assert_eq!(RawBodyReservation::in_use_for_test(), baseline);
    }

    #[test]
    fn zero_and_overflow_reservations_are_safe() {
        let baseline = RawBodyReservation::in_use_for_test();
        let zero = RawBodyReservation::try_acquire(0, baseline).expect("zero reservation");
        let one = RawBodyReservation::try_acquire(1, baseline + 1).expect("one byte");
        assert!(RawBodyReservation::try_acquire(usize::MAX, usize::MAX).is_none());
        drop(zero);
        drop(one);
        assert_eq!(RawBodyReservation::in_use_for_test(), baseline);
    }
}
