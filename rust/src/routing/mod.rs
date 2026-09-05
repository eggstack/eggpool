//! M5 deterministic routing, fairness, and local selection-claim boundary.

mod claim;
mod eligibility;
mod fairness;
mod router;

pub use claim::{ClaimError, ClaimTransition, SelectionClaim, SelectionSnapshot};
pub use eligibility::{
    EligibilityPolicy, FairnessMode, FairnessScope, LocalQuotaMode, RoutingCandidate,
    RoutingExclusion, RoutingPlan, RoutingRequestFacts, ThinkingRequirement,
};
pub use fairness::{
    DeterministicFairnessRandom, FAIRNESS_KEY_HARD_CAP, FairnessDecision, FairnessKey,
    FairnessRandom, FairnessRotor,
};
pub use router::{RoutingDecisionTrace, RoutingRouter};
