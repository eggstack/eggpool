use std::time::Duration;

use eggpool::coordinator::{FailureObservation, RetryPolicy, classify, parse_retry_after};

#[test]
fn c014_retry_after_numeric_and_http_dates_share_the_configured_cap() {
    let policy = RetryPolicy::default();
    let now = 1_700_000_000;

    assert_eq!(
        parse_retry_after("12", now, policy),
        Some(Duration::from_secs(12))
    );
    assert_eq!(
        parse_retry_after("999999", now, policy),
        Some(Duration::from_secs(1_800))
    );
    assert_eq!(
        parse_retry_after("Tue, 14 Nov 2023 22:15:20 GMT", now, policy),
        Some(Duration::from_secs(120))
    );
    assert_eq!(
        parse_retry_after("Sun, 14 Nov 2027 22:13:32 GMT", now, policy),
        Some(Duration::from_secs(1_800))
    );
    assert_eq!(
        parse_retry_after("Tue, 14 Nov 2023 22:13:20 GMT", now, policy),
        Some(Duration::ZERO)
    );
    assert_eq!(
        parse_retry_after("Tue, 14 Nov 2023 22:13:19 GMT", now, policy),
        None
    );
    assert_eq!(parse_retry_after("not-a-date", now, policy), None);
    assert_eq!(parse_retry_after("-1", now, policy), None);
    assert_eq!(parse_retry_after("1.5", now, policy), None);
}

#[test]
fn c014_retry_after_uses_a_custom_policy_cap_for_http_dates() {
    let policy = RetryPolicy {
        max_attempts: 3,
        max_retry_after: Duration::from_secs(30),
    };
    assert_eq!(
        parse_retry_after("Tue, 14 Nov 2023 22:15:20 GMT", 1_700_000_000, policy),
        Some(Duration::from_secs(30))
    );
    let mut observation = FailureObservation::response(1, 1, http::StatusCode::TOO_MANY_REQUESTS);
    observation.retry_after = Some(Duration::from_secs(120));
    let effects = classify(&observation, policy);
    assert_eq!(effects.retry_after, Some(Duration::from_secs(30)));
    assert_eq!(effects.backoff_until, Some(Duration::from_secs(30)));
}
