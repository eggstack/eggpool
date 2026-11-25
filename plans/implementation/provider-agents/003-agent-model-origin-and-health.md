# Provider Agents Milestone 003 — Agent Model Origin, Discovery, and Health

Status: ready

Repository baseline: `c4d774aeb883307f28ef94f6143dc688b3098946`

Source roadmap:

- `plans/subsystems/provider-agents-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`

Applicable ADRs:

- `plans/adrs/ADR-0016-provider-agent-upstreams.md` (accepted)

Primary class: capability

## 1. Objective

Make agent-served models discoverable, honestly labelled by origin, and
deterministically disambiguated when two agents expose the same model name; make
an offline agent visible and route-excluded instead of a mysterious failure.

## 2. Why this milestone is ready

Ready.

- Hard: satisfied. M001 is implemented and conditionally closed through
  `plans/closure/provider-agents/001-status.md`, and M002 is implemented and
  conditionally closed through `plans/closure/provider-agents/002-status.md`.
  Both outstanding conditions are external to this milestone — a repo-wide
  dependency-audit gate owned by Security hardening M008, and the live ticket
  producer in `eggstack/eggpass#10` — and neither is an interface dependency of
  the catalog, routing, status, or dashboard work here, so neither blocks
  implementation.
- Hard: ADR-0016 acceptance is satisfied (2026-11-24).

## 3. Current implementation evidence

- `rust/src/catalog/cache.rs`: `ModelCatalogCache` keys `models` by `model_id`,
  keeps `provider_models: model_id -> provider_id -> ProviderModelIdentity`,
  `account_support: model_id -> accounts`, `account_providers`, and
  `account_provider_keys`. `ModelIdentity` carries `display_name` and
  `first_provider_id`; `ProviderModelIdentity` carries `display_name`.
- `rust/src/catalog/refresh.rs`: per-account discovery branches on
  `AccountAuthMode`; static accounts use the configured key, OAuth accounts use
  `OAuthAccountManager::access_token_for_user`. Agent accounts need a third
  branch that uses the account key and endpoint.
- `rust/src/routing/eligibility.rs`: binding visibility and account eligibility
  are decided here; an availability signal must be applied without changing the
  owner rules.
- `rust/src/server/dashboard/admin.rs` and `src/server/dashboard/render/`: the
  member model list and account surfaces.
- `rust/src/operations/status.rs` and `rust/src/server/health.rs`: bounded,
  secret-free readiness and status projections.
- Duplicate model names across accounts of one provider already merge into one
  catalog row today; the projection has no origin label.

## 4. Invariants that must not regress

- Dispatch identity remains the upstream `model_id`; origin labels and numeric
  suffixes are display-only and never reach a provider or the policy grammar.
- Origin labels are deterministic across reloads and process restarts for the
  same configuration.
- No secret, credential, ticket, or request body appears in labels, status, or
  diagnostics.
- An offline agent is excluded only for its own account; no other account is
  disabled and no other owner's account is selected.
- `model_policy` continues to allow or deny by model and provider; this
  milestone does not add an account-level policy grammar.

## 5. Scope

### In scope

- Per-account origin identity and labels in the catalog projection.
- Deterministic collision suffixes in the per-user model list.
- Agent discovery through the tunnel (`/v1/models`).
- Tunnel state as a bounded health/availability input, projected to status and
  the dashboard.
- Routing exclusion for offline agents.
- Tests and documentation.

### Explicitly out of scope

- Account-qualified request addressing (`model@agent`); only add it if evidence
  shows the model/provider grammar is insufficient.
- Changing provider model ids or wire surfaces.
- Health probing of sleeping machines.
- Removing OAuth (M004).

## 6. Required production changes

**Origin identity.** Add a projection-only origin value
`(provider_id, account_name, label)`. The label is the agent's enrolled display
name when present, otherwise the account name. Keep catalog storage keyed by
`model_id`; do not create a second model identity for the same upstream model.

