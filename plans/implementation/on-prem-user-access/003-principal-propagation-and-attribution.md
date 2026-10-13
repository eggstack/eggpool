# On-Prem User Access Milestone 003 — Principal propagation and durable attribution

Status: blocked

Repository baseline: `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9` (architecture/roadmap authority committed on `plans/on-prem-small-team-user-access` at `81beec4e215e8991880d407ed2752d01ff88d58a`)

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m003--request-principal-and-durable-user-attribution`

Long-term requirements:
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: infrastructure

## 1. Objective and boundary

Bind downstream user ID to every logical inference execution, durable request
and terminal settlement without changing upstream provider-account identity.
**Hard blocker:** M002 closure and a stable route principal/scope contract.

## 2. Baseline

`server/inference.rs` extracts headers but no principal; in
`coordinator/endpoints.rs` `execute_endpoint` parses, resolves aliases
and selects finite/stream. `coordinator/finite.rs::FiniteRequest` and
`coordinator/streaming/types.rs::StreamRequest` carry inference state.
`coordinator/publication.rs::PublicationInput` inserts `requests`
indexed by provider-account `account_id`; `FinalizationIdentity`
persists the selected account/model. `finalization.rs` and
`reconciliation.rs` settle/recover. `server/health.rs::models_api`
and `integration_profile` currently have no downstream user filter.

### Always-preserved invariants
- A single on-prem instance; TOML/environment retains upstream provider topology/credentials, model routing, and operator-owned settings. SQLite owns only downstream users, sessions, grants, profiles, budget reservations, consumption and audit.
- No SaaS organizations, hosted signup, embedded reverse proxy/VPN, enterprise identity dependency, separate service/database, provider credential web editor, or assertion of legal authority to share personal subscription seats.
- All legacy inference wire paths and return shapes (Chat Completions, Messages, Responses, Responses compact), streaming/terminal semantics, redaction, configuration lifecycle, and operator CLI remain compatible unless an explicit migration contract states otherwise.
- Respect `GenerationLease`, coordinator publication/finalization compensation, one SQLite gate/worker with WAL/NORMAL, `config_reload_policy.rs` as reload authority, and `--no-default-features` compilation.
- In-flight resource ownership remains deterministic. No database locks span provider I/O or expensive password hashing. No raw prompt, token, password, session, provider key, or cache key reaches diagnostics or ordinary request storage.

### Shared repository verification
Run on implementation and report actual results in the closure record, not the plan:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```
For dependency changes also run locked release build, `cargo deny` and
`cargo tree -e features`; for user-facing release-doc edits run
`uv run python scripts/validate_release_docs.py`. Ensure `git diff --check`.

## 3. Required production changes

### A — One ingress principal
Create typed `AuthenticatedPrincipal {user_id, credential_id, role/scopes,
auth_epoch}` (or narrower equivalent). Server middleware resolves and
sets it as request extension; do not pass mutable user IDs in untrusted
headers or provider payload. Route handlers consume it and use same
principal for all `/v1/*`, including `/v1/responses/compact`.
Leave generation lease handling untouched and avoid public-request-struct
breakage where compatibility demands private wrappers.

### B — Request ownership and propagation
Thread principal through the one parsed request endpoint decision into
finite, streaming, compact, internal semantic selector and all retries.
Do not allow selector calls to accidentally acquire a second operator
identity or escape all accounting; explicitly record internal request
parent/child linkage, or define an auditable attribution strategy without
duplicating user logical request counts. In future M004 ensure any
unsupported identity propagation path fails before provider dispatch.
Prefer stable `user_id` across redirects, retries and failovers, never
derived from provider account.

### C — Durable attribution
Append a migration with nullable `requests.user_id` and credential ID
where genuinely necessary (avoid storing full bearer secret). Add appropriate
indexes for user/recent queries and FK semantics so disabling/removing
accounts preserves historical attribution. New managed requests must always
have non-null user ID; existing rows explicitly legacy/unattributed.
Carry owner identity into `FinalizationIdentity`; idempotent request
republication and terminalization require identity equality before
resuming an existing row, preventing cross-user request-ID collisions.
Retries must not overwrite user identity, even when provider account
changes. Add bounded user-filtered query helpers, not full-table scan or
user dimensions on every existing provider rollup by default.

### D — Invalidation contract and discovery seam
Resolve current user active status and scope on **new** request. Snapshot
the relevant authorization epoch per request; decide explicitly that
already-dispatched requests may finish after revocation while no new
request can start. Provide typed authority for M004 model-filtered
`/v1/models` and `/api/integrations/v1/profile` with revision/ETag
invalidation when policy changes, without yet implementing model rules.

## 4. Ordered work packages

1. Define principal, lifetime and request-extension scope, cross-surface tests.
2. Propagate principal through endpoint/finite/stream/compact/selector
   while preserving source-native no-rewrite and existing wire fixtures.
3. Migrate request attribution and publication/finalization invariants,
   including crash recovery and replay/duplicate semantics.
4. Add filtered data-access seams, versioned discovery contract and
   memory/latency evidence.

## 5. Failure/cancellation/restart/contention

Identity persists before dispatch as part of the same publication
transaction and survives cancellation/finalization supervision; do not
misattribute a retried account. A stale/disabled principal at admission
fails closed, but an already-started stream's owner stays stable for
settlement. Old process-generated requests retain lease-scoped immutable
identity on config reload. DB/read outage must not degrade into anonymous
managed inference. No second SQLite writer/connection.

## 6. Compatibility/migration

Keep old `requests` rows queryable; never overwrite historical caller
identity as initial admin. Ensure old analytics, provider account view,
`model_routers`, response bodies and client configurations remain
unchanged. Explicitly identify old shared-key inference as a legacy
principal until removed. Schema checksum integrity and backup restore are
mandatory.

## 7. Focused verification

New `rust/tests/on_prem_attribution.rs` and extend
`coordinator_publication.rs`, `coordinator_finalization.rs`,
`coordinator_boundaries.rs`, `coordinator_c008.rs`,
`coordinator_c009.rs`, `coordinator_c011.rs`,
`database_compatibility.rs`, `codex_compaction_compat.rs`.
Negative: spoofed downstream user header, cross-user duplicate
`proxy_request_id`, alternate provider retry, stream abrupt EOF,
compact native dispatch, semantic internal selector, password revocation
mid-stream and foreign-key attribution after user disable.
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_attribution -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
```
Also run shared suite.

## 8. Acceptance, docs and closure

Update `architecture/deep-dive-request-lifecycle.md`,
`architecture/deep-dive-database.md`,
`architecture/deep-dive-observability.md` and protocol docs where needed.
Accept when every managed inference request is stably and durably owned,
including retries, compact and aborted streams, and existing provider
metrics stay stable. Stop if public coordinator ABI would regress or
selection/compensation owner is ambiguous. Closure
`plans/closure/on-prem-user-access/003-status.md` needs per-surface
and fault-injection matrix, migration/legacy fixtures, measured overhead,
commands/results and unblock audit.

