//! Instance-global budget ceilings intersected with per-user rules.
//!
//! Exactly one instance-wide scope: a rule with `model_id = None` caps all
//! models, a rule with `model_id = Some` caps that exact model. There is no
//! organization/tenant identifier and no second database writer: checks and
//! holds run in the same publication SQLite transaction as the user budget,
//! and settlement folds the same per-attempt usage rows. Instance tables
//! carry no user reference, so global counters survive user disable/delete
//! and cover unattributed (legacy-key) requests as well. Absent instance
//! rules, publication takes no instance hold and writes no instance rows, so
//! a single-admin workload is unchanged except measured gate overhead.
//!
//! Hard/estimated semantics, worst-case holds, and the operator-tariff trust
//! boundary are shared with `user_budget` (M008 contract); this module only
//! adds the global scope and its intersection.

use thiserror::Error;
use tokio_rusqlite::rusqlite::{OptionalExtension, params};

use crate::{
    db::{Database, DatabaseError},
    user_budget::{
        BudgetAttemptSettlement, BudgetCheck, BudgetEnforcement, BudgetHold, BudgetLimits,
        BudgetPriceSource, BudgetProof, BudgetWindow, exceeds, hold_for_admission, hold_for_retry,
    },
};

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct InstanceBudgetRule {
    pub id: i64,
    pub model_id: Option<String>,
    pub window: BudgetWindow,
    pub max_requests: Option<i64>,
    pub max_tokens: Option<i64>,
    pub max_microdollars: Option<i64>,
    pub max_concurrent: Option<i64>,
    pub enforcement: BudgetEnforcement,
    pub version: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct InstanceBudgetUsage {
    pub model_id: String,
    pub window: BudgetWindow,
    pub window_start: String,
    pub request_count: i64,
    pub token_count: i64,
    pub cost_microdollars: i64,
    pub provider_reported_microdollars: i64,
    pub local_estimated_microdollars: i64,
    pub reservation_fallback_microdollars: i64,
    pub unknown_token_requests: i64,
    pub unknown_cost_requests: i64,
}

#[derive(Debug, Error)]
pub enum InstanceBudgetError {
    #[error("invalid instance budget rule")]
    InvalidRule,
    #[error("an active administrator is required")]
    ActorNotAdmin,
    #[error("instance budget database operation failed: {0}")]
    Database(#[from] DatabaseError),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MutationResult {
    Updated,
    Removed(bool),
    ActorNotAdmin,
}

#[derive(Debug, Clone)]
pub struct InstanceBudgetService {
    database: Database,
}

impl InstanceBudgetService {
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Create or replace the instance rule for one model scope and window.
    /// Only active admins may mutate; ordinary member/API tokens cannot.
    pub async fn set_rule(
        &self,
        actor_id: &str,
        model_id: Option<&str>,
        window: BudgetWindow,
        limits: BudgetLimits,
    ) -> Result<(), InstanceBudgetError> {
        if model_id.is_some_and(|value| value.is_empty() || value.trim() != value)
            || [
                limits.requests,
                limits.tokens,
                limits.microdollars,
                limits.concurrent,
            ]
            .into_iter()
            .flatten()
            .any(|value| value <= 0)
            || [
                limits.requests,
                limits.tokens,
                limits.microdollars,
                limits.concurrent,
            ]
            .into_iter()
            .all(|value| value.is_none())
        {
            return Err(InstanceBudgetError::InvalidRule);
        }
        let actor_id = actor_id.to_owned();
        let model_id = model_id.map(str::to_owned);
        let window = window.as_str().to_owned();
        let enforcement = limits.enforcement.as_str().to_owned();
        let outcome = self
            .database
            .with_transaction(move |connection| {
                let actor: bool = connection.query_row(
                    "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND role = 'admin' AND state = 'active')",
                    [&actor_id],
                    |row| row.get(0),
                )?;
                if !actor {
                    return Ok(MutationResult::ActorNotAdmin);
                }
                connection.execute(
                    "INSERT INTO instance_budget_rules(
                        model_id, window, max_requests, max_tokens,
                        max_microdollars, max_concurrent, enforcement, updated_by)
                      VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                      ON CONFLICT(ifnull(model_id, ''), window) DO UPDATE SET
                         max_requests = excluded.max_requests,
                         max_tokens = excluded.max_tokens,
                         max_microdollars = excluded.max_microdollars,
                         max_concurrent = excluded.max_concurrent,
                         enforcement = excluded.enforcement,
                         version = instance_budget_rules.version + 1,
                         updated_by = excluded.updated_by,
                         updated_at = CURRENT_TIMESTAMP",
                    params![
                        model_id,
                        window,
                        limits.requests,
                        limits.tokens,
                        limits.microdollars,
                        limits.concurrent,
                        enforcement,
                        actor_id,
                    ],
                )?;
                connection.execute(
                    "INSERT INTO instance_budget_rule_audit(
                        actor_id, model_id, window, action, max_requests,
                        max_tokens, max_microdollars, max_concurrent, enforcement, rule_version)
                      SELECT updated_by, model_id, window, 'set', max_requests,
                             max_tokens, max_microdollars, max_concurrent, enforcement, version
                      FROM instance_budget_rules WHERE model_id IS ?1 AND window = ?2",
                    params![model_id, window],
                )?;
                Ok(MutationResult::Updated)
            })
            .await
            .map_err(InstanceBudgetError::Database)?;
        match outcome {
            MutationResult::Updated => Ok(()),
            MutationResult::ActorNotAdmin => Err(InstanceBudgetError::ActorNotAdmin),
            MutationResult::Removed(_) => Err(InstanceBudgetError::InvalidRule),
        }
    }

    pub async fn remove_rule(
        &self,
        actor_id: &str,
        model_id: Option<&str>,
        window: BudgetWindow,
    ) -> Result<bool, InstanceBudgetError> {
        let actor_id = actor_id.to_owned();
        let model_id = model_id.map(str::to_owned);
        let window = window.as_str().to_owned();
        let outcome = self
            .database
            .with_transaction(move |connection| {
                let actor: bool = connection.query_row(
                    "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND role = 'admin' AND state = 'active')",
                    [&actor_id],
                    |row| row.get(0),
                )?;
                if !actor {
                    return Ok(MutationResult::ActorNotAdmin);
                }
                let previous = connection
                    .query_row(
                        "SELECT max_requests, max_tokens, max_microdollars, max_concurrent, enforcement, version
                          FROM instance_budget_rules WHERE model_id IS ?1 AND window = ?2",
                        params![model_id, window],
                        |row| {
                            Ok((
                                row.get::<_, Option<i64>>(0)?,
                                row.get::<_, Option<i64>>(1)?,
                                row.get::<_, Option<i64>>(2)?,
                                row.get::<_, Option<i64>>(3)?,
                                row.get::<_, String>(4)?,
                                row.get::<_, i64>(5)?,
                            ))
                        },
                    )
                    .optional()?;
                let removed = connection.execute(
                    "DELETE FROM instance_budget_rules WHERE model_id IS ?1 AND window = ?2",
                    params![model_id, window],
                )? == 1;
                if let Some((requests, tokens, cost, concurrent, enforcement, version)) = previous {
                    connection.execute(
                        "INSERT INTO instance_budget_rule_audit(
                            actor_id, model_id, window, action, max_requests,
                            max_tokens, max_microdollars, max_concurrent, enforcement, rule_version)
                          VALUES (?1, ?2, ?3, 'remove', ?4, ?5, ?6, ?7, ?8, ?9)",
                        params![
                            actor_id,
                            model_id,
                            window,
                            requests,
                            tokens,
                            cost,
                            concurrent,
                            enforcement,
                            version + 1,
                        ],
                    )?;
                }
                Ok(MutationResult::Removed(removed))
            })
            .await
            .map_err(InstanceBudgetError::Database)?;
        match outcome {
            MutationResult::Removed(changed) => Ok(changed),
            MutationResult::ActorNotAdmin => Err(InstanceBudgetError::ActorNotAdmin),
            MutationResult::Updated => Err(InstanceBudgetError::InvalidRule),
        }
    }

    pub async fn rules(&self) -> Result<Vec<InstanceBudgetRule>, InstanceBudgetError> {
        self.database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT id, model_id, window, max_requests, max_tokens,
                            max_microdollars, max_concurrent, enforcement, version
                      FROM instance_budget_rules
                      ORDER BY model_id IS NOT NULL, model_id, window",
                )?;
                statement
                    .query_map([], |row| {
                        let window: String = row.get(2)?;
                        let enforcement: String = row.get(7)?;
                        Ok(InstanceBudgetRule {
                            id: row.get(0)?,
                            model_id: row.get(1)?,
                            window: parse_window(&window),
                            max_requests: row.get(3)?,
                            max_tokens: row.get(4)?,
                            max_microdollars: row.get(5)?,
                            max_concurrent: row.get(6)?,
                            enforcement: BudgetEnforcement::parse(&enforcement)
                                .unwrap_or(BudgetEnforcement::Estimated),
                            version: row.get(8)?,
                        })
                    })?
                    .collect()
            })
            .await
            .map_err(InstanceBudgetError::Database)
    }

    pub async fn usage(
        &self,
        since_utc: &str,
        limit: u16,
    ) -> Result<Vec<InstanceBudgetUsage>, InstanceBudgetError> {
        if since_utc.len() != 10 || limit == 0 || limit > 500 {
            return Err(InstanceBudgetError::InvalidRule);
        }
        let since_utc = since_utc.to_owned();
        self.database
            .call(move |connection| {
                let mut statement = connection.prepare(
                    "SELECT model_id, window, window_start, request_count, token_count,
                            cost_microdollars, provider_reported_microdollars,
                            local_estimated_microdollars, reservation_fallback_microdollars,
                            unknown_token_requests, unknown_cost_requests
                      FROM instance_budget_usage WHERE window_start >= ?1
                      ORDER BY window_start DESC, model_id LIMIT ?2",
                )?;
                statement
                    .query_map(params![since_utc, i64::from(limit)], |row| {
                        let window: String = row.get(1)?;
                        Ok(InstanceBudgetUsage {
                            model_id: row.get(0)?,
                            window: parse_window(&window),
                            window_start: row.get(2)?,
                            request_count: row.get(3)?,
                            token_count: row.get(4)?,
                            cost_microdollars: row.get(5)?,
                            provider_reported_microdollars: row.get(6)?,
                            local_estimated_microdollars: row.get(7)?,
                            reservation_fallback_microdollars: row.get(8)?,
                            unknown_token_requests: row.get(9)?,
                            unknown_cost_requests: row.get(10)?,
                        })
                    })?
                    .collect()
            })
            .await
            .map_err(InstanceBudgetError::Database)
    }
}

