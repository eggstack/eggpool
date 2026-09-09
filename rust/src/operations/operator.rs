//! O007 operator-facing services.
//!
//! These functions are deliberately presentation-light: SQL, catalog refresh,
//! routing eligibility, pricing, and model-info mutation stay behind reusable
//! operation boundaries so the CLI is only an adapter.

use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

use serde::Serialize;
use serde_json::{Value, json};
use tokio_rusqlite::{OptionalExtension, rusqlite::params};

use crate::{
    Config,
    db::{Database, DatabaseError},
    routing::RoutingRequestFacts,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory},
};

pub const VALID_PERIODS: [&str; 4] = ["1h", "24h", "7d", "30d"];
pub const VALID_BUCKETS: [&str; 2] = ["hour", "day"];
pub const VALID_GROUPS: [&str; 4] = ["provider", "model", "provider_model", "account"];
pub const VALID_MODEL_INFO_STATUSES: [&str; 6] = [
    "fresh",
    "partial",
    "sparse_new",
    "stale",
    "conflicting",
    "unmatched",
];
pub const MAX_OPERATOR_ROWS: u32 = 10_000;

pub fn account_list(config: &Config) -> Vec<Value> {
    config
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            let provider_id = provider_id.clone();
            provider.accounts.iter().map(move |account| {
                json!({
                    "name": account.name,
                    "provider": provider_id,
                    "routing_priority": provider.routing_priority,
                    "enabled": account.enabled,
                    "weight": account.weight,
                    "credential_configured": account.api_key.as_ref().is_some_and(|key| !key.trim().is_empty()) || (!account.api_key_env.is_empty() && std::env::var(&account.api_key_env).is_ok()),
                    "api_key_env": account.api_key_env,
                })
            })
        })
        .collect()
}

