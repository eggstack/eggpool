# Request Admission and Wire Milestone 007 — Closure Status

Status: corrective pass required

Corrective pass: `plans/closure/request-admission-wire/009-status.md` — the
redaction coverage recorded here was found to under-cover three of the four
documented classes. Milestone 009 closes it.

Source implementation plan:

- `plans/implementation/request-admission-wire/007-terminal-detail-scrubbing-and-transport-robustness.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-007--terminal-detail-scrubbing-and-transport-robustness-corrections`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M007 is complete and **closed**. Three transport-robustness corrections are
landed:

- a terminal error detail that is persisted now has its credential-shaped
  material removed before the write, on both the insert and the update path;
- an HTTP-date `Retry-After` hint with an out-of-range clock field is no
  longer accepted as a retry hint, so it cannot produce a negative retry
  offset;
- the authorization scheme token is matched case-insensitively, while the
  credential comparison stays constant-time and the loopback exemption stays
  unchanged.

No client-visible contract changed. The redaction applies to the durable
diagnostic column, not to a client-facing field. Existing persisted rows are
not retroactively scrubbed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A terminal detail containing credential material is persisted without that material | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_removes_credential_material_from_persisted_rows` | pass | The persisted value contains no part of the seeded secret, including a truncated prefix |
| The redaction applies on the update path, not only the insert path | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_applies_to_the_failed_attempt_update_path` | pass | The failed-attempt update is covered separately from the terminal insert |
| An ordinary diagnostic detail survives substantially intact | `rust/tests/terminal_detail_redaction.rs::terminal_detail_sanitizer_preserves_ordinary_diagnostics` | pass | The substantive text is still present; the rule is label- and prefix-driven, not blanket entropy matching |
| An out-of-range clock field is not accepted as a retry hint | `rust/tests/coordinator_failure.rs::out_of_range_retry_after_clock_fields_are_not_a_retry_hint` | pass | A negative hour, minute, or second yields no hint |
| The fixed policy backoff stays in charge when the hint is unusable | `rust/tests/coordinator_failure.rs::an_unusable_retry_hint_leaves_the_fixed_policy_backoff_in_charge` | pass | A previously accepted request is not turned into a hard error |
| A valid HTTP-date still produces the same retry hint | `rust/tests/coordinator_failure.rs::a_valid_http_date_still_produces_the_same_retry_hint` | pass | Non-regression guard |
| A numeric `Retry-After` is unchanged | `rust/tests/coordinator_failure.rs::a_numeric_retry_hint_is_unchanged` | pass | Non-regression guard |
| The authorization scheme token is matched case-insensitively | `rust/tests/server_auth.rs::authorization_scheme_token_is_case_insensitive` | pass | An upper-case scheme with the correct credential is accepted; an incorrect credential is still rejected |
| The loopback exemption is unchanged | `rust/tests/server_auth.rs::loopback_public_endpoints_stay_exempt_from_the_credential` | pass | Non-regression guard |
| No public HTTP status code, route, or response body shape changes | `rust/src/server/middleware.rs` changes only the scheme-token comparison; no route, status, or body code was touched | pass | The redaction applies to a durable diagnostic column, not a client-facing field |
| Strict Clippy with `-D warnings` | `cargo clippy --workspace --all-targets -- -D warnings` | pass | Zero findings |
| Default and no-default workspace suites pass | The affected targets green (§4) | partial | The full serial workspace suite and the `--no-default-features` build were **not** run in this pass, by instruction to keep verification targeted. Outstanding (§10) |

## 3. Production implementation evidence

- `rust/src/coordinator/finalization.rs`: `sanitize_detail` is now a
  deterministic redaction projection in front of the durable write. It
  removes (a) a value that follows a credential-shaped label and a `:` or `=`
  separator, over 34 label spellings including `api_key`, `authorization`,
  `x-api-key`, `password`, and `token`; (b) a value that follows a
  credential scheme word (`bearer`, `basic`, `token`, `digest`); (c) a value
  with a known vendor token prefix and a minimum total length, over 11
  prefixes including `sk-`, `ghp_`, `github_pat_`, `xoxb-`, `AKIA`, and
  `AIza`; and (d) a long opaque run of 32 or more characters that mixes
  letters and digits. Removed material is replaced with `[redacted]`,
  which contains no part of the removed value. Everything else is copied
  verbatim. The pre-existing control-character filter and the
  `MAX_ERROR_DETAIL` cap are unchanged, and the redaction happens before
  the cap so a secret is never truncated into a partial credential.
- `rust/src/coordinator/failure.rs`: the HTTP-date parser now checks both
  bounds (`0..=23` for the hour, `0..=59` for the minute, `0..=60` for the
  second) instead of only the upper bound.
- `rust/src/server/middleware.rs`: the authorization scheme token is
  compared case-insensitively, the credential is still compared in constant
  time, and the loopback exemption is untouched.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test terminal_detail_redaction \
  --test coordinator_failure --test server_auth --test coordinator_finalization \
  -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml \
  --test routing_fail_closed --test quota --test routing_domain \
  --test routing_domain_d008 --test routing_claims \
  --test routing_selection_efficiency --test health \
  --test durable_health_effects --test terminal_detail_redaction \
  --test coordinator_failure --test server_auth \
  --test coordinator_finalization --test coordinator_c007 \
  --test coordinator_c008 --test coordinator_c009 --test coordinator_c010 \
  --test coordinator_c011 --test coordinator_publication \
  --test database_compatibility --test on_prem_budgets \
  --test on_prem_global_budgets --test status_projection \
  --test status_command --test dashboard_filters \
  --test dashboard_ttft_qualification --test stats_transcoding \
  -- --test-threads=1