fn parse_window(value: &str) -> BudgetWindow {
    if value == "month" {
        BudgetWindow::Month
    } else {
        BudgetWindow::Day
    }
}

#[derive(Debug, Clone)]
pub(crate) struct InstancePermit {
    pub policy_version: i64,
    pub day_start: String,
    pub month_start: String,
}

struct LoadedInstanceRule {
    scope_model: Option<String>,
    window: String,
    max_requests: Option<i64>,
    max_tokens: Option<i64>,
    max_cost: Option<i64>,
    max_concurrent: Option<i64>,
    enforcement: BudgetEnforcement,
}

/// Evaluate the instance-global intersection for one new logical request.
/// Returns `None` (allowed, no hold) when no instance rule matches the model,
/// so rule-free operation takes no instance hold and writes no instance rows.
pub(crate) fn check_initial(
    connection: &tokio_rusqlite::rusqlite::Connection,
    model_id: &str,
    tokens: i64,
    microdollars: i64,
    proof: Option<&BudgetProof>,
) -> Result<(BudgetCheck, Option<InstancePermit>, BudgetHold), tokio_rusqlite::rusqlite::Error> {
    let (policy_version, day_start, month_start): (i64, String, String) = connection.query_row(
        "SELECT revision, date('now'), strftime('%Y-%m-01', 'now') FROM policy_state WHERE singleton = 1",
        [],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;
    let permit = InstancePermit {
        policy_version,
        day_start,
        month_start,
    };
    let rules = load_matching_rules(connection, model_id)?;
    if rules.is_empty() {
        return Ok((BudgetCheck::Allowed, None, BudgetHold::empty()));
    }
    let hard_token = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_tokens.is_some());
    let hard_cost = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_cost.is_some());
    let Some(hold) = hold_for_admission(tokens, microdollars, proof, hard_token, hard_cost) else {
        return Ok((BudgetCheck::NotEnforceable, None, BudgetHold::empty()));
    };
    let exceeded = rules_exceeded(
        connection,
        &permit,
        &rules,
        hold,
        tokens,
        microdollars,
        true,
    )?;
    Ok((
        if exceeded {
            BudgetCheck::Exceeded
        } else {
            BudgetCheck::Allowed
        },
        Some(permit),
        hold,
    ))
}

