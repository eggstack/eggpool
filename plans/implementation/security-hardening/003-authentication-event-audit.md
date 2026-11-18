# Security Hardening Milestone 003 — Authentication event audit

Status: active

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-3--authentication-event-audit`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  items 8 (secret-free diagnostics), 11 (single-instance local authority), and
  12 (identity-bound policy and accounting)
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:

- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`
  — session and throttle decisions are unchanged; this milestone only records
  their outcomes.
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` — the
  identity model and the local-operator trust boundary are unchanged.
- No new ADR is required: authentication decisions do not change.

Primary class: infrastructure

## 1. Objective

Add a bounded, durable, content-free record of authentication and
authorization outcomes so an operator can see a brute-force or token-stuffing
attempt after the fact, without adding a synchronous database write to any
authentication path.

## 2. Why this milestone is ready

No hard dependencies. The bounded-channel + single-writer pattern already
exists in the metrics coalescer and the task supervisor; the refusal branches
already live in one middleware and one sign-in handler; the admin audit page
already has an administrator-only section model. The schema change is additive.

## 3. Current implementation evidence

- `rust/src/web_session.rs` — login failures are process-local buckets; they
  are not persisted and are lost at restart. A successful sign-in clears the
  account bucket.
- `rust/src/identity.rs` — `verify_password`, `verify_api_token_principal`,
  and `access_state` return outcomes to callers but record nothing.
- `rust/src/server/auth.rs` — login refusals (transport, origin, throttle,
  invalid credentials) return responses without a durable record or a
  structured event.
- `rust/src/server/middleware.rs` — 401/403 refusals (missing session, missing
  token, member-on-admin, transport/origin rejection) likewise return without
  a record.
- `rust/src/identity.rs` — `user_admin_audit`, `user_budget_rule_audit`, and
  `gateway_audit_export` cover administrative mutations only.
- `rust/src/operations/metrics.rs` — the existing coalescer is the reference
  for bounded background writes; `rust/src/task_supervisor.rs` owns shutdown.
- `rust/assets/db/migrations/0066_personal_upstream.sql` is the current
  migration head.
- `rust/src/server/dashboard/admin.rs` and
  `rust/src/server/dashboard/render/admin.rs` own the administrator audit
  page.

## 4. Invariants that must not regress

- No authentication decision awaits a database write, channel send, or lock
  held by the writer. A full channel drops an event, never the request.
- No row stores a credential, token, prompt, request body, header value, raw
  IP address, or username.
- The writer is owned by the process runtime and stops within the existing
  shutdown deadline.
- The single SQLite gate is respected; the writer uses the existing
  `Database` handle and does not open a second connection.
- The content-free JSON audit export shape is unchanged (see Scope).
- Existing throttle limits, lockout semantics, and response status codes are
  unchanged.

## 5. Scope

### In scope

- One additive migration after `0066`.
- A typed event vocabulary for sign-in, session, token, scope, CSRF, origin,
  transport, and member/admin authorization outcomes.
- A bounded sink and coalescing writer; minute-window aggregation with a
  count.
- Emission from the existing refusal branches.
- An administrator-only security-events section on the existing audit page.
- Structured `tracing` events carrying only the event code and outcome.

### Explicitly out of scope

- Changing `gateway_audit_export`'s JSON shape or adding a `security` kind.
  If export is wanted, it is a follow-up plan with a contract update.
- An HTTP access log.
- Storing raw IP addresses, usernames, user agents, or request paths.
- Alerting, webhooks, or retention configuration.
- Changing any authentication or throttling decision.

## 6. Required production changes

- Migration `0067_security_events.sql`: one table with an id, a window start,
  a typed event code, an outcome, a principal kind, an optional internal user
  id, and an aggregated count. Indexes support the newest-first admin read and
  the window upsert. No foreign key is required; a deleted user leaves the
  internal id as a historical reference.
- `rust/src/security_events.rs` (new): event/outcome/principal-kind enums with
  fixed string codes, a bounded sink, a coalescing writer, and retention
  pruning.
- `rust/src/operations` or process runtime: construct the sink with the
  database and a task-supervisor handle; expose a non-blocking `record`
  method; flush on shutdown.
- `rust/src/server/auth.rs`, `rust/src/server/middleware.rs`, and
  `rust/src/web_session.rs`: emit events at the refusal branches. Pass the
  optional sink through the existing state; a missing sink is a no-op for
  tests.
- `rust/src/identity.rs` and the dashboard admin module: query and render the
  administrator-only section; resolve a username for display only when the row
  carries a user id.

## 7. Ordered work packages

### Work package A — Schema and typed vocabulary

Intent: one durable, bounded shape.

Required changes: the migration, enums, and repository read/write methods.
Keep the vocabulary closed; an unknown code is a compile error, not a free
string.

Acceptance evidence: `database_compatibility` applies the chain to an older
database and reads the new table; unit tests round-trip every code.

### Work package B — Bounded writer

Intent: never block or fail an auth decision.

Required changes: capacity-bounded channel; `try_send` at the emission point;
a single task that aggregates into a minute window and upserts; a retention
prune (start with 30 days or 10,000 rows, whichever binds first; adjust only
with SBC evidence); a drop counter; a bounded flush at shutdown.

Acceptance evidence: a full-channel test returns immediately and increments
the drop counter; a flood test keeps the table bounded; shutdown flushes
without exceeding the existing deadline; a restart preserves rows.

### Work package C — Emission points

Intent: cover the outcomes that matter without touching decisions.

Required changes: login success, invalid credentials, account throttle,
session rejection, missing/invalid personal token, personal-token throttle,
CSRF rejection, origin rejection, insecure-transport rejection, member-on-
admin denial, and (soft dependency on M004) scope denial. One emission per
refusal branch.

Acceptance evidence: focused auth tests assert one aggregated row with the
expected code and count; a successful sign-in and a successful inference do
not emit failure events; no request text appears in any row.

### Work package D — Administrator view

Intent: make the record usable.

Required changes: a "Security events" section on `/admin/audit`, newest
first, capped with the existing page limit; event code, outcome, count,
window, and a resolved username or a dash. The section renders only for an
administrator session and never for a member.

Acceptance evidence: admin page contains the section; member request gets the
existing refusal; an event row with an unknown user id renders a dash.

## 8. Failure, cancellation, restart, contention semantics

- The sink is fire-and-forget. A closed channel, a full channel, or a writer
  task that has stopped all degrade to a dropped counter; authentication
  continues.
- Aggregation is per minute window; two events in different windows are two
  rows by design.
- On shutdown the writer drains what the channel already holds under a bounded
  timeout. Events not drained are lost; this is recorded in the closure
  evidence, not hidden.
- Concurrent emitters contend only on the channel's internal lock; the writer
  serializes database access through the existing gate.

## 9. Compatibility and migration

One additive migration. Older binaries ignore the table; a restored backup
without it runs the migration on next start. The admin page gains a section;
no existing row, column, page field, or JSON export field changes. The event
vocabulary is internal and may gain codes in later milestones.

## 10. Required tests

- `rust/tests/database_compatibility.rs` — migration on an upgraded database.
- `rust/tests/server_auth.rs` — login success/failure/throttle events;
  transport and origin refusals; no secret-bearing content.
- `rust/tests/on_prem_sessions.rs` — session and token refusals.
- `rust/tests/on_prem_admin_ui.rs` — admin-only section, member refusal.
- Unit — aggregation, window boundaries, capacity/drop behavior, retention.
- Negative — a full sink does not change a 401; a deleted user leaves a
  readable historical row.
- Full serial workspace suite.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test server_auth -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/deep-dive-security.md` — the security-event record, its
  content-free rule, its writer, and its bound.
