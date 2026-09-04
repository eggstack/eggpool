//! Canonical migration discovery, checksum validation, and application.

use super::{Database, DatabaseError};
use sha2::{Digest, Sha256};

/// One migration copied from the canonical Python schema at build time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Migration {
    pub version: u32,
    pub name: &'static str,
    pub sql: &'static str,
    pub expected_sha256: &'static str,
}

include!(concat!(env!("OUT_DIR"), "/eggpool_migrations.rs"));

/// The current migration ledger state observed in SQLite.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MigrationState {
    pub applied_versions: Vec<u32>,
    pub applied_this_run: Vec<u32>,
}

/// Applies canonical migrations without rewriting or renumbering history.
#[derive(Debug, Clone)]
pub struct MigrationRunner {
    database: Database,
}

impl MigrationRunner {
    pub fn new(database: &Database) -> Self {
        Self {
            database: database.clone(),
        }
    }

    pub fn migrations() -> &'static [Migration] {
        MIGRATIONS
    }

    pub fn validate_embedded_checksums() -> Result<(), DatabaseError> {
        Self::validate_checksums(MIGRATIONS)
    }

    pub fn validate_checksums(migrations: &[Migration]) -> Result<(), DatabaseError> {
        for migration in migrations {
            let actual = hex_digest(migration.sql.as_bytes());
            if actual != migration.expected_sha256 {
                return Err(DatabaseError::MigrationChecksumMismatch {
                    name: migration.name.to_owned(),
                    expected: migration.expected_sha256.to_owned(),
                    actual,
                });
            }
        }
        Ok(())
    }

    pub async fn run(&self) -> Result<MigrationState, DatabaseError> {
        Self::validate_embedded_checksums()?;
        self.ensure_ledger().await?;
        let applied = self.applied().await?;
        let mut pending = Vec::new();

        for migration in MIGRATIONS {
            match applied
                .iter()
                .find(|(version, _)| *version == migration.version as i64)
            {
                Some((_, name)) if ledger_name_matches(name, migration.name) => {}
                Some((version, name)) => {
                    return Err(DatabaseError::MigrationNameMismatch {
                        version: *version,
                        expected: migration.name.to_owned(),
                        actual: name.clone(),
                    });
                }
                None => pending.push(*migration),
            }
        }

        for (version, _) in &applied {
            if !MIGRATIONS
                .iter()
                .any(|migration| migration.version as i64 == *version)
            {
                return Err(DatabaseError::UnknownMigration { version: *version });
            }
        }
        if !pending.is_empty() && self.database.config().read_only {
            return Err(DatabaseError::ReadOnlyMigration);
        }

        for migration in &pending {
            let name = migration.name.to_owned();
            let sql = migration.sql.to_owned();
            let version = migration.version as i64;
            self.database
                .with_transaction(move |connection| {
                    connection.execute_batch(&sql)?;
                    connection.execute(
                        "INSERT INTO _migrations (version, name) VALUES (?1, ?2)",
                        (version, name),
                    )?;
                    Ok(())
                })
                .await?;
        }

        let applied_versions = applied
            .into_iter()
            .map(|(version, _)| version as u32)
            .chain(pending.iter().map(|migration| migration.version))
            .collect();
        Ok(MigrationState {
            applied_versions,
            applied_this_run: pending.iter().map(|migration| migration.version).collect(),
        })
    }

    async fn ensure_ledger(&self) -> Result<(), DatabaseError> {
        if self.database.config().read_only {
            return Ok(());
        }
        self.database
            .with_transaction(|connection| {
                connection.execute_batch(
                    "CREATE TABLE IF NOT EXISTS _migrations (\n\
                    version INTEGER PRIMARY KEY,\n\
                    name TEXT NOT NULL,\n\
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n\
                )",
                )?;
                Ok(())
            })
            .await
    }

    async fn applied(&self) -> Result<Vec<(i64, String)>, DatabaseError> {
        self.database
            .call(|connection| {
                let mut statement =
                    connection.prepare("SELECT version, name FROM _migrations ORDER BY version")?;
                statement
                    .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
                    .collect::<Result<Vec<_>, _>>()
            })
            .await
    }
}

fn hex_digest(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn ledger_name_matches(actual: &str, expected: &str) -> bool {
    actual == expected || actual == expected.strip_suffix(".sql").unwrap_or(expected)
}

#[cfg(test)]
mod tests {
    use super::MigrationRunner;

    #[test]
    fn canonical_inventory_is_complete_and_immutable() {
        MigrationRunner::validate_embedded_checksums().expect("canonical checksums match");
        assert_eq!(MigrationRunner::migrations().len(), 54);
        assert_eq!(MigrationRunner::migrations().first().unwrap().version, 1);
        assert_eq!(MigrationRunner::migrations().last().unwrap().version, 54);
    }

    #[test]
    fn checksum_corruption_is_refused_before_database_use() {
        let mut corrupted = MigrationRunner::migrations()[0];
        corrupted.expected_sha256 = "0";
        let error = MigrationRunner::validate_checksums(&[corrupted])
            .expect_err("changed migration content must fail closed");
        assert!(matches!(
            error,
            crate::db::DatabaseError::MigrationChecksumMismatch { .. }
        ));
    }
}
