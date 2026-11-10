//! Bounded statement cache for publication and finalization SQL.
//!
//! `Connection::execute` and `query_row` prepare a fresh statement on every
//! call. Those transactions, including the budget helpers they call, prepare
//! through the worker connection's statement cache instead. SQL text and
//! bound parameters stay the same. The cache is memory on that connection
//! and is empty after every process start.

use tokio_rusqlite::rusqlite::{CachedStatement, Connection, Params, Row};

/// Distinct statements prepared inside `TransactionKind::Publication` and
/// `TransactionKind::Finalization`, including user and instance budget SQL.
/// `hot_statement_census_fits_the_cache` fails when a newly listed statement
/// would exceed this constant.
pub(crate) const HOT_STATEMENT_CACHE_CAPACITY: usize = 49;

pub(crate) const HOT_STATEMENTS: &[&str] = &[
    "INSERT INTO instance_budget_reservations(\n            request_id, model_id, policy_version, day_start, month_start,\n            held_tokens, held_microdollars, status, expires_at,\n            enforcement, bound_tokens, bound_microdollars, price_source)\n          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'active',\n                  datetime('now', '+' || ?8 || ' seconds'), ?9, ?10, ?11, ?12)",
    "INSERT INTO instance_budget_usage(\n                model_id, window, window_start, request_count, token_count,\n                cost_microdollars, provider_reported_microdollars,\n                local_estimated_microdollars, reservation_fallback_microdollars,\n                unknown_token_requests, unknown_cost_requests)\n              VALUES (?1, ?2, ?3, 1, ?4, ?5, ?6, ?7, ?8, ?9, ?10)\n              ON CONFLICT(model_id, window, window_start) DO UPDATE SET\n                 request_count = instance_budget_usage.request_count + 1,\n                 token_count = instance_budget_usage.token_count + excluded.token_count,\n                 cost_microdollars = instance_budget_usage.cost_microdollars + excluded.cost_microdollars,\n                 provider_reported_microdollars = instance_budget_usage.provider_reported_microdollars + excluded.provider_reported_microdollars,\n                 local_estimated_microdollars = instance_budget_usage.local_estimated_microdollars + excluded.local_estimated_microdollars,\n                 reservation_fallback_microdollars = instance_budget_usage.reservation_fallback_microdollars + excluded.reservation_fallback_microdollars,\n                 unknown_token_requests = instance_budget_usage.unknown_token_requests + excluded.unknown_token_requests,\n                 unknown_cost_requests = instance_budget_usage.unknown_cost_requests + excluded.unknown_cost_requests,\n                 updated_at = CURRENT_TIMESTAMP",
    "INSERT INTO request_attempts\n(request_id, attempt_number, account_id, provider_id, model_id, protocol, streamed)\nVALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
    "INSERT INTO requests\n(account_id, model_id, status, protocol, streamed,\nreserved_microdollars, proxy_request_id, provider_id, first_attempt_at,\nuser_id, credential_id, auth_epoch, principal_kind, request_kind, parent_proxy_request_id)\nVALUES (?1, ?2, 'pending', ?3, ?4, ?5, ?6, ?7,\nCASE WHEN ?8 = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,\n?9, ?10, ?11, ?12, ?13, ?14)",
    "INSERT INTO reservations\n(request_id, account_id, model_id, reserved_microdollars,\nestimated_tokens, expires_at)\nVALUES (?1, ?2, ?3, ?4, ?5, datetime('now', ?6))",
    "INSERT INTO routing_decisions\n(request_id, attempt_number, model_id, provider_id, protocol,\nselected_account_id, selected_account_name, selected_tier, selected_score,\neligible_count, scored_count, attempted_excluded_count, top_score,\ntop_score_account_name, exclude_reasons_json, score_components_json)\nVALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
    "INSERT INTO user_budget_reservations(\n            request_id, user_id, model_id, policy_version, day_start, month_start,\n            held_tokens, held_microdollars, status, expires_at,\n            enforcement, bound_tokens, bound_microdollars, price_source)\n          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'active',\n                  datetime('now', '+' || ?9 || ' seconds'), ?10, ?11, ?12, ?13)",
    "INSERT INTO user_budget_usage(\n                user_id, model_id, window, window_start, request_count, token_count,\n                cost_microdollars, provider_reported_microdollars,\n                local_estimated_microdollars, reservation_fallback_microdollars,\n                unknown_token_requests, unknown_cost_requests)\n             VALUES (?1, ?2, ?3, ?4, 1, ?5, ?6, ?7, ?8, ?9, ?10, ?11)\n             ON CONFLICT(user_id, model_id, window, window_start) DO UPDATE SET\n                request_count = user_budget_usage.request_count + 1,\n                token_count = user_budget_usage.token_count + excluded.token_count,\n                cost_microdollars = user_budget_usage.cost_microdollars + excluded.cost_microdollars,\n                provider_reported_microdollars = user_budget_usage.provider_reported_microdollars + excluded.provider_reported_microdollars,\n                local_estimated_microdollars = user_budget_usage.local_estimated_microdollars + excluded.local_estimated_microdollars,\n                reservation_fallback_microdollars = user_budget_usage.reservation_fallback_microdollars + excluded.reservation_fallback_microdollars,\n                unknown_token_requests = user_budget_usage.unknown_token_requests + excluded.unknown_token_requests,\n                unknown_cost_requests = user_budget_usage.unknown_cost_requests + excluded.unknown_cost_requests,\n                updated_at = CURRENT_TIMESTAMP",
    "INSERT OR IGNORE INTO user_budget_attempt_usage(\n            attempt_id, request_id, input_tokens, output_tokens, cost_microdollars,\n            provider_reported_microdollars, local_estimated_microdollars,\n            cost_source, tokens_known, cost_known)\n          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
    "SELECT EXISTS(SELECT 1 FROM user_credentials WHERE id = ?1 AND user_id = ?2 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP))",
    "SELECT EXISTS(SELECT 1 FROM web_sessions WHERE id = ?1 AND user_id = ?2 AND revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP AND last_used_at > datetime('now', '-12 hours'))",
    "SELECT a.completed_at, r.status\n                                 FROM request_attempts a\n                                 LEFT JOIN reservations r ON r.request_id = a.request_id\n                                     AND r.account_id = a.account_id\n                                     AND r.model_id = a.model_id\n                                 WHERE a.request_id = ?1 AND a.attempt_number = ?2\n                                 ORDER BY r.id DESC LIMIT 1",
    "SELECT a.id, a.account_id, a.provider_id, a.model_id,\na.protocol, r.id\nFROM request_attempts a\nLEFT JOIN reservations r ON r.request_id = a.request_id\nAND r.account_id = a.account_id\nAND r.model_id = a.model_id\nWHERE a.request_id = ?1 AND a.attempt_number = ?2\nORDER BY r.id DESC LIMIT 1",
    "SELECT account_id, model_id, provider_id, protocol, streamed, status,\n                            user_id, credential_id, auth_epoch, principal_kind, request_kind, parent_proxy_request_id\n                     FROM requests WHERE id = ?1",
    "SELECT coalesce(sum(CASE WHEN (?1 = 'day' AND day_start = ?2)\n                                              OR (?1 = 'month' AND month_start = ?2)\n                                         THEN held_requests ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?1 = 'day' AND day_start = ?2)\n                                              OR (?1 = 'month' AND month_start = ?2)\n                                         THEN held_tokens ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?1 = 'day' AND day_start = ?2)\n                                              OR (?1 = 'month' AND month_start = ?2)\n                                         THEN held_microdollars ELSE 0 END), 0), count(*)\n                      FROM instance_budget_reservations WHERE status = 'active'",
    "SELECT coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)\n                                              OR (?2 = 'month' AND month_start = ?3)\n                                         THEN held_requests ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)\n                                              OR (?2 = 'month' AND month_start = ?3)\n                                         THEN held_tokens ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)\n                                              OR (?2 = 'month' AND month_start = ?3)\n                                         THEN held_microdollars ELSE 0 END), 0), count(*)\n                      FROM instance_budget_reservations WHERE model_id = ?1\n                        AND status = 'active'",
    "SELECT coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)\n                                              OR (?2 = 'month' AND month_start = ?3)\n                                         THEN held_requests ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)\n                                              OR (?2 = 'month' AND month_start = ?3)\n                                         THEN held_tokens ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)\n                                              OR (?2 = 'month' AND month_start = ?3)\n                                         THEN held_microdollars ELSE 0 END), 0), count(*)\n                     FROM user_budget_reservations WHERE user_id = ?1 AND status = 'active'",
    "SELECT coalesce(sum(CASE WHEN (?3 = 'day' AND day_start = ?4)\n                                              OR (?3 = 'month' AND month_start = ?4)\n                                         THEN held_requests ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?3 = 'day' AND day_start = ?4)\n                                              OR (?3 = 'month' AND month_start = ?4)\n                                         THEN held_tokens ELSE 0 END), 0),\n                            coalesce(sum(CASE WHEN (?3 = 'day' AND day_start = ?4)\n                                              OR (?3 = 'month' AND month_start = ?4)\n                                         THEN held_microdollars ELSE 0 END), 0), count(*)\n                     FROM user_budget_reservations WHERE user_id = ?1 AND model_id = ?2\n                       AND status = 'active'",
    "SELECT coalesce(sum(input_tokens + output_tokens), 0),\n                    coalesce(sum(cost_microdollars), 0),\n                    coalesce(sum(provider_reported_microdollars), 0),\n                    coalesce(sum(local_estimated_microdollars), 0),\n                    coalesce(sum(CASE WHEN tokens_known = 0 THEN 1 ELSE 0 END), 0),\n                    coalesce(sum(CASE WHEN cost_known = 0 THEN 1 ELSE 0 END), 0)\n              FROM user_budget_attempt_usage WHERE request_id = ?1",
    "SELECT coalesce(sum(input_tokens + output_tokens), 0),\n                    coalesce(sum(cost_microdollars), 0),\n                    coalesce(sum(provider_reported_microdollars), 0),\n                    coalesce(sum(local_estimated_microdollars), 0),\n                    coalesce(sum(CASE WHEN tokens_known = 0 THEN 1 ELSE 0 END), 0),\n                    coalesce(sum(CASE WHEN cost_known = 0 THEN 1 ELSE 0 END), 0)\n             FROM user_budget_attempt_usage WHERE request_id = ?1",
    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),\n                        coalesce(sum(cost_microdollars), 0)\n                  FROM instance_budget_usage WHERE model_id = ?1\n                    AND window = ?2 AND window_start = ?3",
    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),\n                        coalesce(sum(cost_microdollars), 0)\n                  FROM instance_budget_usage WHERE window = ?1 AND window_start = ?2",
    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),\n                        coalesce(sum(cost_microdollars), 0)\n                 FROM user_budget_usage WHERE user_id = ?1 AND model_id = ?2\n                   AND window = ?3 AND window_start = ?4",
    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),\n                        coalesce(sum(cost_microdollars), 0)\n                 FROM user_budget_usage WHERE user_id = ?1 AND window = ?2 AND window_start = ?3",
    "SELECT completed_at FROM request_attempts WHERE id = ?1",
    "SELECT id, account_id, model_id, provider_id, protocol, streamed, status,\nuser_id, credential_id, auth_epoch, principal_kind, request_kind, parent_proxy_request_id\nFROM requests WHERE proxy_request_id = ?1",
    "SELECT model_id, day_start, month_start, held_tokens,\n                    held_microdollars, status\n             FROM instance_budget_reservations WHERE request_id = ?1",
    "SELECT model_id, policy_version, day_start, month_start\n             FROM instance_budget_reservations WHERE request_id = ?1 AND status = 'active'",
    "SELECT model_id, window, max_requests, max_tokens, max_microdollars,\n                max_concurrent, enforcement\n          FROM instance_budget_rules WHERE model_id IS NULL OR model_id = ?1",
    "SELECT model_id, window, max_requests, max_tokens, max_microdollars,\n                max_concurrent, enforcement\n          FROM user_budget_rules WHERE user_id = ?1 AND (model_id IS NULL OR model_id = ?2)",
    "SELECT name FROM accounts WHERE id = ?1",
    "SELECT request_id, account_id, model_id, status FROM reservations WHERE id = ?1",
    "SELECT request_id, account_id, provider_id, model_id, protocol,\n                            completed_at, status_code, error_class\n                     FROM request_attempts WHERE id = ?1",
    "SELECT revision, date('now'), strftime('%Y-%m-01', 'now') FROM policy_state WHERE singleton = 1",
    "SELECT state, auth_epoch FROM users WHERE id = ?1",
    "SELECT status FROM requests WHERE id = ?1",
    "SELECT status FROM reservations WHERE id = ?1",
    "SELECT user_id, model_id, day_start, month_start, held_tokens,\n                    held_microdollars, status\n             FROM user_budget_reservations WHERE request_id = ?1",
    "SELECT user_id, model_id, policy_version, day_start, month_start\n             FROM user_budget_reservations WHERE request_id = ?1 AND status = 'active'",
    "UPDATE instance_budget_reservations SET held_tokens = held_tokens + ?1,\n             held_microdollars = held_microdollars + ?2\n          WHERE request_id = ?3 AND status = 'active'",
    "UPDATE instance_budget_reservations SET status = 'settled', settled_at = CURRENT_TIMESTAMP,\n              observed_tokens = ?1, observed_microdollars = ?2,\n              token_usage_complete = ?3, cost_usage_complete = ?4\n          WHERE request_id = ?5 AND status = 'active'",
    "UPDATE request_attempts SET completed_at = CURRENT_TIMESTAMP,\n                 status_code = ?1, error_class = ?2, error_detail = ?3,\n                 release_reason = ?4, bytes_received = ?5, bytes_emitted = ?6,\n                 latency_ms = ?7, upstream_request_id = ?8,\n                 retry_category = ?10, is_retry_outcome = ?11\n                 WHERE id = ?9 AND completed_at IS NULL",
    "UPDATE request_attempts SET error_class = 'PublicationInterrupted',\nrelease_reason = 'publication_interrupted',\ncompleted_at = CURRENT_TIMESTAMP\nWHERE id = ?1 AND completed_at IS NULL",
    "UPDATE requests SET account_id = ?, reserved_microdollars = ?,\nprovider_id = ? WHERE id = ? AND status = 'pending'",
    "UPDATE requests SET status = ?1, completed_at = CURRENT_TIMESTAMP,\n                         input_tokens = ?2, output_tokens = ?3, cost_microdollars = ?4,\n                         status_code = ?5, error_class = ?6, error_detail = ?7,\n                         upstream_request_id = ?8, last_attempt_id = ?9,\n                         cache_read_tokens = ?11, cache_write_tokens = ?12,\n                         reasoning_tokens = ?13, thinking_characters = ?14,\n                         upstream_latency_ms = ?15, first_byte_ms = ?16,\n                         bytes_received = ?17, bytes_emitted = ?18,\n                         upstream_connect_ms = ?19, upstream_read_ms = ?20,\n                         coordinator_overhead_ms = ?21,\n                         cache_counter_status = ?22, cached_input_tokens = ?23,\n                         cache_read_input_tokens = ?24,\n                         cache_creation_input_tokens = ?25,\n                         cache_write_input_tokens = ?26,\n                         cache_write_input_reported = ?27,\n                         input_tokens_reported = ?28, output_tokens_reported = ?29,\n                         total_tokens_reported = ?30, transcoded = ?31,\n                         provider_cost_microdollars = ?32,\n                         provider_cost_source = ?33,\n                         local_cost_microdollars = ?34,\n                         local_cost_exactness = ?35,\n                         upstream_protocol = ?36 WHERE id = ?10\n                         AND status NOT IN ('completed','client_error','cancelled','error',\n                         'interrupted','failed','client_disconnected')",
    "UPDATE reservations SET status = 'released',\nreleased_at = CURRENT_TIMESTAMP,\nrelease_reason = 'publication_interrupted'\nWHERE id = ?1 AND status = 'active'",
    "UPDATE reservations SET status = 'released', released_at = CURRENT_TIMESTAMP,\n                     release_reason = ?1 WHERE id = ?2 AND status = 'active'",
    "UPDATE user_budget_reservations SET held_tokens = held_tokens + ?1,\n             held_microdollars = held_microdollars + ?2\n         WHERE request_id = ?3 AND status = 'active'",
    "UPDATE user_budget_reservations SET status = 'settled', settled_at = CURRENT_TIMESTAMP,\n             observed_tokens = ?1, observed_microdollars = ?2,\n             token_usage_complete = ?3, cost_usage_complete = ?4\n         WHERE request_id = ?5 AND status = 'active'",
];