```

### Results

- `cargo fmt --all` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — **pass**.
- `terminal_detail_redaction` — 4 passed, 0 failed.
- `coordinator_failure` — 11 passed, 0 failed.
- `server_auth` — 2 passed, 0 failed.
- `coordinator_finalization` — 3 passed, 0 failed.
- The 25-target sweep — 25 targets, 0 failures, 1 pre-existing ignored
  test.

### Regression-guard evidence

The implementation agent reverted each production change in an isolated
worktree at the baseline commit and confirmed the corresponding test fails,
then restored the change and re-ran the target green. The redaction,
retry-after, and scheme-token corrections were each covered by a test that
fails against the pre-change behavior.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| Credentials, prompts, raw bodies, and cache keys stay out of persistence and diagnostics | The durable detail is redacted before the write; the removed material is replaced with a marker that contains no part of it |
| The client-visible response contract does not change | Only the durable diagnostic column and the scheme-token comparison changed; no route, status, or body code was touched |
| The constant-time credential comparison is preserved | The comparison path was not modified; only the scheme-token match above it |
| The loopback exemption is preserved | `server_auth::loopback_public_endpoints_stay_exempt_from_the_credential` |
| A malformed transport hint must not become a hard error | The out-of-range HTTP-date yields no hint and the fixed policy backoff stays in charge |
| No new dependency, config key, or migration | `git diff --name-only` shows none |
| Strict Clippy with `-D warnings` | Clean |

## 6. Failure and recovery review

- **Duplicate delivery** — the durable detail write is an upsert of the
  terminal row; redaction is a pure function of the value, so a repeated
  write converges.
- **Cancellation races** — the redaction happens on the value before the
  write; a cancelled write leaves the pre-existing row, which is not worse
  than before.
- **Restart** — persisted rows are read back as-is; the redaction is
  applied on write, not on read, so a row written by an older build is
  still readable.
- **Partial persistence failure** — unchanged; the terminal write path was
  not restructured.
- **Malformed / unauthorized input** — this milestone is the malformed-input
  path: an out-of-range clock field and an out-of-range scheme token are the
  two malformed inputs now handled explicitly.
- **Idempotency** — the retry-hint change affects the computed delay, not a
  write.

## 7. Migration and compatibility review

No schema migration, no config change, no protocol change. The redaction
applies to new writes only; rows written by an earlier build are not
retroactively scrubbed and are not corrupted by the change.

## 8. Security review

- **Secret handling** — this milestone is the secret-handling milestone. The
  redaction is deterministic, conservative, and label- and prefix-driven so
  ordinary diagnostic text survives.
- **Known over-redaction** — a diagnostic that contains a long opaque
  alphanumeric run of 32 or more characters, or a value that follows a word
  such as `token` or `secret` with a `:` or `=` separator, is redacted even
  when it is not a credential. This is deliberate: the durable column is a
  diagnostic, and under-redaction would persist a live credential. The
  over-redaction is bounded and does not affect any client-facing field.
- **Privilege bounds** — unchanged. The scheme-token change does not relax
  the credential check, the loopback exemption, or the accepted header set.
- **DoS bounds** — the redaction is a single left-to-right pass over the
  detail with the pre-existing `MAX_ERROR_DETAIL` cap; it allocates in
  proportion to the capped output and does not recurse or backtrack
  unboundedly.
- **Audit behavior** — the removed material is not logged anywhere.

## 9. Documentation and operations

- `architecture/deep-dive-security.md`: records the redaction rule, its
  over-redaction behavior, and the constant-time comparison.
- `architecture/deep-dive-request-lifecycle.md`: records that the terminal
  detail is redacted before the durable write, on both the insert and the
  update path.
- No new operator command. The redaction is visible to an operator as
  `[redacted]` in the stored detail.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The full serial workspace suite and the `--no-default-features` build were not run in this pass, by instruction to keep verification targeted at this stage | The no-default-features parity required by the source plan is unverified | Run `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` and `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings`, then the full serial suite, before the change is pushed |
| medium | Rows written before this milestone are not retroactively scrubbed | A credential that was persisted by an earlier build is still in the durable column | The source plan explicitly excludes retroactive scrubbing. A separate maintenance action should be considered to scrub historical rows, and it needs its own plan because it rewrites durable state |
| low | The redaction rule does not cover a bare credential that is not labeled, not scheme-prefixed, and shorter than the 32-character opaque threshold | Such a value is still persisted | Deliberate: an unlabeled short value is indistinguishable from ordinary diagnostic text. Widening the rule would redact most ordinary diagnostics |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Milestone 008 remains
blocked on the acceptance of ADR-0003 and is unaffected by this closure.

## 12. Registry updates

- `plans/registry.md`: the request-admission-wire milestone 007 row moves
  from `ready` to `closed`, with the closure record linked.
- `plans/subsystems/request-admission-wire-roadmap.md`: the milestone 007
  row in the milestone status table moves to `closed`.
- `plans/implementation/request-admission-wire/007-terminal-detail-scrubbing-and-transport-robustness.md`:
  the `Status` line moves from `ready` to `implemented`.