/// Re-evaluate the instance intersection before a retry attempt extends the
/// logical hold. Returns `None` when this request never took an instance
/// hold, so rule-free requests skip instance work on every attempt.
pub(crate) fn check_retry(
    connection: &tokio_rusqlite::rusqlite::Connection,
    request_id: i64,
    tokens: i64,
    microdollars: i64,
    proof: Option<&BudgetProof>,
) -> Result<(BudgetCheck, Option<BudgetHold>), tokio_rusqlite::rusqlite::Error> {
    let reservation = connection
        .query_row(
            "SELECT model_id, policy_version, day_start, month_start
             FROM instance_budget_reservations WHERE request_id = ?1 AND status = 'active'",
            [request_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .optional()?;
    let Some((model_id, policy_version, day_start, month_start)) = reservation else {
        return Ok((BudgetCheck::Allowed, None));
    };
    let permit = InstancePermit {
        policy_version,
        day_start,
        month_start,
    };
    let rules = load_matching_rules(connection, &model_id)?;
    let hard_token = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_tokens.is_some());
    let hard_cost = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_cost.is_some());
    let Some(hold) = hold_for_retry(tokens, microdollars, proof, hard_token, hard_cost) else {
        return Ok((BudgetCheck::NotEnforceable, None));
    };
    let exceeded = rules_exceeded(
        connection,
        &permit,
        &rules,
        hold,
        tokens,
        microdollars,
        false,
    )?;
    Ok((
        if exceeded {
            BudgetCheck::Exceeded
        } else {
            BudgetCheck::Allowed
        },
        Some(hold),
    ))
}

