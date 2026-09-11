//! SQLite compatibility boundary for the Rust migration candidate.
//!
//! The Rust-owned asset tree contains the exact historical migrations and
//! Python-era SHA-256 manifest so Rust cannot silently grow a second schema
//! source.

mod connection;
pub mod migrations;
pub mod repositories;

pub use connection::{
    Database, DatabaseConfig, DatabaseError, DatabaseStats, DatabaseTransaction,
    RetentionCleanupPolicy, RetentionCleanupReport,
};
pub use migrations::{Migration, MigrationRunner, MigrationState};
pub use repositories::{
    Account, AccountConfig, AccountModelSupport, AccountRepository, CatalogModel,
    CatalogModelWrite, CatalogPersistenceBatch, CatalogPingWrite, CatalogRefreshState,
    CatalogRefreshWrite, CatalogRepository, DashboardAccountRow, DashboardCacheSummary,
    DashboardData, DashboardEventRow, DashboardModelRow, DashboardRepository, DashboardRequestRow,
    DashboardRetryRow, DashboardRoutingRow, DashboardSummary, DashboardTimeseriesRow, Model,
    ModelRepository, Ping, PingRepository, ProviderModelMetadata, ProviderModelWrite, Request,
    RequestRepository, UsageRollupRepository, UsageSummary, UsageWindowRepository,
    UsageWindowSnapshot,
};

pub use crate::health::{
    AccountBackoffRecord, AccountBackoffRepository, AccountBackoffRepositoryError,
    ModelQuarantineRecord, ModelQuarantineRepository, ModelQuarantineRepositoryError,
};
