//! Typed persistence for the existing schema-54 health tables.

use std::{
    collections::BTreeMap,
    time::{SystemTime, UNIX_EPOCH},
};

use thiserror::Error;
use tokio_rusqlite::rusqlite::{OptionalExtension, params};

use crate::db::{Database, DatabaseError};

use super::{
    BackoffReason, EvidenceProvenance, ModelQuarantine, QuarantineEntry, QuarantineState,
    entry_from_row,
};

#[derive(Debug, Error)]
pub enum AccountBackoffRepositoryError {
    #[error("database error: {0}")]
    Database(#[from] DatabaseError),
    #[error("invalid account backoff row: {0}")]
    Invalid(String),
}

#[derive(Debug, Error)]
pub enum ModelQuarantineRepositoryError {
    #[error("database error: {0}")]
    Database(#[from] DatabaseError),
    #[error("invalid model quarantine row: {0}")]
    Invalid(String),
}

#[derive(Debug, Clone)]
struct RawBackoff {
    id: i64,
    account_id: i64,
    model_id: Option<String>,
    reason: String,
    status_code: Option<i64>,
    error_class: Option<String>,
    consecutive_failures: i64,
    backoff_until: Option<String>,
    last_failure_at: String,
    updated_at: String,
}

#[derive(Debug, Clone)]
struct RawQuarantine {
    id: i64,
    provider_id: String,
    account_id: String,
    canonical_model_id: String,
    upstream_model_id: Option<String>,
    upstream_protocol: String,
    state: String,
    evidence_provenance: String,
    reason: String,
    first_observed: String,
    last_observed: String,
    observation_count: i64,
    expiry: Option<String>,
    cleared_at: Option<String>,
    clear_reason: Option<String>,
    last_status_code: Option<i64>,
    last_error_class: Option<String>,
}

/// Validated account backoff state. Timestamps remain POSIX wall-clock
/// epochs; health hydration converts them to monotonic remaining durations.
#[derive(Debug, Clone, PartialEq)]
pub struct AccountBackoffRecord {
    pub id: i64,
    pub account_id: i64,
    pub model_id: Option<String>,
    pub reason: BackoffReason,
    pub status_code: Option<u16>,
    pub error_class: Option<String>,
    pub consecutive_failures: u32,
    pub backoff_until_epoch: Option<f64>,
    pub last_failure_epoch: f64,
    pub updated_epoch: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ModelQuarantineRecord {
    pub id: i64,
    pub provider_id: String,
    pub account_id: String,
    pub canonical_model_id: String,
    pub upstream_model_id: Option<String>,
    pub upstream_protocol: String,
    pub state: String,
    pub evidence_provenance: String,
    pub reason: String,
    pub first_observed_epoch: f64,
    pub last_observed_epoch: f64,
    pub observation_count: u32,
    pub expiry_epoch: Option<f64>,
    pub cleared_at_epoch: Option<f64>,
    pub clear_reason: Option<String>,
    pub last_status_code: Option<u16>,
    pub last_error_class: Option<String>,
}

#[derive(Debug, Clone)]
pub struct AccountBackoffRepository {
    database: Database,
}

impl AccountBackoffRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn list_all(
        &self,
        limit: u32,
    ) -> Result<Vec<AccountBackoffRecord>, AccountBackoffRepositoryError> {
        let limit = i64::from(limit.min(5_000));
        let rows = self.database.call(move |connection| {
            let mut statement = connection.prepare("SELECT id,account_id,model_id,reason,status_code,error_class,consecutive_failures,backoff_until,last_failure_at,updated_at FROM account_backoffs ORDER BY account_id,model_id,reason LIMIT ?1")?;
            statement.query_map([limit + 1], |row| Ok(RawBackoff {
                id: row.get(0)?, account_id: row.get(1)?, model_id: row.get(2)?, reason: row.get(3)?,
                status_code: row.get(4)?, error_class: row.get(5)?, consecutive_failures: row.get(6)?,
                backoff_until: row.get(7)?, last_failure_at: row.get(8)?, updated_at: row.get(9)?,
            }))?.collect::<Result<Vec<_>, _>>()
        }).await?;
        if rows.len() > usize::try_from(limit).unwrap_or(5_000) {
            return Err(AccountBackoffRepositoryError::Invalid(
                "hydration limit exceeded".to_owned(),
            ));
        }
        rows.into_iter().map(parse_backoff).collect()
    }

