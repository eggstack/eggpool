//! Small process-local services used by operational commands.
//!
//! These modules deliberately contain no CLI presentation and no second
//! runtime/reload authority. `control` adapts local frames to the runtime's
//! `ReloadService`; `paths` and `process` provide reusable, secret-free
//! lifecycle observations for operational commands.

pub mod backup;
pub mod catalog;
pub mod config_mutation;
pub mod control;
pub mod deploy;
pub mod integrations;
pub mod lifecycle;
pub mod metrics;
pub mod operator;
pub mod paths;
pub mod process;
pub mod provenance;
pub mod status;
pub mod update;
