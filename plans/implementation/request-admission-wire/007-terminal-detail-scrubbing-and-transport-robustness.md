# Request Admission and Wire Milestone 007 — Terminal detail scrubbing and transport robustness corrections

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for admission errors and downstream observations
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. Every change is internal to EggPool: no public contract
  changes, no new protocol, and no dependency change.
- Stop for architecture review if secret redaction requires a new durable
  contract or a new operator-visible error field.

Primary class: invariant

## 1. Objective

Make the persisted terminal error detail actually free of secret material
before it reaches durable storage, and close two transport-input parsing
defects that accept malformed values as valid.

## 2. Why this milestone is ready

No dependencies. Every defect is local to an existing module, has a
deterministic failing case, and needs no new dependency, schema, or physical
target.

## 3. Current implementation evidence

**The terminal detail sanitizer does not remove secrets.**
`rust/src/coordinator/finalization.rs` defines `sanitize_detail`, which
filters control characters other than newline and tab and truncates the result
to a 512-byte limit. It performs no redaction of credential-shaped material.
The sanitized value is then written to durable storage: the finalization
write path binds `error_detail` into an insert and an update statement in
the same file. The detail value originates from failure outcomes that carry
upstream error text, and upstream error text can carry an echoed credential
or an echoed request header. The current sanitizer bounds the length and the
character set but not the content.

**A negative retry-after date is accepted.** The HTTP-date parser in
`rust/src/coordinator/failure.rs` validates the parsed clock fields with an
upper-bound-only check on the hour, minute, and second. A value such as
`-1:00:00` satisfies that check, and the resulting negative epoch offset is
then used to compute a retry-after duration. The month and day are validated
by an allow-list, and the day has an upper bound, but the clock fields do not
have a lower bound.

**The authorization scheme token is matched case-sensitively.** In
`rust/src/server/middleware.rs` the provided API key is extracted by
stripping either the prefix `Bearer ` or the prefix `bearer `, then falling
back to the `x-api-key` header. The authentication scheme token is
case-insensitive by specification, so a client sending `BEARER ` is
rejected even though the credential itself is correct.

## 4. Invariants that must not regress

- Credentials, prompts, raw bodies, and cache keys stay out of persistence,
  logs, and diagnostics.
- The admission path performs one endpoint execution call and reuses one
  parsed body for finite and streaming selection, depth validation, model
  mutation, and `from_admitted` construction.
- The body-size limit remains generation-owned.
- The server's authentication behavior, the constant-time comparison, and the
  loopback exemption remain unchanged.
- No public HTTP status code, route, or response body shape changes.
- The routing-decision schema, exclusion reason codes, and public
  `QuotaFairScorer` API remain unchanged.
- No new dependency, concurrent map, or lock architecture change.

## 5. Scope

### In scope

- The secret redaction performed by the terminal detail sanitizer in
  `rust/src/coordinator/finalization.rs`.
- The lower-bound validation of the retry-after HTTP-date clock fields in
  `rust/src/coordinator/failure.rs`.
- The case-insensitive authorization scheme token matching in
  `rust/src/server/middleware.rs`.
- Regression tests and guards for each of the above.

### Explicitly out of scope

- The compact endpoint capability contract. That is milestone 008 in this
  subsystem.
- Any change to the retry decision itself — how many attempts are made or
  which scope is retried.
- Any change to the client-facing error text that a downstream client
  receives. Only the durable copy is redacted.
- Any change to the authentication policy, the credential comparison, the
  header names accepted, or the loopback exemption.
- Any change to the admission rules, the stateless policy, or the depth
  validation.
- Any change to the wire codec set or the kernel boundary.

## 6. Required production changes

**Terminal detail redaction.** The sanitizer used before a terminal detail is
persisted must remove credential-shaped material, not merely bound its length
and character set. The redaction must be applied on the write path so that
every persisted value is redacted, including values that arrive through the
second write path in the same file and values produced by the streaming
terminal path. The redaction must be secret-free in its own output: the
redacted detail must not contain the secret it removed, and must not record
the removed value in any form.

The redaction approach must be conservative about false positives. A detail
that merely resembles a token but is not credential material should be
preserved where practical, because the detail is operator-facing diagnostic
context. If a precise rule is not achievable, a conservative over-redaction
is acceptable and must be documented, but the rule must be deterministic.

**Retry-after clock validation.** The HTTP-date parser must reject clock
fields outside their valid ranges, including negative values, rather than
accepting them and computing a negative offset.

**Authorization scheme matching.** The scheme token must be matched
case-insensitively while the credential comparison itself stays
constant-time and unchanged.

**Guard.** Add coverage that a credential-shaped value written through the
terminal detail path does not appear in the persisted row.

## 7. Ordered work packages