    pub async fn hydrate_into(
        &self,
        manager: &super::HealthManager,
        account_names: &BTreeMap<i64, String>,
        wall_now: f64,
    ) -> Result<usize, AccountBackoffRepositoryError> {
        let records = self.list_all(5_000).await?;
        manager
            .hydrate_backoffs(&records, account_names, wall_now)
            .map_err(|error| AccountBackoffRepositoryError::Invalid(error.to_string()))
    }

    pub async fn list_active(
        &self,
        now: f64,
        limit: u32,
    ) -> Result<Vec<AccountBackoffRecord>, AccountBackoffRepositoryError> {
        Ok(self
            .list_all(limit)
            .await?
            .into_iter()
            .filter(|record| {
                record
                    .backoff_until_epoch
                    .is_none_or(|deadline| deadline > now)
            })
            .collect())
    }

    pub async fn upsert(
        &self,
        record: &AccountBackoffRecord,
    ) -> Result<(), AccountBackoffRepositoryError> {
        validate_backoff_record(record)?;
        let record = record.clone();
        let now = wall_now();
        let deadline = record
            .backoff_until_epoch
            .map(|value| value.min(now + 1_800.0));
        let reason = record.reason.as_str().to_owned();
        self.database.with_transaction(move |connection| {
            let existing: Option<i64> = connection.query_row("SELECT id FROM account_backoffs WHERE account_id=?1 AND model_id IS ?2 AND reason=?3", params![record.account_id, record.model_id, reason], |row| row.get(0)).optional()?;
            let now_text = epoch_to_timestamp(now);
            let deadline = deadline.map(epoch_to_timestamp);
            if let Some(id) = existing {
                connection.execute("UPDATE account_backoffs SET status_code=?1,error_class=?2,consecutive_failures=?3,backoff_until=?4,last_failure_at=?5,updated_at=?5 WHERE id=?6", params![record.status_code.map(i64::from), record.error_class, i64::from(record.consecutive_failures), deadline, now_text, id])?;
            } else {
                connection.execute("INSERT INTO account_backoffs (account_id,model_id,reason,status_code,error_class,consecutive_failures,backoff_until,last_failure_at,updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?8)", params![record.account_id, record.model_id, reason, record.status_code.map(i64::from), record.error_class, i64::from(record.consecutive_failures), deadline, now_text])?;
            }
            Ok(())
        }).await.map_err(Into::into)
    }

    pub async fn clear_success(
        &self,
        account_id: i64,
        model_id: Option<&str>,
        reasons: &[BackoffReason],
    ) -> Result<usize, AccountBackoffRepositoryError> {
        let model_id = model_id.map(str::to_owned);
        let reason_names: Vec<String> = reasons
            .iter()
            .map(|reason| reason.as_str().to_owned())
            .collect();
        self.database
            .with_transaction(move |connection| {
                let mut sql = String::from("DELETE FROM account_backoffs WHERE account_id = ?1");
                let mut values: Vec<Box<dyn tokio_rusqlite::rusqlite::ToSql>> =
                    vec![Box::new(account_id)];
                let reason_start = if let Some(model_id) = model_id {
                    sql.push_str(" AND (model_id IS NULL OR model_id = ?2)");
                    values.push(Box::new(model_id));
                    3
                } else {
                    sql.push_str(" AND model_id IS NULL");
                    2
                };
                if !reason_names.is_empty() {
                    sql.push_str(" AND reason IN (");
                    sql.push_str(
                        &(0..reason_names.len())
                            .map(|index| format!("?{}", index + reason_start))
                            .collect::<Vec<_>>()
                            .join(","),
                    );
                    sql.push(')');
                    for reason in reason_names {
                        values.push(Box::new(reason));
                    }
                }
                let refs: Vec<&dyn tokio_rusqlite::rusqlite::ToSql> =
                    values.iter().map(|value| value.as_ref()).collect();
                connection.execute(&sql, refs.as_slice())
            })
            .await
            .map_err(Into::into)
    }

