//! SQLite compatibility boundary for the Rust migration candidate.
//!
//! The canonical schema remains `src/eggpool/db/schema`.  The build script
//! embeds those exact files and their Python-era SHA-256 manifest so Rust
//! cannot silently grow a second migration source.

mod connection;
pub mod migrations;
pub mod repositories;

pub use connection::{Database, DatabaseConfig, DatabaseError, DatabaseStats};
pub use migrations::{Migration, MigrationRunner, MigrationState};
pub use repositories::{
    Account, AccountConfig, AccountModelSupport, AccountRepository, CatalogModel,
    CatalogModelWrite, CatalogPersistenceBatch, CatalogPingWrite, CatalogRefreshState,
    CatalogRefreshWrite, CatalogRepository, DashboardSummary, Model, ModelRepository, Ping,
    PingRepository, ProviderModelMetadata, ProviderModelWrite, Request, RequestRepository,
    UsageRollupRepository, UsageSummary, UsageWindowRepository, UsageWindowSnapshot,
};

pub use crate::health::{
    AccountBackoffRecord, AccountBackoffRepository, AccountBackoffRepositoryError,
    ModelQuarantineRecord, ModelQuarantineRepository, ModelQuarantineRepositoryError,
};
