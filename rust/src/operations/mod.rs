//! Small process-local services used by the M9 operational commands.
//!
//! These modules deliberately contain no CLI presentation and no second
//! runtime/reload authority.  `control` adapts local frames to M8's
//! `ReloadService`; `paths` and `process` provide reusable, secret-free
//! lifecycle observations for later commands.

pub mod backup;
pub mod config_mutation;
pub mod control;
pub mod deploy;
pub mod integrations;
pub mod metrics;
pub mod operator;
pub mod paths;
pub mod process;
pub mod update;
