# Application Boundary Milestone 002 — Content-free gateway audit export

Status: implemented

Repository baseline: `e049025274a397d7799c3ae21ed106947c2a1572`

Source roadmap:

- `plans/subsystems/application-boundary-roadmap.md` M002

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:

- `plans/adrs/ADR-0009-downstream-application-boundary.md` (accepted)
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: capability

## 1. Objective

Add one administrator-only, bounded, local export of gateway facts that
EggUI and CodeGG do not already share: who administered what, and who
called which model with what terminal outcome and token counts.

## 2. Why this milestone is ready

ADR-0009 is accepted. The export reads tables that already exist.
M001 is a soft dependency: this plan must not read tool arguments or
hosted-orchestration payloads, so it can land before or after
withdrawal.

## 3. Current implementation evidence

- `user_admin_audit` in
  `rust/assets/db/migrations/0060_user_admin_audit.sql` stores actor,
  action, target kind, target id, and time.
- `rust/src/identity.rs` `RequestAttribution` snapshots user,
  credential, auth epoch, principal kind, and request kind onto
  request rows. `recent_user_requests` returns at most 500 content-free
  rows and omits internal selector children from logical history.
- The dashboard `/admin/audit` page renders the administration rows.
  Members cannot read it. See
  `architecture/deep-dive-on-prem-user-access.md`.
- Usage rollups in `rust/src/operations/metrics.rs` are
  provider/account keyed and omit user ids.
- No CLI export joins administration and inference attribution.

## 4. Invariants that must not regress

- No prompt, raw body, cache key, tool argument, tool result, token
  secret, or provider credential in the export or its logs.
- One SQLite gate. The export is a read through `Database`.
- Members see only their own usage surface. The new export is
  administrator-only.
- Request publication and finalization ownership stay in
  `rust/src/coordinator/`.
- Internal selector child rows stay out of logical per-user history.

## 5. Scope

### In scope

- A local administrator export, CLI and one authenticated read, of
  administration audit rows and content-free inference attribution.
- A documented field list and a hard row cap.
- Tests that a forbidden field cannot appear.

### Explicitly out of scope

- Syslog, CEF, HTTP push, and a SIEM product.
- Storing prompts or answers, including an opt-in.
- A new analytics database or a second writer.
- Per-member cross-user visibility.
- Replacing the dashboard administration page.

## 6. Required production changes

Prefer a projection over `user_admin_audit` and the existing request
attribution and usage columns. Add a column only when the closure
would otherwise be unable to say whether a request was denied by a
user or instance budget, and only if that fact is a scalar reason code
already produced by budget admission. Do not add a free-text detail
column.

Expose `eggpool audit export` from the local CLI, writing to stdout as
JSON lines, and one administrator browser or token-authenticated GET
that returns the same projection. Both enforce a default limit of 500
rows and a maximum of 2000, ordered newest first. The JSON object names
its schema version. Fields are identifiers, action names, model id,
protocol, status, token counts, latency, budget reason code when
present, and timestamps. Redaction runs before serialization.

The dashboard administration page may link to the export. It does not
gain prompt columns.

## 7. Ordered work packages

### Work package A — Projection

Intent: one typed struct is the only export shape.

Required changes: a read function next to the existing audit and
attribution readers. Unit tests build rows that contain a prompt-like
column elsewhere and prove the struct has no such field.

Acceptance evidence: the serialized fixture matches a checked-in
allowlist of keys.

### Work package B — Administrator read path

Intent: CLI and HTTP use that struct, and non-administrators cannot.

Required changes: CLI command and a thin server handler. Reuse the
route-role gate in `rust/src/server/middleware.rs`.

Acceptance evidence: member and agent-token requests are denied.
An administrator receives at most the requested cap.

### Work package C — Docs

Intent: operators know this is gateway metadata.

Required changes: CLI reference, API reference, and the on-prem
architecture note. State that EggUI transcripts and CodeGG sessions
stay in those applications.

Acceptance evidence: the docs name the field list and the caps.

## 8. Failure, cancellation, restart, contention semantics

The export is a single read. A concurrent finalization may omit the
newest row; the next export sees it. Cancellation closes the response.
Restart does not lose durable rows. The read holds the SQLite gate only
for the bounded query and does not run inside inference admission.
A truncated cap is reported with a `truncated` flag rather than a
silent short page.

## 9. Compatibility and migration

No migration unless work package A proves a budget reason code is not
already stored, in which case one additive nullable column on the
existing request table is the whole schema change, with backup/restore
coverage in `rust/tests/database_compatibility.rs`. The export schema
version starts at 1. Older databases still export administration rows
and whatever attribution columns they have.

## 10. Required tests

- Allowlist of JSON keys; a fixture containing a prompt string in an
  unrelated column does not appear.
- Cap and truncation flag.
- Administrator success, member denial, agent-token denial.
- Selector-child rows excluded, matching `recent_user_requests`.
- Migration test only if a column is added.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test on_prem_attribution -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Add `database_compatibility` to the focused list if a migration is
introduced.

## 12. Documentation updates

- `docs/cli-reference.md`
- `docs/api-reference.md`
- `architecture/deep-dive-on-prem-user-access.md`
- `architecture/deep-dive-observability.md` with one paragraph that the
  export is a projection, not a new telemetry pipeline

## 13. Acceptance criteria

An administrator can export the latest gateway facts for both a chat
caller and a coding caller as JSON lines containing only the allowlisted
fields. A member cannot. The file and the HTTP body contain no prompt.

## 14. Stop conditions

Stop if the useful export requires storing prompts, tool arguments, or
provider bodies. Stop if the read cannot be served through the existing
database gate. Stop if member/admin separation would need a new role.
Do not add a network sink in this milestone.

## 15. Closure evidence required

The allowlisted fixture, denial tests, cap test, whether a migration
was added, doc links, and the serial command outcomes.

## 16. Handoff notes

Inspect the request row before adding a column. If budget denial is
already a status or error code on that row, project it and skip the
migration. Keep the export off the inference hot path.