pub(crate) struct InstanceReservationSpec<'a> {
    pub request_id: i64,
    pub model_id: &'a str,
    pub tokens: i64,
    pub microdollars: i64,
    pub ttl_seconds: i64,
    pub hold: BudgetHold,
}

pub(crate) fn insert_reservation(
    connection: &tokio_rusqlite::rusqlite::Connection,
    spec: InstanceReservationSpec<'_>,
    permit: &InstancePermit,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    connection.execute(
        "INSERT INTO instance_budget_reservations(
            request_id, model_id, policy_version, day_start, month_start,
            held_tokens, held_microdollars, status, expires_at,
            enforcement, bound_tokens, bound_microdollars, price_source)
          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'active',
                  datetime('now', '+' || ?8 || ' seconds'), ?9, ?10, ?11, ?12)",
        params![
            spec.request_id,
            spec.model_id,
            permit.policy_version,
            permit.day_start,
            permit.month_start,
            spec.tokens,
            spec.microdollars,
            spec.ttl_seconds,
            spec.hold.enforcement.as_str(),
            spec.hold.bound_tokens,
            spec.hold.bound_microdollars,
            spec.hold.price_source.map(BudgetPriceSource::as_str),
        ],
    )?;
    Ok(())
}

pub(crate) fn extend_reservation(
    connection: &tokio_rusqlite::rusqlite::Connection,
    request_id: i64,
    tokens: i64,
    microdollars: i64,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    let changed = connection.execute(
        "UPDATE instance_budget_reservations SET held_tokens = held_tokens + ?1,
             held_microdollars = held_microdollars + ?2
          WHERE request_id = ?3 AND status = 'active'",
        params![tokens, microdollars, request_id],
    )?;
    if changed != 1 {
        return Err(tokio_rusqlite::rusqlite::Error::InvalidParameterName(
            "active instance budget reservation missing".to_owned(),
        ));
    }
    Ok(())
}

fn load_matching_rules(
    connection: &tokio_rusqlite::rusqlite::Connection,
    model_id: &str,
) -> Result<Vec<LoadedInstanceRule>, tokio_rusqlite::rusqlite::Error> {
    let mut statement = connection.prepare(
        "SELECT model_id, window, max_requests, max_tokens, max_microdollars,
                max_concurrent, enforcement
          FROM instance_budget_rules WHERE model_id IS NULL OR model_id = ?1",
    )?;
    statement
        .query_map(params![model_id], |row| {
            let enforcement: String = row.get(6)?;
            Ok(LoadedInstanceRule {
                scope_model: row.get(0)?,
                window: row.get(1)?,
                max_requests: row.get(2)?,
                max_tokens: row.get(3)?,
                max_cost: row.get(4)?,
                max_concurrent: row.get(5)?,
                enforcement: BudgetEnforcement::parse(&enforcement)
                    .unwrap_or(BudgetEnforcement::Estimated),
            })
        })?
        .collect()
}

