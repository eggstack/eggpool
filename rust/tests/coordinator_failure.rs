//! Retry-hint parsing guards for the coordinator failure classifier.
//!
//! A `Retry-After` HTTP-date whose clock field is out of range carries no
//! usable hint. It must leave the fixed policy backoff in charge instead of
//! producing a negative or otherwise wrong retry duration, and it must never
//! turn a previously accepted request into a hard error.

use std::time::Duration;

use eggpool::{
    coordinator::{
        FailureObservation, NextAction, RetryPolicy, classify, parse_retry_after,
        retry_after_from_headers,
    },
    health::{BackoffReason, compute_backoff_seconds},
};

/// The epoch second of the fixture HTTP-date, `2035-01-01T00:00:00Z`.
const FIXTURE_DATE_EPOCH: i64 = 2_051_222_400;

/// A "now" two days before the fixture date.
///
/// A malformed clock field shifts the parsed instant earlier than the
/// nominal date (an hour for a negative hour), so "now" has to sit more than
/// an hour before the nominal date for the malformed value to have produced
/// any hint at all.
const FIXTURE_NOW: i64 = FIXTURE_DATE_EPOCH - 2 * 86_400;

/// The fixed policy backoff that stays in charge when no hint is usable.
const RATE_LIMIT_POLICY_BACKOFF_SECONDS: f64 = 60.0;

#[test]
fn out_of_range_retry_after_clock_fields_are_not_a_retry_hint() {
    let policy = RetryPolicy::default();
    for value in [
        "Mon, 01 Jan 2035 -1:00:00 GMT",
        "Mon, 01 Jan 2035 00:-1:00 GMT",
        "Mon, 01 Jan 2035 00:00:-1 GMT",
    ] {
        assert_eq!(
            parse_retry_after(value, FIXTURE_NOW, policy),
            None,
            "{value} must not be accepted as a retry hint"
        );
        let mut headers = http::HeaderMap::new();
        headers.insert(
            http::header::RETRY_AFTER,
            http::HeaderValue::from_static(value),
        );
        assert_eq!(
            retry_after_from_headers(&headers, policy),
            None,
            "{value} must not be accepted as a retry hint"
        );
    }
}

#[test]
fn an_unusable_retry_hint_leaves_the_fixed_policy_backoff_in_charge() {
    let policy = RetryPolicy::default();
    let hint = parse_retry_after("Mon, 01 Jan 2035 -1:00:00 GMT", FIXTURE_NOW, policy);
    assert_eq!(hint, None);

    let mut malformed = FailureObservation::response(1, 1, http::StatusCode::TOO_MANY_REQUESTS);
    malformed.retry_after = hint;
    let mut absent = FailureObservation::response(2, 1, http::StatusCode::TOO_MANY_REQUESTS);
    absent.retry_after = None;

    let malformed_effects = classify(&malformed, policy);
    let absent_effects = classify(&absent, policy);
    assert_eq!(malformed_effects.backoff_until, None);
    assert_eq!(
        malformed_effects.backoff_until,
        absent_effects.backoff_until
    );
    assert_eq!(
        malformed_effects.backoff_reason.as_deref(),
        Some("rate_limited")
    );
    assert_eq!(malformed_effects.action, absent_effects.action);
    assert_eq!(malformed_effects.action, NextAction::RetryAccount);
    // The request still succeeds through the fallback path.
    assert!(malformed_effects.retry);
    assert_eq!(
        compute_backoff_seconds(BackoffReason::RateLimited, 1, None, false),
        Some(RATE_LIMIT_POLICY_BACKOFF_SECONDS)
    );
}

#[test]
fn a_valid_http_date_still_produces_the_same_retry_hint() {
    let policy = RetryPolicy::default();
    assert_eq!(
        parse_retry_after(
            "Mon, 01 Jan 2035 00:00:00 GMT",
            FIXTURE_DATE_EPOCH - 120,
            policy
        ),
        Some(Duration::from_secs(120))
    );
    // The bounded ceiling and the elapsed-date behavior are unchanged.
    assert_eq!(
        parse_retry_after(
            "Mon, 01 Jan 2035 00:00:00 GMT",
            FIXTURE_DATE_EPOCH - 7_200,
            policy
        ),
        Some(Duration::from_secs(1_800))
    );
    assert_eq!(
        parse_retry_after(
            "Mon, 01 Jan 2035 00:00:00 GMT",
            FIXTURE_DATE_EPOCH + 60,
            policy
        ),
        None
    );
    // The header path resolves against the wall clock, so an elapsed date is
    // the stable observable there.
    let mut headers = http::HeaderMap::new();
    headers.insert(
        http::header::RETRY_AFTER,
        http::HeaderValue::from_static("Mon, 01 Jan 2000 00:00:00 GMT"),
    );
    assert_eq!(retry_after_from_headers(&headers, policy), None);
}

#[test]
fn a_numeric_retry_hint_is_unchanged() {
    let policy = RetryPolicy::default();
    assert_eq!(
        parse_retry_after("12", FIXTURE_NOW, policy),
        Some(Duration::from_secs(12))
    );
    assert_eq!(
        parse_retry_after("999999", FIXTURE_NOW, policy),
        Some(Duration::from_secs(1_800))
    );
    assert_eq!(parse_retry_after("-1", FIXTURE_NOW, policy), None);
}
