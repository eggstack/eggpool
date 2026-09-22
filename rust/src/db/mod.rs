//! SQLite persistence boundary for the native EggPool runtime.
//!
//! The Rust-owned asset tree contains the exact migration chain and SHA-256
//! manifest so the runtime cannot silently grow a second schema source.

mod connection;
pub mod migrations;
#[cfg(feature = "qualification-db-diagnostics")]
mod qualification;
pub mod repositories;

pub use connection::{
    Database, DatabaseConfig, DatabaseError, DatabaseStats, DatabaseTransaction,
    RetentionCleanupPolicy, RetentionCleanupReport,
};
#[cfg(feature = "qualification-db-diagnostics")]
pub use qualification::{
    QualificationDbSnapshot, QualificationEffectivePragmas, QualificationTransactionRecord,
};

pub(crate) use connection::TransactionKind;
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
