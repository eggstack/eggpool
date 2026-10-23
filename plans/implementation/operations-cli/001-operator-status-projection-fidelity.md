# Operations CLI Milestone 001 — Operator status projection fidelity

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/operations-cli-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for observed status facts
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. The typed status schema and the CLI command surface are
  unchanged.
- Stop for architecture review if the correction would change the server's
  published status schema, which is a compatibility surface for the embedded
  dashboard assets and for the dashboard parity tooling.

Primary class: invariant

## 1. Objective

Make the CLI's offline status fallback structurally identical to the server's
published status schema by constructing the typed projection instead of a
hand-written JSON literal, and make the redaction boundary between them real
instead of a passthrough.

## 2. Why this milestone is ready

No dependencies. The typed status structs already exist and are already
serialized by the server. This milestone changes which construction path the
CLI offline fallback uses.

## 3. Current implementation evidence

**The typed status schema has one owner.** `rust/src/operations/status.rs`
defines `ProxyStatus`, `ProviderStatus`, `ProviderObservation`,
`ProxyHealthSummary`, `ProviderHealthSummary`, `RuntimeHealthSummary`, and
`ProxyStatusSnapshot`, all of which derive `Serialize`. The server serializes
these types directly.

**The CLI offline fallback builds the same payload by hand.** In
`rust/src/runtime.rs` the offline fallback assembles a JSON literal whose
`"proxy"` object carries the same field names as `ProxyHealthSummary`, whose
`"providers"` entries carry the same field names as `ProviderHealthSummary`,
and whose `"runtime"` object carries the same field names as
`RuntimeHealthSummary`. The two shapes are related only by convention. A field
added to a typed struct is silently absent from the fallback, and a field
removed from a typed struct silently persists in the fallback.

**The redaction helper is a passthrough.** `rust/src/runtime.rs` defines
`sanitize_status_value`, which returns its input unchanged. The accompanying
comment states that the server snapshot is already secret-free by
construction, which is an assertion about a different component standing in
for a sanitizer. There is no test that fails if a secret-shaped value
survives the CLI projection.

**The provider entries are assembled separately.** The fallback builds each
provider entry as its own JSON literal and then sorts and truncates the
collection, so the per-provider shape is a second hand-maintained copy of
the typed provider summary.

## 4. Invariants that must not regress

- The server's published status schema is unchanged.
- The CLI's human-readable status output wording is unchanged.
- The CLI command tree and argument surface are unchanged.
- The offline fallback continues to report that the server is unavailable
  rather than claiming a live server.
- The provider collection continues to be sorted and bounded by the same
  maximum.
- No credential, prompt, raw body, or cache key is printed by any operator
  projection.
- The CLI never mutates runtime state that the running server generation
  owns.

## 5. Scope

### In scope

- The construction of the CLI's offline status fallback.
- The redaction helper applied to operator status projections.
- Regression tests and guards for schema parity and redaction.

### Explicitly out of scope

- The PID lock handling, deadline arithmetic, and numeric conversions. That is
  milestone 002 in this subsystem.
- Any change to the server's published status schema or to the embedded
  dashboard assets.
- Any change to the human-readable rendering of the CLI status output.
- Any change to the CLI command tree.
- Any change to how the online status snapshot is read back over the control
  socket.
- Any change to the dashboard subsystem's projections.

## 6. Required production changes

**Typed offline fallback.** The offline fallback must construct the typed
projection structs from `rust/src/operations/status.rs` with explicit
unavailable values, and serialize them through the same `Serialize` path the
server uses. The fallback must then be structurally incapable of drifting
from the server schema, because it is the same type.

**Reordering risk.** The server's snapshot field order is determined by the
struct declaration order. The fallback's current literal order matches the
struct order today. Constructing the typed value preserves that order
automatically, which is one of the reasons the change is safe.

**Redaction.** The redaction helper must perform a real projection that
removes secret-shaped material, and both the online and offline paths must
route through it. A value that the server already redacts must not be
reintroduced by the CLI, and the CLI must not redact a field the server
publishes verbatim in a way that changes its meaning.

**Guard.** Add a structural guard that asserts the offline fallback and the
server projection come from one schema source. A field added to a typed
struct must not require a matching change anywhere else.

## 7. Ordered work packages

### Work package A — Typed offline fallback construction

Intent:

Remove the hand-written JSON literal from the CLI offline fallback.

Required changes:

- Build the typed status structs in the offline fallback.
- Serialize through the same `Serialize` implementation.
- Preserve the unavailable semantics, the provider ordering, and the
  provider bound.

Acceptance evidence:

- A test asserts the offline fallback's field set equals the server
  projection's field set.
- A test asserts the provider entries in the fallback carry the typed
  provider summary's full field set.
- A test asserts the fallback still reports the server as unavailable.

### Work package B — Real redaction boundary

Intent:

Make the redaction helper a real projection.

Required changes:

- Implement the redaction so that secret-shaped material does not pass.
- Route both the online and offline status paths through it.

Acceptance evidence:

- A test seeds a secret-shaped value into a status fact and asserts it does
  not appear in the CLI projection.
- A test asserts the redacted projection still carries the non-sensitive
  fields.

### Work package C — Structural schema-parity guard

Intent:

Stop the fallback and the server schema from drifting again.

Required changes:

- Add a guard asserting one schema source for both.

Acceptance evidence:

- The guard fails when a field is added to a typed status struct without a
  corresponding change elsewhere, which after work package A is impossible by
  construction.

## 8. Failure, cancellation, restart, contention semantics

- The offline fallback is used when the server is unreachable. It must keep
  producing the current unavailable-shaped result rather than an error.
- The redaction must not panic on large or unusual values.
- No cancellation path is added or changed.
- No restart behavior changes.

## 9. Compatibility and migration

- No migration. No storage change.
- No configuration key is added, removed, or reclassified.
- No public HTTP or CLI contract change. The server's published schema is
  unchanged, and the CLI's output remains field-for-field what it is today.
- No public Rust API break. The typed status structs keep their names,
  fields, and serialization.

## 10. Required tests

- `rust/tests/status_command.rs`: the CLI status command output and the
  offline fallback parity coverage.
- A focused test target may be added under `rust/tests/` for the schema
  guard and the redaction regression. Name it for the behavior, not for a
  milestone number.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/overview.md` — add an entry for the operations CLI surface and
  its ownership, so the review index covers it.
- `docs/cli-reference.md` — only if a command's output changes, which this
  milestone must not cause.

## 13. Acceptance criteria

- The offline fallback and the server status projection are the same type, so
  a field change to the schema cannot leave one behind.
- The offline fallback's field set equals the server projection's field set.
- A secret-shaped value does not survive the CLI status projection.
- The server's published status schema and the CLI's human-readable output
  are unchanged.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- The correction would change the server's published status schema.
- The correction would change the CLI's human-readable output.
- The typed structs cannot express the fallback's unavailable semantics
  without adding a variant to the published schema.
- Evidence contradicts an invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that the pre-milestone fallback drifted when a typed field was
  added, so the new guard is a genuine regression guard.
- The documentation diff showing the review index entry.

## 16. Handoff notes

This is a small, self-contained correction. It touches a large file, so keep
the diff tight: the change should be the fallback construction, the redaction
helper, and the tests.

Do not use this milestone to reduce the size of `rust/src/runtime.rs`. The
decomposition is deferred in the roadmap's deferred-work section.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.
