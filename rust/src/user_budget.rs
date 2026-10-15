//! Durable downstream-user budgets, reservations, and bounded usage reads.

use thiserror::Error;
use tokio_rusqlite::rusqlite::{OptionalExtension, params};

use crate::db::{Database, DatabaseError};

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BudgetWindow {
    Day,
    Month,
}

impl BudgetWindow {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Day => "day",
            Self::Month => "month",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BudgetEnforcement {
    Hard,
    #[default]
    Estimated,
}

impl BudgetEnforcement {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Hard => "hard",
            Self::Estimated => "estimated",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "hard" => Some(Self::Hard),
            "estimated" => Some(Self::Estimated),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct UserBudgetRule {
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
pub struct UserBudgetUsage {
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

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BudgetLimits {
    pub requests: Option<i64>,
    pub tokens: Option<i64>,
    pub microdollars: Option<i64>,
    pub concurrent: Option<i64>,
    pub enforcement: BudgetEnforcement,
}

#[derive(Debug, Error)]
pub enum UserBudgetError {
    #[error("invalid user budget rule")]
    InvalidRule,
    #[error("an active administrator is required")]
    ActorNotAdmin,
    #[error("user does not exist or is unavailable")]
    UserUnavailable,
    #[error("user budget database operation failed: {0}")]
    Database(#[from] DatabaseError),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MutationResult {
    Updated,
    Removed(bool),
    ActorNotAdmin,
    UserUnavailable,
}

#[derive(Debug, Clone)]
pub struct UserBudgetService {
    database: Database,
    browser_admin_session: Option<crate::web_session::SessionFence>,
}

impl UserBudgetService {
    pub fn new(database: Database) -> Self {
        Self {
            database,
            browser_admin_session: None,
        }
    }

    pub fn with_browser_admin_session(mut self, principal: &crate::web_session::Principal) -> Self {
        self.browser_admin_session =
            Some(crate::web_session::SessionFence::from_principal(principal));
        self
    }

    pub async fn set_rule(
        &self,
        actor_id: &str,
        user_id: &str,
        model_id: Option<&str>,
        window: BudgetWindow,
        limits: BudgetLimits,
    ) -> Result<(), UserBudgetError> {
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
            return Err(UserBudgetError::InvalidRule);
        }
        let actor_id = actor_id.to_owned();
        let user_id = user_id.to_owned();
        let model_id = model_id.map(str::to_owned);
        let window = window.as_str().to_owned();
        let enforcement = limits.enforcement.as_str().to_owned();
        let browser_admin_session = self.browser_admin_session.clone();
        let outcome = self.database
            .with_transaction(move |connection| {
                let actor = if let Some(fence) = browser_admin_session.as_ref() {
                    fence.validates_actor(connection, &actor_id, true)?
                } else {
                    connection.query_row(
                        "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND role = 'admin' AND state = 'active')",
                        [&actor_id],
                        |row| row.get(0),
                    )?
                };
                if !actor {
                    return Ok(MutationResult::ActorNotAdmin);
                }
                let user_exists: bool = connection.query_row(
                    "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND state = 'active')",
                    [&user_id],
                    |row| row.get(0),
                )?;
                if !user_exists {
                    return Ok(MutationResult::UserUnavailable);
                }
                connection.execute(
                    "INSERT INTO user_budget_rules(
                        user_id, model_id, window, max_requests, max_tokens,
                        max_microdollars, max_concurrent, enforcement, updated_by)
                      VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
                      ON CONFLICT(user_id, ifnull(model_id, ''), window) DO UPDATE SET
                         max_requests = excluded.max_requests,
                         max_tokens = excluded.max_tokens,
                         max_microdollars = excluded.max_microdollars,
                         max_concurrent = excluded.max_concurrent,
                         enforcement = excluded.enforcement,
                         version = user_budget_rules.version + 1,
                         updated_by = excluded.updated_by,
                         updated_at = CURRENT_TIMESTAMP",
                    params![
                        user_id,
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
                    "INSERT INTO user_budget_rule_audit(
                        user_id, actor_id, model_id, window, action, max_requests,
                        max_tokens, max_microdollars, max_concurrent, enforcement, rule_version)
                      SELECT user_id, updated_by, model_id, window, 'set', max_requests,
                             max_tokens, max_microdollars, max_concurrent, enforcement, version
                      FROM user_budget_rules WHERE user_id = ?1 AND model_id IS ?2 AND window = ?3",
                    params![user_id, model_id, window],
                )?;
                Ok(MutationResult::Updated)
            })
            .await
            .map_err(UserBudgetError::Database)?;
        match outcome {
            MutationResult::Updated => Ok(()),
            MutationResult::ActorNotAdmin => Err(UserBudgetError::ActorNotAdmin),
            MutationResult::UserUnavailable => Err(UserBudgetError::UserUnavailable),
            MutationResult::Removed(_) => Err(UserBudgetError::InvalidRule),
        }
    }

    pub async fn remove_rule(
        &self,
        actor_id: &str,
        user_id: &str,
        model_id: Option<&str>,
        window: BudgetWindow,
    ) -> Result<bool, UserBudgetError> {
        let actor_id = actor_id.to_owned();
        let user_id = user_id.to_owned();
        let model_id = model_id.map(str::to_owned);
        let window = window.as_str().to_owned();
        let browser_admin_session = self.browser_admin_session.clone();
        let outcome = self.database
            .with_transaction(move |connection| {
                let actor = if let Some(fence) = browser_admin_session.as_ref() {
                    fence.validates_actor(connection, &actor_id, true)?
                } else {
                    connection.query_row(
                        "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND role = 'admin' AND state = 'active')",
                        [&actor_id],
                        |row| row.get(0),
                    )?
                };
                if !actor {
                    return Ok(MutationResult::ActorNotAdmin);
                }
                let previous = connection
                    .query_row(
                        "SELECT max_requests, max_tokens, max_microdollars, max_concurrent, enforcement, version
                          FROM user_budget_rules WHERE user_id = ?1 AND model_id IS ?2 AND window = ?3",
                        params![user_id, model_id, window],
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
                    "DELETE FROM user_budget_rules WHERE user_id = ?1 AND model_id IS ?2 AND window = ?3",
                    params![user_id, model_id, window],
                )? == 1;
                if let Some((requests, tokens, cost, concurrent, enforcement, version)) = previous {
                    connection.execute(
                        "INSERT INTO user_budget_rule_audit(
                            user_id, actor_id, model_id, window, action, max_requests,
                            max_tokens, max_microdollars, max_concurrent, enforcement, rule_version)
                          VALUES (?1, ?2, ?3, ?4, 'remove', ?5, ?6, ?7, ?8, ?9, ?10)",
                        params![
                            user_id,
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
            .map_err(UserBudgetError::Database)?;
        match outcome {
            MutationResult::Removed(changed) => Ok(changed),
            MutationResult::ActorNotAdmin => Err(UserBudgetError::ActorNotAdmin),
            MutationResult::Updated | MutationResult::UserUnavailable => {
                Err(UserBudgetError::InvalidRule)
            }
        }
    }

    pub async fn rules(&self, user_id: &str) -> Result<Vec<UserBudgetRule>, UserBudgetError> {
        let user_id = user_id.to_owned();
        self.database
            .call(move |connection| {
                let mut statement = connection.prepare(
                    "SELECT id, model_id, window, max_requests, max_tokens,
                            max_microdollars, max_concurrent, enforcement, version
                      FROM user_budget_rules WHERE user_id = ?1
                      ORDER BY model_id IS NOT NULL, model_id, window",
                )?;
                statement
                    .query_map([user_id], |row| {
                        let window: String = row.get(2)?;
                        let enforcement: String = row.get(7)?;
                        Ok(UserBudgetRule {
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
            .map_err(UserBudgetError::Database)
    }

    pub async fn usage(
        &self,
        user_id: &str,
        since_utc: &str,
        limit: u16,
    ) -> Result<Vec<UserBudgetUsage>, UserBudgetError> {
        if since_utc.len() != 10 || limit == 0 || limit > 500 {
            return Err(UserBudgetError::InvalidRule);
        }
        let user_id = user_id.to_owned();
        let since_utc = since_utc.to_owned();
        self.database
            .call(move |connection| {
                let mut statement = connection.prepare(
                    "SELECT model_id, window, window_start, request_count, token_count,
                            cost_microdollars, provider_reported_microdollars,
                            local_estimated_microdollars, reservation_fallback_microdollars,
                            unknown_token_requests, unknown_cost_requests
                     FROM user_budget_usage WHERE user_id = ?1 AND window_start >= ?2
                     ORDER BY window_start DESC, model_id LIMIT ?3",
                )?;
                statement
                    .query_map(params![user_id, since_utc, i64::from(limit)], |row| {
                        let window: String = row.get(1)?;
                        Ok(UserBudgetUsage {
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
            .map_err(UserBudgetError::Database)
    }
}

#[derive(Debug, Clone)]
pub(crate) struct BudgetPermit {
    pub policy_version: i64,
    pub day_start: String,
    pub month_start: String,
}

/// Conservative price provenance recorded with each held reservation.
/// Only the operator tariff is ever recorded; estimated holds keep a NULL
/// price source so estimates can never be mistaken for a hard price.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BudgetPriceSource {
    OperatorTariff,
}

impl BudgetPriceSource {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::OperatorTariff => "operator_tariff",
        }
    }
}

/// Per-request worst-case proof assembled by the coordinator from admission
/// facts (input/output bounds), the operator tariff snapshot for the selected
/// provider, and nothing else. Every value is an immutable request property;
/// the publication transaction only decides against it, so there is no
/// check-then-dispatch gap.
///
/// Formal accounting assumptions (M008 corrective):
/// - `input_worst_case_tokens` is a byte-length upper bound on input tokens
///   (every token encodes at least one ingress byte), never an average.
/// - `requested_output_tokens` is the caller's explicit compatible output cap
///   for the request surface; `None` means unbounded or unusable.
/// - One attempt can bill at most `input + output`. The initial hold covers
///   two attempt-equivalents (the concrete attempt plus one selector/overhead
///   equivalent, because selector dispatch is unattributed by design); each
///   retry extends by one equivalent after re-checking.
/// - Hard USD converts worst-case tokens through the operator-configured
///   provider tariff only. Estimator fallbacks (EWMA/family/global generic)
///   are never a hard price.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BudgetProof {
    pub input_worst_case_tokens: i64,
    pub requested_output_tokens: Option<i64>,
    pub tariff_microdollars_per_million_tokens: Option<i64>,
}

/// Initial hard-mode hold covers two single-attempt equivalents; each retry
/// extends by one equivalent after re-checking limits.
pub(crate) const HARD_HOLD_INITIAL_EQUIVALENTS: i128 = 2;
pub(crate) const HARD_HOLD_RETRY_EQUIVALENTS: i128 = 1;
const MICRODOLLARS_PER_MILLION_TOKENS: i128 = 1_000_000;

/// Token/cost amounts the publication transaction must hold, plus the
/// auditable worst-case facts recorded with the reservation row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct BudgetHold {
    pub tokens: i64,
    pub microdollars: i64,
    pub enforcement: BudgetEnforcement,
    pub bound_tokens: i64,
    pub bound_microdollars: i64,
    pub price_source: Option<BudgetPriceSource>,
}

/// Worst-case tokens billable by one attempt under a hard rule, or `None`
/// when no conservative bound exists (unbounded output, negative input,
/// non-positive or overflowing total). `None` always fails closed.
pub(crate) fn single_attempt_worst_tokens(proof: &BudgetProof) -> Option<i64> {
    let output = proof.requested_output_tokens.filter(|value| *value > 0)?;
    if proof.input_worst_case_tokens < 0 {
        return None;
    }
    let single = i128::from(proof.input_worst_case_tokens) + i128::from(output);
    if single <= 0 || single > i128::from(i64::MAX) {
        return None;
    }
    Some(single as i64)
}

/// Worst-case microdollars for `tokens` at the operator tariff, or `None`
/// when the tariff is unknown/negative or the conversion overflows.
pub(crate) fn tariff_cost_microdollars(
    tokens: i64,
    tariff_per_million: Option<i64>,
) -> Option<i64> {
    let tariff = tariff_per_million.filter(|value| *value >= 0)?;
    if tokens < 0 {
        return None;
    }
    let numerator = i128::from(tokens) * i128::from(tariff);
    let cost = numerator / MICRODOLLARS_PER_MILLION_TOKENS
        + i128::from(numerator % MICRODOLLARS_PER_MILLION_TOKENS != 0);
    if cost > i128::from(i64::MAX) {
        return None;
    }
    Some(cost as i64)
}

pub(crate) struct BudgetReservationSpec<'a> {
    pub request_id: i64,
    pub user_id: &'a str,
    pub model_id: &'a str,
    pub tokens: i64,
    pub microdollars: i64,
    pub ttl_seconds: i64,
    pub hold: BudgetHold,
}

struct BudgetCheckRequest<'a> {
    user_id: &'a str,
    permit: &'a BudgetPermit,
    new_request: bool,
}

struct LoadedBudgetRule {
    scope_model: Option<String>,
    window: String,
    max_requests: Option<i64>,
    max_tokens: Option<i64>,
    max_cost: Option<i64>,
    max_concurrent: Option<i64>,
    enforcement: BudgetEnforcement,
}

pub(crate) struct BudgetAttemptSettlement {
    pub request_id: i64,
    pub attempt_id: i64,
    pub attempt_transitioned: bool,
    pub terminalize_request: bool,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost_microdollars: i64,
    pub provider_reported_microdollars: Option<i64>,
    pub local_estimated_microdollars: Option<i64>,
    pub tokens_known: bool,
    pub cost_known: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BudgetCheck {
    Allowed,
    Exceeded,
    /// A hard rule applies but its worst case is unprovable (unbounded
    /// output, unknown tariff, overflow). Callers must deny before any
    /// upstream submission; this is never coerced into an estimate.
    NotEnforceable,
}

pub(crate) fn check_initial(
    connection: &tokio_rusqlite::rusqlite::Connection,
    user_id: &str,
    model_id: &str,
    tokens: i64,
    microdollars: i64,
    proof: Option<&BudgetProof>,
) -> Result<(BudgetCheck, BudgetPermit, BudgetHold), tokio_rusqlite::rusqlite::Error> {
    let (policy_version, day_start, month_start): (i64, String, String) = connection.query_row(
        "SELECT revision, date('now'), strftime('%Y-%m-01', 'now') FROM policy_state WHERE singleton = 1",
        [],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;
    let permit = BudgetPermit {
        policy_version,
        day_start,
        month_start,
    };
    let rules = load_matching_rules(connection, user_id, model_id)?;
    let hard_token = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_tokens.is_some());
    let hard_cost = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_cost.is_some());
    let Some(hold) = hold_for_admission(tokens, microdollars, proof, hard_token, hard_cost) else {
        return Ok((BudgetCheck::NotEnforceable, permit, BudgetHold::empty()));
    };
    let exceeded = rules_exceeded(
        connection,
        BudgetCheckRequest {
            user_id,
            permit: &permit,
            new_request: true,
        },
        &rules,
        hold,
        tokens,
        microdollars,
    )?;
    Ok((
        if exceeded {
            BudgetCheck::Exceeded
        } else {
            BudgetCheck::Allowed
        },
        permit,
        hold,
    ))
}

/// Resolve the amounts a new logical request must hold. Hard dimensions use
/// the proof's worst case; estimated dimensions keep the estimator's
/// projection. Returns `None` when a hard dimension is unprovable.
pub(crate) fn hold_for_admission(
    estimate_tokens: i64,
    estimate_microdollars: i64,
    proof: Option<&BudgetProof>,
    hard_token: bool,
    hard_cost: bool,
) -> Option<BudgetHold> {
    let single = proof.and_then(single_attempt_worst_tokens);
    let hold_tokens = if hard_token {
        let single = single?;
        checked_mul(single, HARD_HOLD_INITIAL_EQUIVALENTS)?
    } else {
        estimate_tokens.max(0)
    };
    let hold_microdollars = if hard_cost {
        let tariff = proof.and_then(|proof| proof.tariff_microdollars_per_million_tokens);
        // A hard cost cap still needs a conservative token proof when token
        // count itself is estimated. Estimates can be below the raw-body
        // bound or capped for resource reasons, so they cannot justify a hard
        // monetary reservation.
        let cost_tokens = checked_mul(single?, HARD_HOLD_INITIAL_EQUIVALENTS)?;
        tariff_cost_microdollars(cost_tokens, tariff)?
    } else {
        estimate_microdollars.max(0)
    };
    let enforcement = if hard_token || hard_cost {
        BudgetEnforcement::Hard
    } else {
        BudgetEnforcement::Estimated
    };
    Some(BudgetHold {
        tokens: hold_tokens,
        microdollars: hold_microdollars,
        enforcement,
        bound_tokens: if hard_token { hold_tokens } else { 0 },
        bound_microdollars: if hard_cost { hold_microdollars } else { 0 },
        price_source: if hard_cost {
            Some(BudgetPriceSource::OperatorTariff)
        } else {
            None
        },
    })
}

/// Resolve the additional amounts one retry attempt must extend. The initial
/// hold already covers two equivalents, so each retry adds one single-attempt
/// equivalent under hard rules after re-checking limits.
pub(crate) fn hold_for_retry(
    estimate_tokens: i64,
    estimate_microdollars: i64,
    proof: Option<&BudgetProof>,
    hard_token: bool,
    hard_cost: bool,
) -> Option<BudgetHold> {
    let single = proof.and_then(single_attempt_worst_tokens);
    let extra_tokens = if hard_token {
        let single = single?;
        checked_mul(single, HARD_HOLD_RETRY_EQUIVALENTS)?
    } else {
        estimate_tokens.max(0)
    };
    let extra_microdollars = if hard_cost {
        let tariff = proof.and_then(|proof| proof.tariff_microdollars_per_million_tokens);
        let cost_tokens = checked_mul(single?, HARD_HOLD_RETRY_EQUIVALENTS)?;
        tariff_cost_microdollars(cost_tokens, tariff)?
    } else {
        estimate_microdollars.max(0)
    };
    let enforcement = if hard_token || hard_cost {
        BudgetEnforcement::Hard
    } else {
        BudgetEnforcement::Estimated
    };
    Some(BudgetHold {
        tokens: extra_tokens,
        microdollars: extra_microdollars,
        enforcement,
        bound_tokens: if hard_token { extra_tokens } else { 0 },
        bound_microdollars: if hard_cost { extra_microdollars } else { 0 },
        price_source: if hard_cost {
            Some(BudgetPriceSource::OperatorTariff)
        } else {
            None
        },
    })
}

pub(crate) fn checked_mul(value: i64, factor: i128) -> Option<i64> {
    let product = i128::from(value) * factor;
    if product < 0 || product > i128::from(i64::MAX) {
        return None;
    }
    Some(product as i64)
}

impl BudgetHold {
    pub(crate) fn empty() -> Self {
        Self {
            tokens: 0,
            microdollars: 0,
            enforcement: BudgetEnforcement::Estimated,
            bound_tokens: 0,
            bound_microdollars: 0,
            price_source: None,
        }
    }
}

pub(crate) fn check_retry(
    connection: &tokio_rusqlite::rusqlite::Connection,
    request_id: i64,
    tokens: i64,
    microdollars: i64,
    proof: Option<&BudgetProof>,
) -> Result<(BudgetCheck, BudgetHold), tokio_rusqlite::rusqlite::Error> {
    let reservation = connection
        .query_row(
            "SELECT user_id, model_id, policy_version, day_start, month_start
             FROM user_budget_reservations WHERE request_id = ?1 AND status = 'active'",
            [request_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                ))
            },
        )
        .optional()?;
    let Some((user_id, model_id, policy_version, day_start, month_start)) = reservation else {
        return Ok((BudgetCheck::Exceeded, BudgetHold::empty()));
    };
    let permit = BudgetPermit {
        policy_version,
        day_start,
        month_start,
    };
    // Hard flags come from the current rules, not the original permit: a
    // rule promoted to hard after admission must still gate the retry.
    let rules = load_matching_rules(connection, &user_id, &model_id)?;
    let hard_token = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_tokens.is_some());
    let hard_cost = rules
        .iter()
        .any(|rule| rule.enforcement == BudgetEnforcement::Hard && rule.max_cost.is_some());
    let Some(hold) = hold_for_retry(tokens, microdollars, proof, hard_token, hard_cost) else {
        return Ok((BudgetCheck::NotEnforceable, BudgetHold::empty()));
    };
    let exceeded = rules_exceeded(
        connection,
        BudgetCheckRequest {
            user_id: &user_id,
            permit: &permit,
            new_request: false,
        },
        &rules,
        hold,
        tokens,
        microdollars,
    )?;
    Ok((
        if exceeded {
            BudgetCheck::Exceeded
        } else {
            BudgetCheck::Allowed
        },
        hold,
    ))
}

pub(crate) fn insert_reservation(
    connection: &tokio_rusqlite::rusqlite::Connection,
    spec: BudgetReservationSpec<'_>,
    permit: &BudgetPermit,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    connection.execute(
        "INSERT INTO user_budget_reservations(
            request_id, user_id, model_id, policy_version, day_start, month_start,
            held_tokens, held_microdollars, status, expires_at,
            enforcement, bound_tokens, bound_microdollars, price_source)
          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'active',
                  datetime('now', '+' || ?9 || ' seconds'), ?10, ?11, ?12, ?13)",
        params![
            spec.request_id,
            spec.user_id,
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
        "UPDATE user_budget_reservations SET held_tokens = held_tokens + ?1,
             held_microdollars = held_microdollars + ?2
         WHERE request_id = ?3 AND status = 'active'",
        params![tokens, microdollars, request_id],
    )?;
    if changed != 1 {
        return Err(tokio_rusqlite::rusqlite::Error::InvalidParameterName(
            "active user budget reservation missing".to_owned(),
        ));
    }
    Ok(())
}

fn load_matching_rules(
    connection: &tokio_rusqlite::rusqlite::Connection,
    user_id: &str,
    model_id: &str,
) -> Result<Vec<LoadedBudgetRule>, tokio_rusqlite::rusqlite::Error> {
    let mut statement = connection.prepare(
        "SELECT model_id, window, max_requests, max_tokens, max_microdollars,
                max_concurrent, enforcement
          FROM user_budget_rules WHERE user_id = ?1 AND (model_id IS NULL OR model_id = ?2)",
    )?;
    statement
        .query_map(params![user_id, model_id], |row| {
            let enforcement: String = row.get(6)?;
            Ok(LoadedBudgetRule {
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

fn rules_exceeded(
    connection: &tokio_rusqlite::rusqlite::Connection,
    check: BudgetCheckRequest<'_>,
    rules: &[LoadedBudgetRule],
    hold: BudgetHold,
    estimate_tokens: i64,
    estimate_microdollars: i64,
) -> Result<bool, tokio_rusqlite::rusqlite::Error> {
    for rule in rules {
        let window_start = if rule.window == "day" {
            &check.permit.day_start
        } else {
            &check.permit.month_start
        };
        let window = rule.window.as_str();
        // A hard row enforces its token/cost caps against the worst-case
        // hold; an estimated row keeps the estimator projection even when a
        // sibling hard row reserves worst-case amounts. Request and
        // concurrency caps are strictly enforceable in both modes.
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
        let model_filter = rule.scope_model.as_deref();
        let (used_requests, used_tokens, used_cost): (i64, i64, i64) =
            if let Some(model) = model_filter {
                connection.query_row(
                    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),
                        coalesce(sum(cost_microdollars), 0)
                 FROM user_budget_usage WHERE user_id = ?1 AND model_id = ?2
                   AND window = ?3 AND window_start = ?4",
                    params![check.user_id, model, window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )?
            } else {
                connection.query_row(
                    "SELECT coalesce(sum(request_count), 0), coalesce(sum(token_count), 0),
                        coalesce(sum(cost_microdollars), 0)
                 FROM user_budget_usage WHERE user_id = ?1 AND window = ?2 AND window_start = ?3",
                    params![check.user_id, window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )?
            };
        let (held_requests, held_tokens, held_cost, active_count): (i64, i64, i64, i64) =
            if let Some(model) = model_filter {
                connection.query_row(
                    "SELECT coalesce(sum(CASE WHEN (?3 = 'day' AND day_start = ?4)
                                              OR (?3 = 'month' AND month_start = ?4)
                                         THEN held_requests ELSE 0 END), 0),
                            coalesce(sum(CASE WHEN (?3 = 'day' AND day_start = ?4)
                                              OR (?3 = 'month' AND month_start = ?4)
                                         THEN held_tokens ELSE 0 END), 0),
                            coalesce(sum(CASE WHEN (?3 = 'day' AND day_start = ?4)
                                              OR (?3 = 'month' AND month_start = ?4)
                                         THEN held_microdollars ELSE 0 END), 0), count(*)
                     FROM user_budget_reservations WHERE user_id = ?1 AND model_id = ?2
                       AND status = 'active'",
                    params![check.user_id, model, window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )?
            } else {
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
                     FROM user_budget_reservations WHERE user_id = ?1 AND status = 'active'",
                    params![check.user_id, window, window_start],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )?
            };
        let request_hold = i64::from(check.new_request);
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
                .is_some_and(|limit| exceeds(limit, active_count, i64::from(check.new_request), 0))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Record one transitioned attempt's usage facts idempotently. The attempt
/// table is keyed by attempt/request only, so both user and instance-global
/// settlement fold the same rows.
#[allow(clippy::too_many_arguments)]
pub(crate) fn record_attempt_usage(
    connection: &tokio_rusqlite::rusqlite::Connection,
    request_id: i64,
    attempt_id: i64,
    input_tokens: i64,
    output_tokens: i64,
    cost_microdollars: i64,
    provider_reported_microdollars: Option<i64>,
    local_estimated_microdollars: Option<i64>,
    tokens_known: bool,
    cost_known: bool,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    let provider_cost = provider_reported_microdollars.unwrap_or(0).max(0);
    let local_cost = local_estimated_microdollars
        .or_else(|| {
            (provider_reported_microdollars.is_none() && cost_microdollars > 0)
                .then_some(cost_microdollars)
        })
        .unwrap_or(0)
        .max(0);
    let cost_source = if provider_reported_microdollars.is_some() {
        "provider_reported"
    } else if local_cost > 0 {
        "local_estimated"
    } else {
        "unknown"
    };
    connection.execute(
        "INSERT OR IGNORE INTO user_budget_attempt_usage(
            attempt_id, request_id, input_tokens, output_tokens, cost_microdollars,
            provider_reported_microdollars, local_estimated_microdollars,
            cost_source, tokens_known, cost_known)
          VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        params![
            attempt_id,
            request_id,
            input_tokens.max(0),
            output_tokens.max(0),
            cost_microdollars.max(0),
            provider_cost,
            local_cost,
            cost_source,
            i64::from(tokens_known),
            i64::from(cost_known),
        ],
    )?;
    Ok(())
}

pub(crate) fn record_attempt_and_maybe_settle(
    connection: &tokio_rusqlite::rusqlite::Connection,
    settlement: BudgetAttemptSettlement,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    let BudgetAttemptSettlement {
        request_id,
        attempt_id,
        attempt_transitioned,
        terminalize_request,
        input_tokens,
        output_tokens,
        cost_microdollars,
        provider_reported_microdollars,
        local_estimated_microdollars,
        tokens_known,
        cost_known,
    } = settlement;
    // Per-attempt usage facts are request-scoped, not user-scoped: they are
    // recorded unconditionally so instance-global settlement (M009) can fold
    // the same attempts for unattributed requests. Only the usage-bucket
    // fold below is gated on a user reservation.
    if attempt_transitioned {
        record_attempt_usage(
            connection,
            request_id,
            attempt_id,
            input_tokens,
            output_tokens,
            cost_microdollars,
            provider_reported_microdollars,
            local_estimated_microdollars,
            tokens_known,
            cost_known,
        )?;
    }
    let reservation = connection
        .query_row(
            "SELECT user_id, model_id, day_start, month_start, held_tokens,
                    held_microdollars, status
             FROM user_budget_reservations WHERE request_id = ?1",
            [request_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, i64>(5)?,
                    row.get::<_, String>(6)?,
                ))
            },
        )
        .optional()?;
    let Some((user_id, model_id, day_start, month_start, held_tokens, held_cost, status)) =
        reservation
    else {
        return Ok(());
    };
    if !terminalize_request || status == "settled" || status == "released" {
        return Ok(());
    }
    if status != "active" {
        return Err(tokio_rusqlite::rusqlite::Error::InvalidParameterName(
            "unknown user budget reservation state".to_owned(),
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
            "INSERT INTO user_budget_usage(
                user_id, model_id, window, window_start, request_count, token_count,
                cost_microdollars, provider_reported_microdollars,
                local_estimated_microdollars, reservation_fallback_microdollars,
                unknown_token_requests, unknown_cost_requests)
             VALUES (?1, ?2, ?3, ?4, 1, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
             ON CONFLICT(user_id, model_id, window, window_start) DO UPDATE SET
                request_count = user_budget_usage.request_count + 1,
                token_count = user_budget_usage.token_count + excluded.token_count,
                cost_microdollars = user_budget_usage.cost_microdollars + excluded.cost_microdollars,
                provider_reported_microdollars = user_budget_usage.provider_reported_microdollars + excluded.provider_reported_microdollars,
                local_estimated_microdollars = user_budget_usage.local_estimated_microdollars + excluded.local_estimated_microdollars,
                reservation_fallback_microdollars = user_budget_usage.reservation_fallback_microdollars + excluded.reservation_fallback_microdollars,
                unknown_token_requests = user_budget_usage.unknown_token_requests + excluded.unknown_token_requests,
                unknown_cost_requests = user_budget_usage.unknown_cost_requests + excluded.unknown_cost_requests,
                updated_at = CURRENT_TIMESTAMP",
            params![
                user_id,
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
        "UPDATE user_budget_reservations SET status = 'settled', settled_at = CURRENT_TIMESTAMP,
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

pub(crate) fn exceeds(limit: i64, first: i64, second: i64, third: i64) -> bool {
    i128::from(first) + i128::from(second) + i128::from(third) > i128::from(limit)
}

fn parse_window(value: &str) -> BudgetWindow {
    if value == "month" {
        BudgetWindow::Month
    } else {
        BudgetWindow::Day
    }
}

#[cfg(test)]
mod hard_cost_tests {
    use super::*;

    #[test]
    fn hard_cost_holds_round_up_for_initial_and_retry_attempts() {
        let proof = BudgetProof {
            input_worst_case_tokens: 0,
            requested_output_tokens: Some(1),
            tariff_microdollars_per_million_tokens: Some(1),
        };

        assert_eq!(tariff_cost_microdollars(1, Some(1)), Some(1));
        assert_eq!(tariff_cost_microdollars(0, Some(1)), Some(0));
        let initial = hold_for_admission(1, 0, Some(&proof), false, true)
            .expect("known hard tariff is provable");
        assert_eq!(initial.microdollars, 1);
        assert_eq!(initial.bound_microdollars, 1);

        let retry = hold_for_retry(1, 0, Some(&proof), false, true)
            .expect("known retry tariff is provable");
        assert_eq!(retry.microdollars, 1);
        assert_eq!(retry.bound_microdollars, 1);

        let combined = hold_for_admission(1, 0, Some(&proof), true, true)
            .expect("hard token and cost bounds are provable");
        assert_eq!(combined.tokens, 2);
        assert_eq!(combined.microdollars, 1);
    }

    #[test]
    fn cost_hard_holds_require_and_use_conservative_proof_tokens() {
        let proof = BudgetProof {
            input_worst_case_tokens: 100_000,
            requested_output_tokens: Some(100_000),
            tariff_microdollars_per_million_tokens: Some(1_000_000),
        };

        let initial = hold_for_admission(1, 0, Some(&proof), false, true)
            .expect("hard cost requires a usable token proof");
        assert_eq!(initial.tokens, 1);
        assert_eq!(initial.microdollars, 400_000);
        assert_eq!(initial.bound_microdollars, 400_000);

        let retry = hold_for_retry(1, 0, Some(&proof), false, true)
            .expect("retry cost uses the single-attempt proof bound");
        assert_eq!(retry.tokens, 1);
        assert_eq!(retry.microdollars, 200_000);
        assert_eq!(retry.bound_microdollars, 200_000);

        let unbounded = BudgetProof {
            requested_output_tokens: None,
            ..proof
        };
        assert!(hold_for_admission(1, 0, Some(&unbounded), false, true).is_none());
        assert!(hold_for_retry(1, 0, Some(&unbounded), false, true).is_none());
    }
}