pub fn account_status(config: &Config) -> Vec<Value> {
    account_list(config)
        .into_iter()
        .map(|mut row| {
            let status = if !row["enabled"].as_bool().unwrap_or(false) {
                "disabled"
            } else if !row["credential_configured"].as_bool().unwrap_or(false) {
                "missing_credential"
            } else {
                "configured"
            };
            row["status"] = Value::String(status.into());
            row
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CatalogRefreshSummary {
    pub model_count: usize,
    pub new_model_count: usize,
    pub withdrawn_model_count: usize,
    pub successful_accounts: usize,
    pub failed_accounts: usize,
    pub skipped_accounts: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CostChange {
    pub request_id: i64,
    pub model_id: String,
    pub provider_id: String,
    pub old_cost: i64,
    pub new_cost: i64,
    pub old_exactness: String,
    pub new_exactness: String,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CostSummary {
    pub scanned: usize,
    pub updated: usize,
    pub skipped: usize,
    pub skipped_no_snapshot: usize,
    pub skipped_missing_tokens: usize,
    pub old_total: i64,
    pub new_total: i64,
    pub changes: Vec<CostChange>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RepairSummary {
    pub scanned: usize,
    pub suspicious: usize,
    pub repaired: usize,
    pub skipped_provider_reported: usize,
    pub unchanged: usize,
    pub old_total: i64,
    pub proposed_total: i64,
    pub changes: Vec<CostChange>,
    pub breakdown: Vec<RepairBreakdown>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RepairBreakdown {
    pub provider_id: String,
    pub account_name: Option<String>,
    pub model_id: String,
    pub rows: usize,
    pub old_total: i64,
    pub new_total: i64,
    pub delta: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ExplainQuery {
    pub name: String,
    pub plan: Vec<String>,
}

pub fn validate_period(period: &str) -> Result<(), String> {
    VALID_PERIODS
        .contains(&period)
        .then_some(())
        .ok_or_else(|| format!("invalid period {period:?}; expected 1h, 24h, 7d, or 30d"))
}

pub fn validate_dashboard_options(
    period: &str,
    bucket: &str,
    group_by: &str,
) -> Result<(), String> {
    validate_period(period)?;
    VALID_BUCKETS
        .contains(&bucket)
        .then_some(())
        .ok_or_else(|| format!("invalid bucket {bucket:?}; expected hour or day"))?;
    VALID_GROUPS
        .contains(&group_by)
        .then_some(())
        .ok_or_else(|| format!("invalid group-by {group_by:?}"))
}

pub fn validate_model_info_status(status: &str) -> Result<(), String> {
    VALID_MODEL_INFO_STATUSES
        .contains(&status)
        .then_some(())
        .ok_or_else(|| format!("invalid model-info status {status:?}"))
}

pub async fn refresh_catalog(
    config: &Config,
    config_path: &Path,
    database: &Database,
) -> Result<CatalogRefreshSummary, String> {
    let process = ProcessRuntime::new_with_config(database.clone(), config)
        .map_err(|error| error.to_string())?;
    let prepared =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "operator".to_owned(), 1)
            .await
            .map_err(|error| error.to_string())?;
    let Some(generation) = prepared.generation() else {
        return Err("operator generation unavailable".into());
    };
    let Some(service) = generation.inference().catalog_service() else {
        return Err("catalog service unavailable".into());
    };
    let result = service.refresh().await.map_err(|error| error.to_string());
    let summary = result.map(|result| {
        let successful_accounts = result
            .outcomes
            .values()
            .filter(|outcome| {
                matches!(
                    outcome,
                    crate::catalog::RefreshOutcome::SuccessEmpty
                        | crate::catalog::RefreshOutcome::SuccessPartial
                        | crate::catalog::RefreshOutcome::SuccessAuthoritative
                )
            })
            .count();
        let failed_accounts = result
            .outcomes
            .values()
            .filter(|outcome| matches!(outcome, crate::catalog::RefreshOutcome::Failed))
            .count();
        let skipped_accounts = result
            .outcomes
            .values()
            .filter(|outcome| matches!(outcome, crate::catalog::RefreshOutcome::Skipped))
            .count();
        CatalogRefreshSummary {
            model_count: result.live_model_ids.len(),
            new_model_count: result.new_model_ids.len(),
            withdrawn_model_count: result.withdrawn_model_ids.len(),
            successful_accounts,
            failed_accounts,
            skipped_accounts,
        }
    });
    let _ = config_path;
    let _ = prepared.abort().await;
    summary
}

pub async fn explain_accounts(
    config: &Config,
    database: &Database,
    model_id: &str,
    provider_id: Option<&str>,
    protocol: Option<&str>,
    include_scores: bool,
    include_gates: bool,
) -> Result<Value, String> {
    let process = ProcessRuntime::new_with_config(database.clone(), config)
        .map_err(|error| error.to_string())?;
    let prepared = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "operator-explain".to_owned(),
        1,
    )
    .await
    .map_err(|error| error.to_string())?;
    let Some(generation) = prepared.generation() else {
        return Err("operator generation unavailable".into());
    };
    let known_providers: BTreeSet<String> = config.providers.keys().cloned().collect();
    let mut facts = RoutingRequestFacts::from_model_id(model_id, &known_providers);
    facts.provider_id = provider_id.map(str::to_owned).or(facts.provider_id);
    facts.requested_protocol = protocol.map(str::to_owned);
    facts.client_protocol = protocol.map(str::to_owned);
    facts.now = now_timestamp().parse().unwrap_or_default();
    let plan = generation
        .inference()
        .router_handle()
        .build_routing_plan(&facts);
    let eligible = plan
        .candidates
        .iter()
        .map(|candidate| candidate.account_name.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    let exclusions = plan
        .exclusions
        .iter()
        .map(|exclusion| {
            (
                exclusion.account_name.as_str(),
                exclusion.reason_code.as_str(),
            )
        })
        .collect::<BTreeMap<_, _>>();
    let accounts = account_status(config)
        .into_iter()
        .map(|mut account| {
            let name = account["name"].as_str().unwrap_or_default().to_owned();
            let is_eligible = eligible.contains(name.as_str());
            let reason = exclusions.get(name.as_str()).copied().unwrap_or(if is_eligible {
                "eligible"
            } else {
                "not_eligible"
            });
            account["eligible"] = Value::Bool(is_eligible);
            account["reason_code"] = Value::String(reason.into());
            if include_gates {
                account["gates"] = json!({
                    "config_enabled": account["enabled"],
                    "credentials_usable": account["credential_configured"],
                    "provider_match": provider_id.is_none_or(|provider| account["provider"] == provider),
                    "protocol_match": protocol.map_or(Value::Null, |requested| Value::Bool(plan.candidates.iter().any(|candidate| candidate.account_name == name && candidate.protocol.as_deref().is_none_or(|value| value == requested)))),
                    "final_eligible": is_eligible,
                });
            }
            account
        })
        .collect::<Vec<_>>();
    let mut result = json!({
        "model_id": model_id,
        "provider": provider_id,
        "protocol": protocol,
        "accounts": accounts,
        "candidates": plan.candidates,
        "exclusions": plan.exclusions,
        "fairness": plan.fairness,
        "catalog_version": plan.catalog_version,
        "health_version": plan.health_version,
    });
    if !include_scores {
        if let Some(candidates) = result.get_mut("candidates").and_then(Value::as_array_mut) {
            for candidate in candidates {
                if let Some(object) = candidate.as_object_mut() {
                    object.remove("score");
                }
            }
        }
    }
    if !include_gates {
        result
            .as_object_mut()
            .map(|object| object.remove("fairness"));
    }
    let _ = prepared.abort().await;
    Ok(result)
}

pub async fn refresh_model_info_from_catalog(
    _config: &Config,
    database: &Database,
) -> Result<(usize, usize), DatabaseError> {
    let rows: Vec<(String, Option<String>, String, String, String)> = database.call(|connection| {
        let mut statement = connection.prepare("SELECT model_id, display_name, protocol, capabilities, source_metadata FROM models WHERE model_id != '__deprecated__' ORDER BY model_id LIMIT ?1")?;
        statement.query_map([i64::from(MAX_OPERATOR_ROWS)], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)))?.collect()
    }).await?;
    let (created, updated) = database.with_transaction(move |connection| {
        let mut created = 0;
        let mut updated = 0;
        for (model_id, display_name, protocol, capabilities, source_metadata) in rows {
            let mut detail = object_from_text(&capabilities);
            let source = object_from_text(&source_metadata);
            if !source.is_empty() { detail.insert("source_metadata".into(), Value::Object(source.clone())); }
            detail.insert("protocol".into(), Value::String(protocol));
            if let Some(display_name) = display_name { detail.insert("display_name".into(), Value::String(display_name)); }
            let detail_json = serde_json::to_string(&Value::Object(detail)).unwrap_or_else(|_| "{}".into());
            let provenance = json!({"sources": {"provider_catalog": {"source": "provider_catalog"}}}).to_string();
            let changed = connection.execute(
                "INSERT INTO model_info_canonical (model_id,status,summary,detail_json,provenance_json,conflicts_json,sparse,first_seen_at,last_seen_at,last_refreshed_at,next_refresh_at) VALUES (?1,'fresh',?2,?3,?4,'{}',0,datetime('now'),datetime('now'),datetime('now'),datetime('now','+1 day')) ON CONFLICT(model_id) DO UPDATE SET status='fresh',summary=excluded.summary,detail_json=excluded.detail_json,provenance_json=excluded.provenance_json,last_seen_at=excluded.last_seen_at,last_refreshed_at=excluded.last_refreshed_at,next_refresh_at=excluded.next_refresh_at",
                params![model_id, format!("Provider catalog model {model_id}"), detail_json, provenance],
            )?;
            if changed == 1 { created += 1; } else { updated += 1; }
        }
        Ok((created, updated))
    }).await?;
    Ok((created, updated))
}

pub async fn list_model_info(
    database: &Database,
    status: Option<&str>,
) -> Result<Vec<Value>, DatabaseError> {
    let status = status.map(str::to_owned);
    database.call(move |connection| {
        let mut statement = connection.prepare("SELECT model_id,status,summary,detail_json,provenance_json,conflicts_json,sparse,first_seen_at,last_seen_at,last_refreshed_at,next_refresh_at FROM model_info_canonical WHERE (?1 IS NULL OR status = ?1) ORDER BY status, model_id LIMIT ?2")?;
        statement.query_map(params![status, i64::from(MAX_OPERATOR_ROWS)], model_info_value)?.collect()
    }).await
}

pub async fn show_model_info(
    database: &Database,
    model_id: &str,
) -> Result<Option<Value>, DatabaseError> {
    let model_id = model_id.to_owned();
    database.call(move |connection| connection.query_row("SELECT model_id,status,summary,detail_json,provenance_json,conflicts_json,sparse,first_seen_at,last_seen_at,last_refreshed_at,next_refresh_at FROM model_info_canonical WHERE lower(model_id)=lower(?1)", [model_id], model_info_value).optional()).await
}

pub async fn list_aliases(
    database: &Database,
    model_id: &str,
    source: Option<&str>,
) -> Result<Vec<Value>, DatabaseError> {
    let model_id = model_id.to_owned();
    let source = source.map(str::to_owned);
    database.call(move |connection| {
        let mut statement = connection.prepare("SELECT source,alias,provider_id,confidence,active,last_seen_at FROM model_info_aliases WHERE lower(model_id)=lower(?1) AND (?2 IS NULL OR source=?2) ORDER BY source,last_seen_at DESC LIMIT ?3")?;
        statement.query_map(params![model_id, source, i64::from(MAX_OPERATOR_ROWS)], |row| Ok(json!({"source": row.get::<_, String>(0)?, "alias": row.get::<_, String>(1)?, "provider_id": row.get::<_, Option<String>>(2)?, "confidence": row.get::<_, Option<f64>>(3)?, "active": row.get::<_, i64>(4)? != 0, "last_seen_at": row.get::<_, Option<String>>(5)?})))?.collect()
    }).await
}

pub async fn seed_configured_aliases(
    config: &Config,
    database: &Database,
) -> Result<usize, DatabaseError> {
    let aliases = config.model_info.aliases.clone();
    database.with_transaction(move |connection| {
        let mut count = 0;
        for alias in aliases {
            if alias.model_id.trim().is_empty() || alias.source.trim().is_empty() || alias.source_model_id.trim().is_empty() { continue; }
            let changed = connection.execute("INSERT INTO model_info_aliases (model_id,provider_id,alias,source,confidence,active,notes) VALUES (?1,?2,?3,?4,1.0,1,?5) ON CONFLICT(provider_id,alias,source) DO UPDATE SET model_id=excluded.model_id,confidence=excluded.confidence,active=1,last_seen_at=CURRENT_TIMESTAMP,notes=excluded.notes", params![alias.model_id, alias.provider_id, alias.source_model_id, alias.source, alias.notes])?;
            count += usize::from(changed > 0);
        }
        Ok(count)
    }).await
}

pub async fn repair_model_info(
    database: &Database,
    limit: u32,
) -> Result<(usize, usize, usize, usize), DatabaseError> {
    let limit = i64::from(limit.min(MAX_OPERATOR_ROWS));
    let rows: Vec<(String, String)> = database
        .call(move |connection| {
            let mut statement = connection.prepare(
                "SELECT model_id,detail_json FROM model_info_canonical ORDER BY model_id LIMIT ?1",
            )?;
            statement
                .query_map([limit], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect()
        })
        .await?;
    let mut upgraded = 0;
    let mut skipped = 0;
    let mut errors = 0;
    for (model_id, detail_json) in &rows {
        let Ok(mut detail) = serde_json::from_str::<Value>(detail_json) else {
            errors += 1;
            continue;
        };
        let Some(object) = detail.as_object_mut() else {
            errors += 1;
            continue;
        };
        if object.get("limits").is_some() {
            skipped += 1;
            continue;
        }
        let limits: Option<String> = database.call({ let model_id = model_id.clone(); move |connection| connection.query_row("SELECT json_object('effective_context',json_extract(capabilities,'$.context_tokens'),'external_context',json_extract(source_metadata,'$.context_window_external'),'effective_output',json_extract(capabilities,'$.max_output_tokens'),'external_output',json_extract(source_metadata,'$.max_output_tokens')) FROM models WHERE model_id=?1", [model_id], |row| row.get(0)).optional() }).await?;
        if let Some(limits) = limits.and_then(|text: String| serde_json::from_str(&text).ok()) {
            object.insert("limits".into(), limits);
        }
        let serialized = serde_json::to_string(&detail).unwrap_or_else(|_| "{}".into());
        database.with_transaction({ let model_id = model_id.clone(); move |connection| { connection.execute("UPDATE model_info_canonical SET detail_json=?1,provenance_json=json_set(provenance_json,'$.backfilled_limits',true) WHERE model_id=?2", params![serialized, model_id]).map(|_| ()) } }).await?;
        upgraded += 1;
    }
    Ok((rows.len(), upgraded, skipped, errors))
}

pub async fn transcoding_stats(database: &Database, period: &str) -> Result<Value, DatabaseError> {
    validate_period(period).map_err(|error| DatabaseError::Sqlite {
        operation: "validate period".into(),
        source: Box::new(tokio_rusqlite::rusqlite::Error::InvalidParameterName(error)),
    })?;
    let modifier = period_modifier(period);
    let (total, native, transcoded): (i64, i64, i64) = database.call(move |connection| connection.query_row("SELECT COUNT(*),COALESCE(SUM(CASE WHEN protocol=COALESCE(upstream_protocol,protocol) THEN 1 ELSE 0 END),0),COALESCE(SUM(CASE WHEN protocol!=COALESCE(upstream_protocol,protocol) AND upstream_protocol IS NOT NULL THEN 1 ELSE 0 END),0) FROM requests WHERE started_at >= datetime('now',?1) AND started_at < datetime('now')", [modifier], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?)))).await?;
    let directions: Vec<(String, String, i64)> = database.call(move |connection| { let mut statement = connection.prepare("SELECT protocol,upstream_protocol,COUNT(*) FROM requests WHERE started_at >= datetime('now',?1) AND started_at < datetime('now') AND upstream_protocol IS NOT NULL AND protocol != upstream_protocol GROUP BY protocol,upstream_protocol ORDER BY COUNT(*) DESC,protocol,upstream_protocol")?; statement.query_map([modifier], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?)))?.collect() }).await?;
    let per_direction = directions
        .into_iter()
        .map(|(client, upstream, count)| (format!("{client}→{upstream}"), count))
        .collect::<BTreeMap<_, _>>();
    Ok(
        json!({"total": total, "native_count": native, "transcoded_count": transcoded, "per_direction": per_direction, "top_loss_warnings": []}),
    )
}

pub async fn recompute_costs(
    database: &Database,
    limit: Option<u32>,
    apply: bool,
) -> Result<CostSummary, DatabaseError> {
    let limit = limit.map(|value| i64::from(value.min(MAX_OPERATOR_ROWS)));
    let rows: Vec<CostRow> = database.call(move |connection| { let sql = "SELECT id,model_id,provider_id,exactness,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,cost_microdollars,provider_cost_microdollars,reserved_microdollars,raw_usage_json FROM requests WHERE status!='pending' ORDER BY started_at DESC,id DESC"; let sql = if limit.is_some() { format!("{sql} LIMIT ?1") } else { sql.into() }; let mut statement = connection.prepare(&sql)?; let mapper = |row: &tokio_rusqlite::rusqlite::Row<'_>| CostRow::from_row(row); match limit { Some(limit) => statement.query_map([limit], mapper)?.collect(), None => statement.query_map([], mapper)?.collect() } }).await?;
    let mut summary = CostSummary {
        scanned: rows.len(),
        updated: 0,
        skipped: 0,
        skipped_no_snapshot: 0,
        skipped_missing_tokens: 0,
        old_total: 0,
        new_total: 0,
        changes: Vec::new(),
    };
    let mut updates = Vec::new();
    for row in rows {
        summary.old_total = summary.old_total.saturating_add(row.old_cost);
        if row.provider_cost.is_some() {
            summary.new_total = summary.new_total.saturating_add(row.old_cost);
            summary.skipped += 1;
            continue;
        }
        if row.tokens() == 0 {
            summary.skipped_missing_tokens += 1;
            summary.new_total = summary.new_total.saturating_add(row.old_cost);
            continue;
        }
        let Some((new_cost, exactness)) = price_cost(database, &row).await? else {
            summary.skipped_no_snapshot += 1;
            summary.new_total = summary.new_total.saturating_add(row.old_cost);
            continue;
        };
        summary.new_total = summary.new_total.saturating_add(new_cost);
        if new_cost == row.old_cost && row.exactness != "estimated" && row.exactness != "unknown" {
            summary.skipped += 1;
            continue;
        }
        summary.updated += 1;
        summary.changes.push(CostChange {
            request_id: row.id,
            model_id: row.model_id.clone(),
            provider_id: row.provider_id.clone(),
            old_cost: row.old_cost,
            new_cost,
            old_exactness: row.exactness.clone(),
            new_exactness: exactness.clone(),
            reason: None,
        });
        updates.push((row.id, new_cost, exactness));
    }
    if apply && !updates.is_empty() {
        database
            .with_transaction(move |connection| {
                for (id, cost, exactness) in updates {
                    connection.execute(
                        "UPDATE requests SET cost_microdollars=?1,exactness=?2 WHERE id=?3",
                        params![cost, exactness, id],
                    )?;
                }
                Ok(())
            })
            .await?;
    }
    Ok(summary)
}

pub async fn repair_costs(
    database: &Database,
    provider: Option<&str>,
    since: Option<&str>,
    limit: Option<u32>,
    apply: bool,
) -> Result<RepairSummary, DatabaseError> {
    let provider = provider.map(str::to_owned);
    let since = since.map(str::to_owned);
    let limit = limit.map(|value| i64::from(value.min(MAX_OPERATOR_ROWS)));
    let query_provider = provider.clone();
    let query_since = since.clone();
    let rows: Vec<CostRow> = database.call(move |connection| {
        let sql = "SELECT r.id,r.model_id,r.provider_id,r.exactness,r.input_tokens,r.output_tokens,r.cache_read_tokens,r.cache_write_tokens,r.reasoning_tokens,r.cost_microdollars,r.provider_cost_microdollars,r.reserved_microdollars,r.local_cost_microdollars,r.local_cost_exactness,a.name,r.raw_usage_json FROM requests r LEFT JOIN accounts a ON a.id=r.account_id WHERE r.status!='pending' AND (?1 IS NULL OR COALESCE(r.provider_id,'') LIKE '%'||?1||'%' OR COALESCE(a.name,'') LIKE '%'||?1||'%') AND (?2 IS NULL OR r.started_at >= ?2) ORDER BY r.cost_microdollars DESC,r.started_at DESC,r.id DESC LIMIT COALESCE(?3,-1)";
        let mut statement = connection.prepare(sql)?;
        statement.query_map(params![query_provider, query_since, limit], CostRow::from_repair_row)?.collect()
    }).await?;
    let mut summary = RepairSummary {
        scanned: rows.len(),
        suspicious: 0,
        repaired: 0,
        skipped_provider_reported: 0,
        unchanged: 0,
        old_total: 0,
        proposed_total: 0,
        changes: Vec::new(),
        breakdown: Vec::new(),
    };
    let mut updates = Vec::new();
    let mut grouped: BTreeMap<(String, Option<String>, String), RepairBreakdown> = BTreeMap::new();
    for row in rows {
        summary.old_total = summary.old_total.saturating_add(row.old_cost);
        summary.proposed_total = summary.proposed_total.saturating_add(row.old_cost);
        if row.provider_cost.is_some() {
            summary.skipped_provider_reported += 1;
            continue;
        }
        let reason = repair_reason(&row);
        let Some(reason) = reason else {
            continue;
        };
        summary.suspicious += 1;
        let local = match (row.local_cost, row.local_exactness.clone()) {
            (Some(cost), Some(exactness)) => Some((cost, exactness)),
            _ => price_cost(database, &row).await?,
        };
        let Some((local_cost, local_exactness)) = local else {
            continue;
        };
        let (new_cost, exactness) = repaired_cost(&row, local_cost, &local_exactness);
        if new_cost == row.old_cost && exactness == row.exactness {
            summary.unchanged += 1;
            continue;
        }
        summary.repaired += 1;
        summary.proposed_total = summary
            .proposed_total
            .saturating_add(new_cost - row.old_cost);
        let change = CostChange {
            request_id: row.id,
            model_id: row.model_id.clone(),
            provider_id: row.provider_id.clone(),
            old_cost: row.old_cost,
            new_cost,
            old_exactness: row.exactness.clone(),
            new_exactness: exactness.clone(),
            reason: Some(reason),
        };
        summary.changes.push(change);
        updates.push((row.id, new_cost, exactness));
        let key = (
            row.provider_id.clone(),
            row.account_name.clone(),
            row.model_id.clone(),
        );
        let item = grouped.entry(key.clone()).or_insert(RepairBreakdown {
            provider_id: key.0.clone(),
            account_name: key.1.clone(),
            model_id: key.2.clone(),
            rows: 0,
            old_total: 0,
            new_total: 0,
            delta: 0,
        });
        item.rows += 1;
        item.old_total += row.old_cost;
        item.new_total += new_cost;
        item.delta += new_cost - row.old_cost;
    }
    summary.breakdown = grouped.into_values().collect();
    summary
        .changes
        .sort_by_key(|change| std::cmp::Reverse((change.new_cost - change.old_cost).abs()));
    if apply && !updates.is_empty() {
        let audit_changes = summary.changes.clone();
        let audit_provider = provider.clone();
        let audit_since = since.clone();
        database
            .with_transaction(move |connection| {
                for (id, cost, exactness) in updates {
                    let audit = audit_changes
                        .iter()
                        .find(|change| change.request_id == id);
                    let (old_cost, old_exactness, reason) = audit
                        .map(|change| {
                            (
                                change.old_cost,
                                change.old_exactness.clone(),
                                change.reason.clone().unwrap_or_else(|| "repair".into()),
                            )
                        })
                        .unwrap_or((0, "unknown".into(), "repair".into()));
                    connection.execute(
                        "UPDATE requests SET cost_microdollars=?1,exactness=?2 WHERE id=?3",
                        params![cost, exactness, id],
                    )?;
                    connection.execute(
                        "INSERT INTO request_cost_repairs (request_id,old_cost_microdollars,new_cost_microdollars,old_exactness,new_exactness,reason,provider_filter,since_date) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
                        params![id, old_cost, cost, old_exactness, exactness, reason, audit_provider, audit_since],
                    )?;
                }
                Ok(())
            })
            .await?;
    }
    Ok(summary)
}

pub async fn explain_dashboard(
    database: &Database,
    period: &str,
    bucket: &str,
    group_by: &str,
) -> Result<Vec<ExplainQuery>, DatabaseError> {
    validate_dashboard_options(period, bucket, group_by).map_err(|error| {
        DatabaseError::Sqlite {
            operation: "validate dashboard options".into(),
            source: Box::new(tokio_rusqlite::rusqlite::Error::InvalidParameterName(error)),
        }
    })?;
    let group_expr = match group_by {
        "provider" => "r.provider_id",
        "model" => "r.model_id",
        "account" => "CAST(r.account_id AS TEXT)",
        _ => "r.provider_id || '/' || r.model_id",
    };
    let bucket_expr = if bucket == "day" {
        "strftime('%Y-%m-%d 00:00:00',r.started_at)"
    } else {
        "strftime('%Y-%m-%d %H:00:00',r.started_at)"
    };
    let modifier = period_modifier(period);
    let queries = vec![
        (
            "fetch_timeseries",
            format!(
                "SELECT {bucket_expr},COUNT(*) FROM requests r WHERE r.started_at >= datetime('now','{modifier}') AND r.started_at < datetime('now') GROUP BY 1"
            ),
        ),
        (
            "fetch_grouped_timeseries",
            format!(
                "SELECT {bucket_expr},{group_expr},COUNT(*) FROM requests r WHERE r.started_at >= datetime('now','{modifier}') AND r.started_at < datetime('now') GROUP BY 1,2"
            ),
        ),
        (
            "fetch_summary",
            format!(
                "SELECT COUNT(*),SUM(input_tokens),SUM(output_tokens),SUM(cost_microdollars) FROM requests r WHERE r.started_at >= datetime('now','{modifier}') AND r.started_at < datetime('now')"
            ),
        ),
        (
            "fetch_account_stats",
            format!(
                "SELECT r.account_id,COUNT(*) FROM requests r WHERE r.started_at >= datetime('now','{modifier}') AND r.started_at < datetime('now') GROUP BY r.account_id"
            ),
        ),
        (
            "fetch_model_stats",
            format!(
                "SELECT r.model_id,COUNT(*) FROM requests r WHERE r.started_at >= datetime('now','{modifier}') AND r.started_at < datetime('now') GROUP BY r.model_id"
            ),
        ),
        (
            "fetch_bandwidth_timeseries",
            format!(
                "SELECT {bucket_expr},SUM(bytes_received),SUM(bytes_emitted) FROM requests r WHERE r.started_at >= datetime('now','{modifier}') AND r.started_at < datetime('now') GROUP BY 1"
            ),
        ),
    ];
    let mut result = Vec::new();
    for (name, sql) in queries {
        let plan: Vec<String> = database
            .call({
                let sql = sql.clone();
                move |connection| {
                    let mut statement = connection.prepare(&format!("EXPLAIN QUERY PLAN {sql}"))?;
                    statement.query_map([], |row| row.get(3))?.collect()
                }
            })
            .await?;
        result.push(ExplainQuery {
            name: name.into(),
            plan,
        });
    }
    Ok(result)
}

type PriceRates = (Option<i64>, Option<i64>, Option<i64>, Option<i64>);

async fn price_cost(
    database: &Database,
    row: &CostRow,
) -> Result<Option<(i64, String)>, DatabaseError> {
    let model = row.model_id.clone();
    let provider = row.provider_id.clone();
    let snapshot: Option<PriceRates> = database
        .call(move |connection| connection.query_row("SELECT COALESCE(input_per_million_microdollars,CAST(input_price_per_1k * 1000000000 AS INTEGER)),COALESCE(output_per_million_microdollars,CAST(output_price_per_1k * 1000000000 AS INTEGER)),cache_read_per_million_microdollars,cache_write_per_million_microdollars FROM model_price_snapshots WHERE model_id=?1 AND provider_id=?2 ORDER BY captured_at DESC,id DESC LIMIT 1", params![model, provider], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))).optional())
        .await?;
    let Some((input, output, cache_read, cache_write)) = snapshot else {
        return Ok(None);
    };
    let regular_input = if usage_includes_cached_input(row.raw_usage.as_deref()) {
        (row.input_tokens - row.cache_read - row.cache_write).max(0)
    } else {
        row.input_tokens.max(0)
    };
    let mut cost = 0_i64;
    let categories = [
        (regular_input, input, 3_000_000_i64),
        (row.output_tokens.max(row.reasoning), output, 15_000_000_i64),
        (row.cache_read, cache_read, 300_000_i64),
        (row.cache_write, cache_write, 3_750_000_i64),
    ];
    let priced = categories
        .iter()
        .filter(|(tokens, rate, _)| *tokens > 0 && rate.is_some())
        .count();
    let nonzero = categories
        .iter()
        .filter(|(tokens, _, _)| *tokens > 0)
        .count();
    if nonzero > 0 && priced == 0 {
        return Ok(Some((
            fallback_cost(regular_input, row.output_tokens.max(row.reasoning)),
            "estimated".into(),
        )));
    }
    let mut partial = false;
    for (tokens, rate, fallback) in categories {
        if tokens <= 0 {
            continue;
        }
        let rate = rate.unwrap_or_else(|| {
            partial = true;
            fallback
        });
        cost = cost.saturating_add(
            ((tokens as i128 * rate as i128) / 1_000_000).min(250_000_000_i128) as i64,
        );
    }
    Ok(Some((
        cost.min(250_000_000),
        if partial { "partial" } else { "derived" }.into(),
    )))
}

fn fallback_cost(input_tokens: i64, output_tokens: i64) -> i64 {
    (((input_tokens.max(0) as i128 * 3_000_000_i128)
        + (output_tokens.max(0) as i128 * 15_000_000_i128))
        / 1_000_000)
        .min(250_000_000_i128) as i64
}

fn usage_includes_cached_input(raw_usage: Option<&str>) -> bool {
    raw_usage
        .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
        .and_then(|value| value.get("prompt_tokens_details").cloned())
        .and_then(|value| value.as_object().cloned())
        .is_some_and(|details| details.contains_key("cached_tokens"))
}

fn repair_reason(row: &CostRow) -> Option<String> {
    const CAP: i64 = 225_000_000;
    if row.old_cost >= CAP {
        return Some("near_request_cap".into());
    }
    let tokens = row.tokens();
    if tokens > 0 && row.old_cost / tokens.max(1) > 10_000 {
        return Some("implausible_cost_per_token".into());
    }
    if row.exactness == "estimated"
        && row.reserved > 0
        && row.old_cost > row.reserved.saturating_mul(4)
    {
        return Some("estimated_far_above_reservation".into());
    }
    if row.exactness == "estimated"
        && row.reserved > 0
        && row.old_cost == row.reserved
        && row
            .local_cost
            .is_some_and(|local| local > 0 && local < row.old_cost)
    {
        return Some("reservation_fallback_overrode_lower_local_estimate".into());
    }
    None
}

fn repaired_cost(row: &CostRow, local_cost: i64, local_exactness: &str) -> (i64, String) {
    if matches!(local_exactness, "derived" | "partial" | "exact") {
        return (local_cost.max(0), local_exactness.to_owned());
    }
    if row.tokens() <= 0 && row.old_cost <= 0 && row.reserved <= 0 {
        return (0, "unknown".into());
    }
    let tokens = row.tokens().max(1);
    let local_plausible = local_cost > 0 && local_cost <= tokens.saturating_mul(100);
    let reservation = (row.reserved > 0 && row.reserved <= 1_000_000)
        .then_some(row.reserved)
        .filter(|value| *value <= tokens.saturating_mul(100));
    let cost = match (local_plausible, reservation) {
        (true, Some(reservation)) => local_cost.min(reservation),
        (true, None) => local_cost,
        (false, Some(reservation)) => reservation,
        (false, None) => tokens.saturating_mul(5).clamp(1, 250_000_000),
    };
    (cost, "estimated".into())
}

#[derive(Debug)]
struct CostRow {
    id: i64,
    model_id: String,
    provider_id: String,
    exactness: String,
    input_tokens: i64,
    output_tokens: i64,
    cache_read: i64,
    cache_write: i64,
    reasoning: i64,
    old_cost: i64,
    provider_cost: Option<i64>,
    reserved: i64,
    local_cost: Option<i64>,
    local_exactness: Option<String>,
    account_name: Option<String>,
    raw_usage: Option<String>,
}
impl CostRow {
    fn from_row(row: &tokio_rusqlite::rusqlite::Row<'_>) -> tokio_rusqlite::rusqlite::Result<Self> {
        Ok(Self {
            id: row.get(0)?,
            model_id: row.get(1)?,
            provider_id: row
                .get::<_, Option<String>>(2)?
                .unwrap_or_else(|| "opencode-go".into()),
            exactness: row
                .get::<_, Option<String>>(3)?
                .unwrap_or_else(|| "unknown".into()),
            input_tokens: row.get::<_, Option<i64>>(4)?.unwrap_or(0),
            output_tokens: row.get::<_, Option<i64>>(5)?.unwrap_or(0),
            cache_read: row.get::<_, Option<i64>>(6)?.unwrap_or(0),
            cache_write: row.get::<_, Option<i64>>(7)?.unwrap_or(0),
            reasoning: row.get::<_, Option<i64>>(8)?.unwrap_or(0),
            old_cost: row.get::<_, Option<i64>>(9)?.unwrap_or(0),
            provider_cost: row.get(10)?,
            reserved: row.get::<_, Option<i64>>(11)?.unwrap_or(0),
            local_cost: None,
            local_exactness: None,
            account_name: None,
            raw_usage: row.get(12)?,
        })
    }
    fn from_repair_row(
        row: &tokio_rusqlite::rusqlite::Row<'_>,
    ) -> tokio_rusqlite::rusqlite::Result<Self> {
        let mut value = Self::from_row(row)?;
        value.local_cost = row.get(12)?;
        value.local_exactness = row.get(13)?;
        value.account_name = row.get(14)?;
        value.raw_usage = row.get(15)?;
        Ok(value)
    }
    fn tokens(&self) -> i64 {
        self.input_tokens.max(0)
            + self.output_tokens.max(self.reasoning).max(0)
            + self.cache_read.max(0)
            + self.cache_write.max(0)
    }
}

fn model_info_value(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<Value> {
    let detail: String = row.get(3)?;
    let provenance: String = row.get(4)?;
    let conflicts: String = row.get(5)?;
    Ok(
        json!({"model_id":row.get::<_,String>(0)?,"status":row.get::<_,String>(1)?,"summary":row.get::<_,Option<String>>(2)?,"detail":serde_json::from_str::<Value>(&detail).unwrap_or_else(|_|json!({})),"provenance":serde_json::from_str::<Value>(&provenance).unwrap_or_else(|_|json!({})),"conflicts":serde_json::from_str::<Value>(&conflicts).unwrap_or_else(|_|json!({})),"sparse":row.get::<_,i64>(6)? != 0,"first_seen_at":row.get::<_,String>(7)?,"last_seen_at":row.get::<_,String>(8)?,"last_refreshed_at":row.get::<_,Option<String>>(9)?,"next_refresh_at":row.get::<_,Option<String>>(10)?}),
    )
}
fn object_from_text(text: &str) -> serde_json::Map<String, Value> {
    serde_json::from_str::<Value>(text)
        .ok()
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}
fn now_timestamp() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or_else(|_| "0".into(), |d| d.as_secs().to_string())
}
fn period_modifier(period: &str) -> &'static str {
    match period {
        "1h" => "-1 hour",
        "7d" => "-7 days",
        "30d" => "-30 days",
        _ => "-24 hours",
    }
}
