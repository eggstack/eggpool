//! Transactional EggPool desktop configurator.
//!
//! `eggpool-connect` receives a secret-free `epc1` connection profile,
//! fetches the current authenticated integration projection, plans the
//! minimal client mutation, commits a byte-exact backup, replaces config
//! atomically, validates locally and client-natively, and rolls back
//! automatically on failure.
//!
//! The helper is not an agent harness and is not a second EggPool proxy. It
//! owns only receiving a connection profile and safely configuring a
//! supported local client. All renderers come from `eggpool-client-config`;
//! `Config`/catalog/database/key/endpoint server concerns stay in the
//! EggPool application.

#![forbid(unsafe_code)]

pub mod atomic;
pub mod backup;
pub mod cli;
pub mod credential;
pub mod detect;
pub mod fetch;
pub mod install;
pub mod outcome;
pub mod paths;
pub mod process;
pub mod transaction;
pub mod verify;

pub use backup::{BACKUP_RETENTION_PER_TARGET, BACKUP_SCHEMA_VERSION, BackupManifest};
pub use cli::{Cli, Commands};
pub use detect::Detection;
pub use install::{FailureInjector, InstallOutcome, PlannedMutation};
pub use outcome::{ConnectError, ExitCode};
pub use transaction::{Phase, Transaction};