**Collision suffix.** When two origins under the same model render the same
label, append ` 2`, ` 3` in a stable order: sort by `(provider_id, account_name)`
and assign suffixes in that order. The suffix is computed at projection time and
must not depend on insertion order, discovery time, or iteration order of a
`HashMap`.

**Discovery.** In `catalog/refresh.rs`, add the `Agent` branch: resolve the
account key (no OAuth manager call), use the account endpoint, fetch
`/v1/models`, and record support. A failed discovery leaves the previous
catalog entry in place and records a bounded outcome; it does not remove models
from other accounts.

**Availability.** Project `AgentTunnelManager` control state as
`Online | Starting | Offline | Failed`. Routing excludes non-online agent
accounts. Status and dashboard render the state and a bounded reason; no active
probing is added and no credential or endpoint secret is exposed.

**Model surface.** Group the member model list by model, show one row per origin
with its label and suffix, and keep the existing model/provider affordances
unchanged.

## 7. Ordered work packages

### Work package A — Origin projection

Intent: label models without changing identity.

Required changes: projection types, label resolution, stable suffix assignment,
catalog snapshot tests.

Acceptance evidence: two agents with the same model and label render
`name`, `name 2`; reordering configuration does not change the assignment;
dispatch still sends the bare model id.

### Work package B — Agent discovery

Intent: populate the catalog through the tunnel.

Required changes: `Agent` branch in refresh, endpoint use, outcome recording,
stale-retention behavior.

Acceptance evidence: a fixture agent populates models; a failed fetch keeps the
previous entry and records the failure; another account is unaffected.

### Work package C — Availability and status

Intent: make offline honest.

Required changes: tunnel state projection, routing exclusion, status/dashboard
rendering, bounded reason vocabulary.

Acceptance evidence: a stopped tunnel excludes the account; status shows a
bounded state; no secret or endpoint credential appears; another owner's accounts
are unaffected.

## 8. Failure, cancellation, restart, contention semantics

- Discovery and status reads are bounded and must not block request admission.
- A tunnel drop during a request fails that request; the account becomes offline
  until the control connection returns.
- Reload rebuilds the projection from the configuration; suffix assignment is
  recomputed deterministically.
- Concurrent status reads are lock-free projections of a snapshot.

## 9. Compatibility and migration

No storage migration. Origin labels are additive to the catalog projection and
API payloads; existing consumers that read `model_id`/`display_name` continue to
work. The label and suffix are not part of the request grammar.

## 10. Required tests

- `rust/tests/catalog_refresh.rs`: agent discovery, stale retention, isolation.
- `rust/src/catalog/cache.rs` unit tests for deterministic suffixes.
- `rust/tests/on_prem_admin_ui.rs`: member model rows and offline state.
- `rust/tests/routing_domain.rs`: offline exclusion, no cross-owner fallback.
- `rust/tests/status_command` or `operations`: bounded, secret-free status.
- Default and no-default serial suites.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

## 12. Documentation updates

- `docs/providers.md`: model identity and origin labels.
- `architecture/deep-dive-catalog.md`: projection ownership and suffix rule.
- `architecture/deep-dive-providers.md`: offline semantics.
- Dashboard help text for the account state.

## 13. Acceptance criteria

- Two agents exposing the same model produce distinct, stable labels.
- Dispatch and policy continue to use the bare model id and provider qualifier.
- An offline agent is excluded for its own account only and is visibly offline.
- Status and dashboard are bounded and secret-free.

## 14. Stop conditions

Stop and report if: origin labels would require changing the dispatch grammar;
suffix assignment cannot be made deterministic; availability cannot be expressed
without active probing; or the change would alter another user's eligibility.

## 15. Closure evidence required

- Determinism evidence for labels and suffixes across reload and reorder.
- Discovery success/failure/isolation evidence.
- Offline exclusion and status redaction evidence.
- Default and no-default serial suite results.

## 16. Handoff notes

Labels are presentation, not identity. Never persist a suffixed label as the
upstream model. Do not add account-level policy or addressing without evidence.
Rust tests run serially.
