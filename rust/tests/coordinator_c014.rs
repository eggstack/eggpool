use std::time::Duration;

use eggpool::coordinator::{
    FailureObservation, RetryPolicy, classify, parse_retry_after, retry_after_from_headers,
};

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

#[test]
fn c014_provider_retry_after_header_sizes_the_rate_limit_cooldown() {
    // The classifier already consumed a provider `Retry-After` hint, but
    // nothing ever put one on the observation, so every 429 fell back to no
    // cooldown at all and routing re-selected the exhausted account.
    let policy = RetryPolicy::default();
    let mut headers = http::HeaderMap::new();
    headers.insert(
        http::header::RETRY_AFTER,
        http::HeaderValue::from_static("300"),
    );
    let mut observation = FailureObservation::response(1, 1, http::StatusCode::TOO_MANY_REQUESTS);
    observation.retry_after = retry_after_from_headers(&headers, policy);
    assert_eq!(observation.retry_after, Some(Duration::from_secs(300)));
    let effects = classify(&observation, policy);
    assert_eq!(effects.backoff_until, Some(Duration::from_secs(300)));
    assert_eq!(effects.retry_after, Some(Duration::from_secs(300)));

    // An over-long hint is capped by policy, and an HTTP-date hint resolves
    // against the current clock.
    headers.insert(
        http::header::RETRY_AFTER,
        http::HeaderValue::from_static("999999"),
    );
    assert_eq!(
        retry_after_from_headers(&headers, policy),
        Some(Duration::from_secs(1_800))
    );
    headers.insert(
        http::header::RETRY_AFTER,
        http::HeaderValue::from_static("Sun, 14 Nov 2099 22:13:32 GMT"),
    );
    assert_eq!(
        retry_after_from_headers(&headers, policy),
        Some(Duration::from_secs(1_800))
    );
    headers.insert(
        http::header::RETRY_AFTER,
        http::HeaderValue::from_static("Tue, 14 Nov 2000 22:13:32 GMT"),
    );
    assert_eq!(retry_after_from_headers(&headers, policy), None);

    // Absent, malformed, and negative hints leave the fixed policy in charge.
    for value in ["not-a-date", "-1", "1.5"] {
        headers.insert(
            http::header::RETRY_AFTER,
            http::HeaderValue::from_static(value),
        );
        assert_eq!(retry_after_from_headers(&headers, policy), None, "{value}");
    }
    let mut without = http::HeaderMap::new();
    without.insert(
        "content-type",
        http::HeaderValue::from_static("application/json"),
    );
    assert_eq!(retry_after_from_headers(&without, policy), None);
    let unannotated = FailureObservation::response(1, 1, http::StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(classify(&unannotated, policy).backoff_until, None);
}