### Work package A — Redaction on the terminal detail write path

Intent:

Ensure no credential-shaped material reaches durable storage in the terminal
detail column.

Required changes:

- Extend the sanitizer in `rust/src/coordinator/finalization.rs` with
  credential redaction.
- Apply it on every path that persists a terminal detail, including the
  update path and the streaming terminal path.

Acceptance evidence:

- A test writes a detail containing credential-shaped material and asserts
  the persisted value contains none of it.
- A test asserts an ordinary diagnostic detail survives the sanitizer
  substantially intact, bounding the over-redaction.
- A test asserts the redacted value does not contain the removed secret in
  any form, including a truncated prefix.

### Work package B — Retry-after clock field validation

Intent:

Reject malformed retry-after dates instead of computing a negative offset.

Required changes:

- Add the lower bounds to the clock-field validation in
  `rust/src/coordinator/failure.rs`.
- Treat an out-of-range value as "no usable retry hint" rather than as an
  error, so the existing fallback applies.

Acceptance evidence:

- A test asserts a negative clock field is not accepted as a retry hint and
  that the existing fallback duration is used instead.
- A test asserts a valid HTTP-date still produces the same retry hint as
  before.

### Work package C — Case-insensitive authorization scheme token

Intent:

Accept the authorization scheme token in any letter case.

Required changes:

- Match the scheme token case-insensitively in
  `rust/src/server/middleware.rs`, keeping the credential comparison
  unchanged.

Acceptance evidence:

- A test asserts a request using an upper-case scheme token with a correct
  credential is accepted, and that an incorrect credential is still rejected.
- A test asserts the constant-time comparison path is unchanged.

## 8. Failure, cancellation, restart, contention semantics

- A redaction rule that cannot classify a value must redact rather than
  persist. Under-redaction is a security defect; over-redaction is a
  diagnostic-quality defect and is the safer failure.
- The redaction must not allocate unboundedly or panic on very large or
  non-UTF-8-adjacent input.
- The retry-after validation change must not turn a previously accepted
  request into a hard error. The fallback path must continue to work.
- The authorization change must not alter the constant-time comparison, the
  loopback exemption, or the header set.
- No cancellation path is added or changed.
- No restart behavior changes.

## 9. Compatibility and migration

- No migration. No column, table, or index changes.
- No config key is added, removed, or reclassified.
- No public HTTP status code, route, or response body shape changes. The
  redacted value is a durable diagnostic column, not a client-visible field.
- No public Rust API break.
- Existing persisted rows are left as they are. This milestone does not
  retroactively scrub durable history; if the durable history needs
  scrubbing, that is a separate operator-facing decision.

## 10. Required tests

- `rust/tests/coordinator_finalization.rs` or the closest existing
  finalization target: redaction coverage on the write path.
- `rust/tests/coordinator_failure.rs` or the closest existing failure target:
  retry-after parsing coverage.
- `rust/tests/server_auth.rs` or the closest existing authentication target:
  scheme-token case coverage.
- A focused test target may be added under `rust/tests/` if the corrections do
  not fit the existing files. Name it for the behavior, not for a milestone
  number.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_auth -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/deep-dive-security.md` — record the terminal detail
  redaction rule and the scheme-token matching behavior.
- `architecture/deep-dive-request-lifecycle.md` — record the retry-after
  validation rule if it describes the retry-hint contract.
- `docs/api-reference.md` — no change expected; confirm the client-visible
  error text is unchanged.

## 13. Acceptance criteria

- A terminal detail containing credential-shaped material does not persist
  that material in any form.
- An ordinary diagnostic detail survives redaction substantially intact.
- A retry-after HTTP-date with an out-of-range clock field is not accepted
  as a retry hint, and the request still succeeds through the fallback path.
- An authorization request with an upper-case scheme token and a correct
  credential is accepted; an incorrect credential is still rejected.
- No public HTTP contract, routing-decision schema, or config classification
  changes.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- The redaction cannot be made deterministic without a new durable contract
  or a new operator-visible field.
- The redaction would redact a large fraction of ordinary diagnostic detail,
  which would make the column useless.
- The scheme-token change would require altering the credential comparison or
  the loopback exemption.
- The retry-after change would turn a previously accepted request into a hard
  error.
- Evidence contradicts a security invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that the pre-milestone sanitizer persisted the credential-shaped
  material, so the new coverage is a genuine regression guard.
- A statement of the redaction rule in prose, including its known
  over-redaction behavior.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

Work package A is the security-relevant one and the others are small. Land
them together, but do not let the small ones delay A.

Redaction is the highest-risk part of this milestone. Prefer a conservative,
deterministic, documented rule over an ambitious one. An over-redacted
diagnostic is a much smaller problem than a persisted credential.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.