/// Evaluate every matching instance rule against settled usage plus active
/// holds. Global (model-agnostic) rules sum across per-model usage rows with
/// a bounded indexed aggregate; exact-model rules read their own row scope.
/// Each logical request counts once: retries extend the one hold instead of
/// adding requests.
fn rules_exceeded(
    connection: &tokio_rusqlite::rusqlite::Connection,
    permit: &InstancePermit,
    rules: &[LoadedInstanceRule],
    hold: BudgetHold,
    estimate_tokens: i64,
    estimate_microdollars: i64,
    new_request: bool,
) -> Result<bool, tokio_rusqlite::rusqlite::Error> {
    for rule in rules {
        let window_start = if rule.window == "day" {
            &permit.day_start
        } else {
            &permit.month_start
        };
        let window = rule.window.as_str();
        let token_add = if rule.enforcement == BudgetEnforcement::Hard && rule.max_tokens.is_some()
        {
            hold.tokens
        } else {
            estimate_tokens.max(0)
        };
        let cost_add = if rule.enforcement == BudgetEnforcement::Hard && rule.max_cost.is_some() {
            hold.microdollars
        } else {
            estimate_microdollars.max(0)
        };
        let (used_requests, used_tokens, used_cost): (i64, i64, i64) =
            if let Some(model) = rule.scope_model.as_deref() {
                connection.query_row(
                    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),
                        coalesce(sum(cost_microdollars), 0)
                  FROM instance_budget_usage WHERE model_id = ?1
                    AND window = ?2 AND window_start = ?3",
                    params![model, window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )?
            } else {
                connection.query_row(
                    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),
                        coalesce(sum(cost_microdollars), 0)
                  FROM instance_budget_usage WHERE window = ?1 AND window_start = ?2",
                    params![window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )?
            };
        // Active holds use the same window-case projection as the user scope
        // so requests straddling a UTC reset still occupy their slot, while
        // the concurrency count spans windows. All aggregates are bounded
        // indexed lookups; no full history scan runs under the writer gate.
        let (held_requests, held_tokens, held_cost, active_count): (i64, i64, i64, i64) =
            if let Some(model) = rule.scope_model.as_deref() {
                connection.query_row(
                    "SELECT coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)
                                              OR (?2 = 'month' AND month_start = ?3)
                                         THEN held_requests ELSE 0 END), 0),
                            coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)
                                              OR (?2 = 'month' AND month_start = ?3)
                                         THEN held_tokens ELSE 0 END), 0),
                            coalesce(sum(CASE WHEN (?2 = 'day' AND day_start = ?3)
                                              OR (?2 = 'month' AND month_start = ?3)
                                         THEN held_microdollars ELSE 0 END), 0), count(*)
                      FROM instance_budget_reservations WHERE model_id = ?1
                        AND status = 'active'",
                    params![model, window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )?
            } else {
                connection.query_row(
                    "SELECT coalesce(sum(CASE WHEN (?1 = 'day' AND day_start = ?2)
                                              OR (?1 = 'month' AND month_start = ?2)
                                         THEN held_requests ELSE 0 END), 0),
                            coalesce(sum(CASE WHEN (?1 = 'day' AND day_start = ?2)
                                              OR (?1 = 'month' AND month_start = ?2)
                                         THEN held_tokens ELSE 0 END), 0),
                            coalesce(sum(CASE WHEN (?1 = 'day' AND day_start = ?2)
                                              OR (?1 = 'month' AND month_start = ?2)
                                         THEN held_microdollars ELSE 0 END), 0), count(*)
                      FROM instance_budget_reservations WHERE status = 'active'",
                    params![window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )?
            };
        // Retries extend the one logical hold: like the user scope, they
        // consume no additional request or concurrency slot.
        let request_hold = i64::from(new_request);
        if rule
            .max_requests
            .is_some_and(|limit| exceeds(limit, used_requests, held_requests, request_hold))
            || rule
                .max_tokens
                .is_some_and(|limit| exceeds(limit, used_tokens, held_tokens, token_add))
            || rule
                .max_cost
                .is_some_and(|limit| exceeds(limit, used_cost, held_cost, cost_add))
            || rule
                .max_concurrent
                .is_some_and(|limit| exceeds(limit, active_count, request_hold, 0))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Fold one request's per-attempt usage into the instance buckets and settle