    pub async fn clear_authentication(
        &self,
        account_id: i64,
    ) -> Result<usize, AccountBackoffRepositoryError> {
        self.clear_success(account_id, None, &[BackoffReason::AuthenticationFailed])
            .await
    }

    pub async fn expire_old(&self, now: f64) -> Result<usize, AccountBackoffRepositoryError> {
        self.database.with_transaction(move |connection| connection.execute("DELETE FROM account_backoffs WHERE backoff_until IS NOT NULL AND backoff_until <= ?1", [epoch_to_timestamp(now)])).await.map_err(Into::into)
    }
}

#[derive(Debug, Clone)]
pub struct ModelQuarantineRepository {
    database: Database,
}

impl ModelQuarantineRepository {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub async fn list_all(
        &self,
        limit: u32,
    ) -> Result<Vec<ModelQuarantineRecord>, ModelQuarantineRepositoryError> {
        let limit = i64::from(limit.min(5_000));
        let rows = self.database.call(move |connection| {
            let mut statement = connection.prepare("SELECT id,provider_id,account_id,canonical_model_id,upstream_model_id,upstream_protocol,state,evidence_provenance,reason,first_observed,last_observed,observation_count,expiry,cleared_at,clear_reason,last_status_code,last_error_class FROM model_quarantine ORDER BY last_observed DESC LIMIT ?1")?;
            statement.query_map([limit + 1], raw_quarantine_from_row)?.collect::<Result<Vec<_>, _>>()
        }).await?;
        if rows.len() > usize::try_from(limit).unwrap_or(5_000) {
            return Err(ModelQuarantineRepositoryError::Invalid(
                "hydration limit exceeded".to_owned(),
            ));
        }
        rows.into_iter().map(parse_quarantine).collect()
    }

    pub async fn hydrate_into(
        &self,
        quarantine: &ModelQuarantine,
        wall_now: f64,
    ) -> Result<usize, ModelQuarantineRepositoryError> {
        let records = self.list_all(5_000).await?;
        let mut count = 0;
        for record in records {
            let entry = entry_from_row(&record).map_err(ModelQuarantineRepositoryError::Invalid)?;
            quarantine.hydrate_entry(entry, wall_now);
            count += 1;
        }
        Ok(count)
    }

    pub async fn list_active(
        &self,
        now: f64,
        limit: u32,
    ) -> Result<Vec<ModelQuarantineRecord>, ModelQuarantineRepositoryError> {
        Ok(self
            .list_all(limit)
            .await?
            .into_iter()
            .filter(|record| {
                record.state != "healthy"
                    && (record.state == "terminal_withdrawn"
                        || record.expiry_epoch.is_none_or(|expiry| expiry > now))
            })
            .collect())
    }

    pub async fn upsert_entry(
        &self,
        entry: &QuarantineEntry,
    ) -> Result<(), ModelQuarantineRepositoryError> {
        validate_entry(entry)?;
        let entry = entry.clone();
        self.database.with_transaction(move |connection| {
            let key = &entry.key;
            let existing: Option<i64> = connection.query_row("SELECT id FROM model_quarantine WHERE provider_id=?1 AND account_id=?2 AND canonical_model_id=?3 AND upstream_model_id IS ?4 AND upstream_protocol=?5", params![key.provider_id, key.account_id, key.canonical_model_id, key.upstream_model_id, key.upstream_protocol], |row| row.get(0)).optional()?;
            if let Some(id) = existing {
                connection.execute("UPDATE model_quarantine SET state=?1,evidence_provenance=?2,reason=?3,last_observed=?4,observation_count=?5,expiry=?6,cleared_at=?7,clear_reason=?8,last_status_code=?9,last_error_class=?10,updated_at=CURRENT_TIMESTAMP WHERE id=?11", params![entry.state.as_str(), entry.evidence_provenance.as_str(), entry.reason, epoch_to_timestamp(entry.last_observed), i64::from(entry.observation_count), entry.expiry.map(epoch_to_timestamp), entry.cleared_at.map(epoch_to_timestamp), entry.clear_reason, entry.last_status_code.map(i64::from), entry.last_error_class, id])?;
            } else {
                connection.execute("INSERT INTO model_quarantine (provider_id,account_id,canonical_model_id,upstream_model_id,upstream_protocol,state,evidence_provenance,reason,first_observed,last_observed,observation_count,expiry,cleared_at,clear_reason,last_status_code,last_error_class) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16)", params![key.provider_id, key.account_id, key.canonical_model_id, key.upstream_model_id, key.upstream_protocol, entry.state.as_str(), entry.evidence_provenance.as_str(), entry.reason, epoch_to_timestamp(entry.first_observed), epoch_to_timestamp(entry.last_observed), i64::from(entry.observation_count), entry.expiry.map(epoch_to_timestamp), entry.cleared_at.map(epoch_to_timestamp), entry.clear_reason, entry.last_status_code.map(i64::from), entry.last_error_class])?;
            }
            Ok(())
        }).await.map_err(Into::into)
    }

