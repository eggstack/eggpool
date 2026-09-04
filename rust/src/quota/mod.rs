//! M5 quota state and fair-share scoring.

mod estimator;
mod scorer;
mod state;

pub use estimator::{
    EWMA_HARD_CAP, EwmaEstimate, GLOBAL_EWMA_HARD_CAP, QuotaAccountSnapshot, QuotaEstimator,
};
pub use scorer::{QuotaFairScorer, RoutingScore, ScoringPolicy};
pub use state::{
    AccountQuota, DEFAULT_REQUEST_CAPACITY_5H, DEFAULT_REQUEST_CAPACITY_7D,
    DEFAULT_REQUEST_CAPACITY_30D, DEFAULT_TOKEN_CAPACITY_5H, DEFAULT_TOKEN_CAPACITY_7D,
    DEFAULT_TOKEN_CAPACITY_30D, ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS,
    PersistedWindowSnapshot, QuotaInvariantError, QuotaPolicy, QuotaWindow, QuotaWindowName,
    RESERVATION_COST_CEILING_MICRODOLLARS, SQLITE_INTEGER_MAX,
};