pub(crate) fn execute(
    connection: &Connection,
    sql: &str,
    params: impl Params,
) -> Result<usize, tokio_rusqlite::rusqlite::Error> {
    prepare(connection, sql)?.execute(params)
}

pub(crate) fn query_row<T, P, F>(
    connection: &Connection,
    sql: &str,
    params: P,
    function: F,
) -> Result<T, tokio_rusqlite::rusqlite::Error>
where
    P: Params,
    F: FnOnce(&Row<'_>) -> Result<T, tokio_rusqlite::rusqlite::Error>,
{
    prepare(connection, sql)?.query_row(params, function)
}

pub(crate) fn prepare<'connection>(
    connection: &'connection Connection,
    sql: &str,
) -> Result<CachedStatement<'connection>, tokio_rusqlite::rusqlite::Error> {
    debug_assert!(
        HOT_STATEMENTS.contains(&sql),
        "publication or finalization SQL is missing from the hot-statement census: {sql}"
    );
    connection.prepare_cached(sql)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use tokio_rusqlite::rusqlite::Connection;

    use super::{HOT_STATEMENT_CACHE_CAPACITY, HOT_STATEMENTS};

    #[test]
    fn cached_execute_matches_direct_execute() {
        let connection = Connection::open_in_memory().expect("memory database");
        connection
            .execute_batch("CREATE TABLE sample(value TEXT NOT NULL)")
            .expect("table");
        connection
            .execute("INSERT INTO sample(value) VALUES (?1)", ["direct"])
            .expect("direct insert");
        connection
            .prepare_cached("INSERT INTO sample(value) VALUES (?1)")
            .expect("cached prepare")
            .execute(["cached"])
            .expect("cached insert");
        let mut statement = connection
            .prepare("SELECT value FROM sample ORDER BY rowid")
            .expect("select");
        let values = statement
            .query_map([], |row| row.get::<_, String>(0))
            .expect("rows")
            .collect::<Result<Vec<_>, _>>()
            .expect("values");
        assert_eq!(values, ["direct", "cached"]);
    }

    #[test]
    fn hot_statement_census_fits_the_cache() {
        let unique: BTreeSet<&str> = HOT_STATEMENTS.iter().copied().collect();
        assert_eq!(
            unique.len(),
            HOT_STATEMENTS.len(),
            "hot statement census contains a duplicate"
        );
        assert!(
            unique.len() <= HOT_STATEMENT_CACHE_CAPACITY,
            "hot statement census ({}) exceeds cache capacity {}",
            unique.len(),
            HOT_STATEMENT_CACHE_CAPACITY
        );
    }
}
