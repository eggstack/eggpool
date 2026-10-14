# On-Prem User Access M008 — Enforceable token and cost admission

Status: ready

Repository baseline: `c18dcc2051ae4987a2cb95a7cd046b8aff2095ff` on `plans/on-prem-small-team-user-access` (PR #7), schema 60, M001–M007 closure records as of 2026-10-13.

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m008`

Long-term references:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: capability

## 1. Objective

Enforceable token and cost admission. This is a **new corrective** to the conditionally closed M005/M006/M007 records, not a rewrite of historical closure.

## 2. Readiness and dependencies

No hard predecessor beyond the accepted M004 model-policy contract and the M005
durable reservation/settlement interface. Those are present; this plan
is ready. The protocol-level capability and pricing contract must be
settled **before** an irreversible change to admitted wire behavior.

## 3. Evidence and why prior qualification missed it

`plans/closure/on-prem-user-access/005-status.md` reports:
(1) output tokens are reserved from a request-size estimate, not a proven
protocol/model maximum; (2) unpriced models may consume a generic fallback
USD estimate, which cannot establish a hard cap. Existing concurrent
last-slot tests prove atomic **estimated** reservations, not an upper bound
on actual provider-reported or billable tokens/cost. The M006 HTML currently
labels these as estimates. In `rust/src/user_budget.rs`,
`check_initial` accepts `tokens`/`microdollars` estimates;
`rules_exceeded` compares them with durable used and active holds;
`record_attempt_and_maybe_settle` records observed/fallback usage.
The `rust/src/coordinator/endpoints.rs`, `finite.rs`,
`streaming/`, `wire/` and `rust/src/config.rs` (pricing fallback)
are relevant ownership boundaries.

## 4. Non-regressing invariants

- Canonical use is **one administrator on one local/SBC instance**, optional <50 named users. No SaaS, organization tenancy, billing platform, centralized IAM, new database writer/service, built-in WAN/VPN/reverse proxy, or mandatory cloud calls.
- Upstream provider topology, API keys, routing and self-hosted model endpoints remain in TOML/environment; SQLite owns users, personal credentials, profiles, policy, downstream budgets, accounting and audit. Do not place upstream keys in user tables or web administration.
- Maintain generation leases, existing coordinator publication/finalization, a **single SQLite connection/gate/worker with WAL/NORMAL**, canonical wire and transparent streaming, bounded hot-path memory and provider fairness. Do not turn downstream user budget refusal into upstream provider unhealthiness.
- Preserve published OpenAI Chat/Responses/compact and Anthropic Messages behavior, permitted client auth headers, existing client profiles, dashboard themes and local legacy upgrade behavior.
- Keep identity and cost metadata redacted, prohibit secret/password/bearer leakage in logs, HTML snapshots or qualification artifacts, and fail closed rather than silently weaken configured security limits.

## 5. Scope and required production changes

### A — Define enforceable versus measured limits

Introduce explicit rule enforcement semantics:
- `hard`: never admit without a demonstrably conservative upper bound for
  **every chargeable execution** (input, output, internal selector work,
  bounded retries/failovers) and all simultaneously enforced windows.
- `estimated`: permits explicitly approximate token or USD budgets;
  must never be advertised as hard, must be auditable at the API/CLI/UI.

Request-count and concurrency limits can remain strictly enforceable;
do not broaden their semantics. Distinguish provider-reported tokens,
local token estimates, provider-reported price, locally configured tariff,
reservation fallback and unknowns in durable usage and operator projections.
Do not infer exact monetary spend from consumer subscription flat-rate quotas.
Decide policy for retry costs and selector calls explicitly: either reserve
their worst-case bounded additional spend up front and extend transactionally
before each attempt, or deny further submission when capacity cannot be
reserved. Do not suppress internal usage.

### B — Protocol/model admissible maxima

Implement a typed, per-surface bounded output-capability contract for Chat
(`max_tokens`/supported variants), Responses (including native compact and
`max_output_tokens`), and Anthropic Messages (`max_tokens`). Never
silently drop/mutate an explicit caller field in a native passthrough mode.
For a configured hard cap, either:
1. prove a safe worst-case bound on billable input+output across the admitted
   context, supported models and all permitted attempts; constrain output
   explicitly using the caller's compatible bound; or
2. reject the **request** with a stable, sanitized and machine-readable
   "not enforceable" response **before provider dispatch**.

Provider tokenizer discrepancies, prompt/tool overhead, native unknown fields
and post-hoc usage all defeat exact counting unless a formally conservative
bound exists. Do not present a field-level `max_output_tokens` alone as a
guarantee of total chargeable tokens. Preserve user-requested output bounds
and cross-surface adaptation notices.

### C — Pricing trust boundary

Hard USD policies require a reviewed operator-specified conservative
tariff/upper-bound price **in provider TOML** (including self-hosted LLMs),
or a verified provider-specific bound that cannot increase unnoticed during
the request. Do not accept `pricing.fallback=generic_estimate` as a hard
price. For no price or unsupported/dynamic billing, fail closed for hard
USD requests; estimated USD policies may continue with visible uncertainty.
Handle integer overflow, zero-cost versus unknown-price, cache changes, and
unit precision. No online price discovery requirement.

### D — Transaction integration and UI truthful projection

Wire admission type/estimate/worst-case permit into the existing
`coordinator/publication.rs` per-logical-request transaction,
`coordinator/semantic.rs` selector path, and retry pre-submission
`user_budget::check_retry`. Prevent budget-check-then-dispatch gaps or
tokens consumed after a failed extension. Maintain finalizer idempotence;
do not hide observed overruns if a provider violates limits.
Expose `hard`/`estimated` and `not_enforceable` consistently in
`/api/user-budget`, `rust/src/server/dashboard/admin.rs`,
CLI `users budget`, and docs without renaming old keys gratuitously.
Provide a compatibility migration/default: existing token/USD rules
remain explicitly `estimated` until the operator chooses hard mode;
**never** silently upgrade them to a falsely enforced guarantee.

## 6. Explicit non-goals

No built-in provider billing reconciliation, guaranteed spend prediction on
unpriced/dynamic models, new provider metadata service, routing based on
budget/cost, additional SQLite writers, or unrelated upstream quota changes.
If the supported wire cannot prove a bound, make hard mode unavailable rather
than modifying the provider's responses.

## 7. Ordered work packages

1. Document authoritative limit units/scope, price provenance, compatibility
   defaults and the immutable admission maximum/unknown state contract.
2. Add append-only migration only if new durable enforcement mode or provenance
   is required (version >60, manifest/checksums), plus parser/API roundtrips.
3. Implement protocol/model bound validation and retry/selector reservation
   with proof-or-reject behavior before network I/O.
4. Preserve settlement/analytics and extend accurate hard-vs-estimated UI
   presentation, docs and negative tests.

## 8. Failure, cancellation, restart and contention

No database transaction crosses provider I/O or expensive tokenization.
A hard-mode rule with missing tokenizer upper bound, unknown tariff or
non-conservative retry/selector estimate must deny admission without starting
an upstream attempt. A rule edit is atomic; in-flight requests retain the
authenticated immutable policy version and their durable held amount, and
new requests see the new enforcement policy. On transport EOF, cancellation,
ambiguous usage, or duplicate finalization, record lower-bound/unknown
usage honestly and preserve holds until an authorized bounded recovery path
can safely settle. DB failures fail closed.

## 9. Compatibility and migration

Existing M005 user-scoped stored rules, provider `accounts`, historic
consumption and source API schemas remain readable. Additive schema only;
old rows explicitly map to an estimated mode and can be upgraded by an admin
with an explicit operation. Preserve unchanged native Chat/Responses/Messages
bytes where possible and never inject output caps into an unsupported surface.
Self-hosted LLM pricing belongs only to operator TOML, not user DB.

## 10. Focused and adversarial tests

Extend `rust/tests/on_prem_budgets.rs`,
`coordinator_publication.rs`, `coordinator_finalization.rs`,
`coordinator_c011.rs`, `on_prem_model_policy.rs`,
`codex_compaction_compat.rs`, `wire_runtime.rs`,
`database_compatibility.rs`, `on_prem_admin_ui.rs`.
Cover Chat/Responses/compact/Messages, allowed/denied native fields,
bounded/unbounded output, omitted tokenizer, provider-reported use above
estimate, retry to different model/provider, selector child spend,
unpriced/private LLM, dynamic price, integer overflow, no-cost token-only
policy, concurrent last-slot, and explicit estimated-mode compatibility.
Assert **zero upstream submissions** on unprovable hard-bound cases.
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
```
## 11. Required verification commands

Run applicable focused targets in §10, then the full default/no-default gates:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
Run `cargo deny --manifest-path rust/Cargo.toml check`, the feature tree,
and `cargo build --manifest-path rust/Cargo.toml --locked --release` when
Cargo dependencies/features change. Use the repository's tooling/test commands
when scripts are changed. Closure records must distinguish local vs hosted
vs physical hardware results; never claim unrun tests as passed.
## 12. Documentation updates

Update `architecture/deep-dive-request-lifecycle.md`,
`architecture/deep-dive-database.md`, `docs/model-limits.md`,
`docs/api-reference.md`, `docs/cli-reference.md` and
`docs/deployment.md` with hard/estimated semantics and hard-mode
proof-or-reject. Keep examples with owned local models.

## 13. Acceptance criteria

Every admitted request under a **hard** rule carries a documented
conservative upper bound covering every possible accounted attempt and
selector, or is denied **without upstream I/O**. Under unknown-price or
unbounded-output cases no false hard USD/token promise can be configured.
Existing estimated rules continue to behave accurately as estimates;
previous exact request/concurrency limits still hold. UI, API and
persistence agree on enforcement mode; all supported wire formats
retain non-budget behavior.

## 14. Stop conditions

Stop if a strict upper bound is unprovable for a required model/wire:
record the unsupported strict mode rather than claiming it. Stop on
cross-attempt double spend, a publisher/finalizer ownership gap,
accidental provider TOML-to-user DB migration, any native protocol rewrite
with lost user intent, or a different SQLite writer design.

## 15. Closure evidence required

New `plans/closure/on-prem-user-access/008-status.md` must include:
formal accounting/maxima assumptions and explicit non-support cases;
admission/selector/retry and unknown-price negative matrix; migration
compatibility; source and test revisions; exact commands/CI outcomes;
security/reservation/finalization review and severity-tagged residuals.
Do **not** retrospectively edit `005-status.md`.

## 16. Handoff

One coherent budget-admission/wire interface pass; `server/*` stays
thin and coordinator remains the single ingress execution owner. Physical
latency/sizing and global hard ceilings are later separate milestones.
