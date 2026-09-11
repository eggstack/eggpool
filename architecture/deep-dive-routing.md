# Deep Dive: Routing and Quota

Back to [Architecture](README.md)

`rust/src/routing/` and `rust/src/quota/` select eligible provider accounts
using health, availability, active load, quota reservations, routing priority,
and bounded fairness. Routing is load-based and never cost-based.

`rust/src/model_router.rs` owns optional virtual aliases and bounded selector
affinity. A selector chooses a concrete model before ordinary provider routing;
it cannot pin an account, bypass health/quota, or reselect after submission.

Claims and reservations are released on every terminal path. Quarantine,
backoff, and capability gates are evaluated before selection and remain scoped
to the provider/model facts that produced them.