- `architecture/overview.md` §12 — one sentence and the owning module.
- `docs/deployment.md` — what an operator can see after a suspected attempt
  and what is deliberately not stored.
- `docs/api-reference.md` only if the admin page is described there.
- `plans/registry.md` status changes.

## 13. Acceptance criteria

- Sign-in failures, throttles, token failures, session refusals, CSRF/origin/
  transport refusals, and admin denials produce aggregated rows within the
  configured window.
- A flood cannot exceed the retention bound, block a request, or grow the
  channel without bound.
- No row contains a username, credential, token, IP address, prompt, header,
  or body.
- The section is administrator-only; restart preserves rows.
- `gateway_audit_export` output is unchanged.

## 14. Stop conditions

Stop and report if:

- the writer requires a second SQLite connection or a new runtime worker pool;
- an emission point would need the raw request body, a token value, or a peer
  address;
- the admin page change would expose a new public API shape;
- the migration cannot be applied through the existing runner without
  renumbering or rewriting a migration.

## 15. Closure evidence required

- The migration and the schema-compatibility run.
- Aggregation, capacity, retention, and shutdown evidence.
- The event-to-emission-point table.
- A content audit showing no secret, username, token, IP, prompt, or body.
- Admin/member rendering evidence.
- A statement that no authentication decision or throttle limit changed.

## 16. Handoff notes

Keep the sink optional so existing unit tests that build a state without a
process runtime continue to compile. Do not derive `Debug` on a payload that
contains a principal identifier unless it is redacted. Tests run serially; the
retention prune must not run on every insert.