    pub async fn mark_cleared(
        &self,
        key: &super::QuarantineKey,
        reason: &str,
        now: f64,
    ) -> Result<usize, ModelQuarantineRepositoryError> {
        let key = key.clone();
        let reason = reason.to_owned();
        self.database.with_transaction(move |connection| connection.execute("UPDATE model_quarantine SET state='healthy',expiry=NULL,cleared_at=?1,clear_reason=?2,updated_at=CURRENT_TIMESTAMP WHERE provider_id=?3 AND account_id=?4 AND canonical_model_id=?5 AND upstream_model_id IS ?6 AND upstream_protocol=?7", params![epoch_to_timestamp(now), reason, key.provider_id, key.account_id, key.canonical_model_id, key.upstream_model_id, key.upstream_protocol])).await.map_err(Into::into)
    }

    pub async fn expire_old(&self, now: f64) -> Result<usize, ModelQuarantineRepositoryError> {
        self.database.with_transaction(move |connection| connection.execute("DELETE FROM model_quarantine WHERE expiry IS NOT NULL AND expiry <= ?1 AND state IN ('suspected','quarantined')", [epoch_to_timestamp(now)])).await.map_err(Into::into)
    }
}

fn parse_backoff(raw: RawBackoff) -> Result<AccountBackoffRecord, AccountBackoffRepositoryError> {
    if raw.id <= 0 || raw.account_id <= 0 || raw.model_id.as_ref().is_some_and(String::is_empty) {
        return Err(AccountBackoffRepositoryError::Invalid(
            "identity".to_owned(),
        ));
    }
    let reason = BackoffReason::try_from(raw.reason.as_str())
        .map_err(|_| AccountBackoffRepositoryError::Invalid("reason".to_owned()))?;
    if reason == BackoffReason::ModelUnavailable && raw.model_id.is_none() {
        return Err(AccountBackoffRepositoryError::Invalid(
            "model-scoped reason has no model".to_owned(),
        ));
    }
    let status_code = match raw.status_code {
        Some(value) if (100..=599).contains(&value) => Some(value as u16),
        Some(_) => {
            return Err(AccountBackoffRepositoryError::Invalid(
                "status_code".to_owned(),
            ));
        }
        None => None,
    };
    let consecutive_failures = u32::try_from(raw.consecutive_failures)
        .map_err(|_| AccountBackoffRepositoryError::Invalid("consecutive_failures".to_owned()))?;
    let last_failure_epoch = required_timestamp(&raw.last_failure_at, "last_failure_at")
        .map_err(AccountBackoffRepositoryError::Invalid)?;
    let updated_epoch = required_timestamp(&raw.updated_at, "updated_at")
        .map_err(AccountBackoffRepositoryError::Invalid)?;
    let backoff_until_epoch = optional_timestamp(raw.backoff_until.as_deref())
        .map_err(AccountBackoffRepositoryError::Invalid)?;
    if reason != BackoffReason::AuthenticationFailed && backoff_until_epoch.is_none() {
        return Err(AccountBackoffRepositoryError::Invalid(
            "nonterminal expiry".to_owned(),
        ));
    }
    Ok(AccountBackoffRecord {
        id: raw.id,
        account_id: raw.account_id,
        model_id: raw.model_id,
        reason,
        status_code,
        error_class: raw.error_class,
        consecutive_failures,
        backoff_until_epoch,
        last_failure_epoch,
        updated_epoch,
    })
}

fn raw_quarantine_from_row(
    row: &tokio_rusqlite::rusqlite::Row<'_>,
) -> tokio_rusqlite::rusqlite::Result<RawQuarantine> {
    Ok(RawQuarantine {
        id: row.get(0)?,
        provider_id: row.get(1)?,
        account_id: row.get(2)?,
        canonical_model_id: row.get(3)?,
        upstream_model_id: row.get(4)?,
        upstream_protocol: row.get(5)?,
        state: row.get(6)?,
        evidence_provenance: row.get(7)?,
        reason: row.get(8)?,
        first_observed: row.get(9)?,
        last_observed: row.get(10)?,
        observation_count: row.get(11)?,
        expiry: row.get(12)?,
        cleared_at: row.get(13)?,
        clear_reason: row.get(14)?,
        last_status_code: row.get(15)?,
        last_error_class: row.get(16)?,
    })
}

fn parse_quarantine(
    raw: RawQuarantine,
) -> Result<super::repository::ModelQuarantineRecord, ModelQuarantineRepositoryError> {
    if raw.id <= 0
        || raw.provider_id.is_empty()
        || raw.account_id.is_empty()
        || raw.canonical_model_id.is_empty()
        || raw.upstream_protocol.is_empty()
        || raw.observation_count <= 0
        || raw.upstream_model_id.as_ref().is_some_and(String::is_empty)
    {
        return Err(ModelQuarantineRepositoryError::Invalid(
            "identity/count".to_owned(),
        ));
    }
    if QuarantineState::try_from(raw.state.as_str()).is_err()
        || EvidenceProvenance::try_from(raw.evidence_provenance.as_str()).is_err()
    {
        return Err(ModelQuarantineRepositoryError::Invalid(
            "state/provenance".to_owned(),
        ));
    }
    let first_observed_epoch = required_timestamp(&raw.first_observed, "first_observed")
        .map_err(ModelQuarantineRepositoryError::Invalid)?;
    let last_observed_epoch = required_timestamp(&raw.last_observed, "last_observed")
        .map_err(ModelQuarantineRepositoryError::Invalid)?;
    let expiry_epoch = optional_timestamp(raw.expiry.as_deref())
        .map_err(ModelQuarantineRepositoryError::Invalid)?;
    let cleared_at_epoch = optional_timestamp(raw.cleared_at.as_deref())
        .map_err(ModelQuarantineRepositoryError::Invalid)?;
    let last_status_code = match raw.last_status_code {
        Some(value) if (100..=599).contains(&value) => Some(value as u16),
        Some(_) => {
            return Err(ModelQuarantineRepositoryError::Invalid(
                "last_status_code".to_owned(),
            ));
        }
        None => None,
    };
    Ok(ModelQuarantineRecord {
        id: raw.id,
        provider_id: raw.provider_id,
        account_id: raw.account_id,
        canonical_model_id: raw.canonical_model_id,
        upstream_model_id: raw.upstream_model_id,
        upstream_protocol: raw.upstream_protocol,
        state: raw.state,
        evidence_provenance: raw.evidence_provenance,
        reason: raw.reason,
        first_observed_epoch,
        last_observed_epoch,
        observation_count: raw
            .observation_count
            .try_into()
            .map_err(|_| ModelQuarantineRepositoryError::Invalid("observation_count".to_owned()))?,
        expiry_epoch,
        cleared_at_epoch,
        clear_reason: raw.clear_reason,
        last_status_code,
        last_error_class: raw.last_error_class,
    })
}

fn validate_backoff_record(
    record: &AccountBackoffRecord,
) -> Result<(), AccountBackoffRepositoryError> {
    if record.account_id <= 0
        || record.model_id.as_ref().is_some_and(String::is_empty)
        || record.consecutive_failures > 1_000_000
        || super::get_backoff_policy(record.reason).is_none()
        || (record.reason == BackoffReason::ModelUnavailable && record.model_id.is_none())
        || (record.reason == BackoffReason::AuthenticationFailed
            && record.backoff_until_epoch.is_some())
        || record
            .backoff_until_epoch
            .is_some_and(|value| !value.is_finite())
    {
        return Err(AccountBackoffRepositoryError::Invalid(
            "identity/reason/expiry".to_owned(),
        ));
    }
    Ok(())
}

fn validate_entry(entry: &QuarantineEntry) -> Result<(), ModelQuarantineRepositoryError> {
    if entry.key.provider_id.is_empty()
        || entry.key.account_id.is_empty()
        || entry.key.canonical_model_id.is_empty()
        || entry.key.upstream_protocol.is_empty()
        || entry.observation_count == 0
        || !entry.first_observed.is_finite()
        || !entry.last_observed.is_finite()
        || entry.expiry.is_some_and(|value| !value.is_finite())
        || entry.cleared_at.is_some_and(|value| !value.is_finite())
    {
        return Err(ModelQuarantineRepositoryError::Invalid(
            "identity/count/timestamp".to_owned(),
        ));
    }
    Ok(())
}

fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}
fn required_timestamp(value: &str, name: &str) -> Result<f64, String> {
    parse_timestamp(value)?.ok_or_else(|| format!("invalid {name}"))
}
fn optional_timestamp(value: Option<&str>) -> Result<Option<f64>, String> {
    value.map(parse_timestamp).transpose().and_then(|value| {
        value.flatten().map_or(Ok(None), |number| {
            if number.is_finite() {
                Ok(Some(number))
            } else {
                Err("non-finite timestamp".to_owned())
            }
        })
    })
}
fn parse_timestamp(value: &str) -> Result<Option<f64>, String> {
    if let Ok(number) = value.parse::<f64>() {
        return Ok(Some(number));
    }
    let (date, time) = value
        .split_once(' ')
        .ok_or_else(|| "timestamp format".to_owned())?;
    let mut date_parts = date.split('-');
    let year: i64 = date_parts
        .next()
        .and_then(|v| v.parse().ok())
        .ok_or_else(|| "timestamp year".to_owned())?;
    let month: i64 = date_parts
        .next()
        .and_then(|v| v.parse().ok())
        .ok_or_else(|| "timestamp month".to_owned())?;
    let day: i64 = date_parts
        .next()
        .and_then(|v| v.parse().ok())
        .ok_or_else(|| "timestamp day".to_owned())?;
    let (clock, fraction) = time.split_once('.').unwrap_or((time, "0"));
    let mut parts = clock.split(':');
    let hour: i64 = parts
        .next()
        .and_then(|v| v.parse().ok())
        .ok_or_else(|| "timestamp hour".to_owned())?;
    let minute: i64 = parts
        .next()
        .and_then(|v| v.parse().ok())
        .ok_or_else(|| "timestamp minute".to_owned())?;
    let second: i64 = parts
        .next()
        .and_then(|v| v.parse().ok())
        .ok_or_else(|| "timestamp second".to_owned())?;
    let micros: f64 = format!("0.{fraction}")
        .parse()
        .map_err(|_| "timestamp fraction".to_owned())?;
    Ok(Some(
        days_from_civil(year, month, day) as f64 * 86_400.0
            + (hour * 3_600 + minute * 60 + second) as f64
            + micros,
    ))
}
fn epoch_to_timestamp(epoch: f64) -> String {
    let seconds = epoch.floor() as i64;
    let micros = ((epoch - seconds as f64) * 1_000_000.0).round() as i64;
    let (year, month, day) = civil_from_days(seconds.div_euclid(86_400));
    let day_seconds = seconds.rem_euclid(86_400);
    let hour = day_seconds / 3_600;
    let minute = day_seconds % 3_600 / 60;
    let second = day_seconds % 60;
    if micros == 0 {
        format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}")
    } else {
        format!("{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}.{micros:06}")
    }
}
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = year - i64::from(month <= 2);
    let era = (if year >= 0 { year } else { year - 399 }).div_euclid(400);
    let year_of_era = year - era * 400;
    let month_prime = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * month_prime + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let days = days + 719_468;
    let era = (if days >= 0 { days } else { days - 146_096 }).div_euclid(146_097);
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    (year + i64::from(month <= 2), month, day)
}