/// its instance hold. Attempt facts come from the shared attempt table, so
/// unattributed requests settle exactly like attributed ones. No-op when the
/// request never took an instance hold.
pub(crate) fn record_attempt_and_maybe_settle(
    connection: &tokio_rusqlite::rusqlite::Connection,
    settlement: BudgetAttemptSettlement,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    let BudgetAttemptSettlement {
        request_id,
        terminalize_request,
        ..
    } = settlement;
    let reservation = connection
        .query_row(
            "SELECT model_id, day_start, month_start, held_tokens,
                    held_microdollars, status
             FROM instance_budget_reservations WHERE request_id = ?1",
            [request_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, String>(5)?,
                ))
            },
        )
        .optional()?;
    let Some((model_id, day_start, month_start, held_tokens, held_cost, status)) = reservation
    else {
        return Ok(());
    };
    if !terminalize_request || status == "settled" || status == "released" {
        return Ok(());
    }
    if status != "active" {
        return Err(tokio_rusqlite::rusqlite::Error::InvalidParameterName(
            "unknown instance budget reservation state".to_owned(),
        ));
    }
    let (observed_tokens, observed_cost, provider_cost, local_cost, token_unknown, cost_unknown): (
        i64,
        i64,
        i64,
        i64,
        i64,
        i64,
    ) = connection.query_row(
        "SELECT coalesce(sum(input_tokens + output_tokens), 0),
                    coalesce(sum(cost_microdollars), 0),
                    coalesce(sum(provider_reported_microdollars), 0),
                    coalesce(sum(local_estimated_microdollars), 0),
                    coalesce(sum(CASE WHEN tokens_known = 0 THEN 1 ELSE 0 END), 0),
                    coalesce(sum(CASE WHEN cost_known = 0 THEN 1 ELSE 0 END), 0)
              FROM user_budget_attempt_usage WHERE request_id = ?1",
        [request_id],
        |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
            ))
        },
    )?;
    let charged_tokens = if token_unknown > 0 {
        held_tokens.max(observed_tokens)
    } else {
        observed_tokens
    };
    let charged_cost = if cost_unknown > 0 {
        held_cost.max(observed_cost)
    } else {
        observed_cost
    };
    let fallback_cost = (charged_cost - provider_cost - local_cost).max(0);
    for (window, start) in [("day", day_start.as_str()), ("month", month_start.as_str())] {
        connection.execute(
            "INSERT INTO instance_budget_usage(
                model_id, window, window_start, request_count, token_count,
                cost_microdollars, provider_reported_microdollars,
                local_estimated_microdollars, reservation_fallback_microdollars,
                unknown_token_requests, unknown_cost_requests)
              VALUES (?1, ?2, ?3, 1, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
              ON CONFLICT(model_id, window, window_start) DO UPDATE SET
                 request_count = instance_budget_usage.request_count + 1,
                 token_count = instance_budget_usage.token_count + excluded.token_count,
                 cost_microdollars = instance_budget_usage.cost_microdollars + excluded.cost_microdollars,
                 provider_reported_microdollars = instance_budget_usage.provider_reported_microdollars + excluded.provider_reported_microdollars,
                 local_estimated_microdollars = instance_budget_usage.local_estimated_microdollars + excluded.local_estimated_microdollars,
                 reservation_fallback_microdollars = instance_budget_usage.reservation_fallback_microdollars + excluded.reservation_fallback_microdollars,
                 unknown_token_requests = instance_budget_usage.unknown_token_requests + excluded.unknown_token_requests,
                 unknown_cost_requests = instance_budget_usage.unknown_cost_requests + excluded.unknown_cost_requests,
                 updated_at = CURRENT_TIMESTAMP",
            params![
                model_id,
                window,
                start,
                charged_tokens,
                charged_cost,
                provider_cost,
                local_cost,
                fallback_cost,
                i64::from(token_unknown > 0),
                i64::from(cost_unknown > 0),
            ],
        )?;
    }
    connection.execute(
        "UPDATE instance_budget_reservations SET status = 'settled', settled_at = CURRENT_TIMESTAMP,
              observed_tokens = ?1, observed_microdollars = ?2,
              token_usage_complete = ?3, cost_usage_complete = ?4
          WHERE request_id = ?5 AND status = 'active'",
        params![
            observed_tokens,
            observed_cost,
            i64::from(token_unknown == 0),
            i64::from(cost_unknown == 0),
            request_id,
        ],
    )?;
    Ok(())
}
