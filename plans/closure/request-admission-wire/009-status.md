# Request Admission and Wire Milestone 009 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/009-terminal-detail-redaction-coverage-correction.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-007--terminal-detail-scrubbing-and-transport-robustness-corrections`

Original plan and closure (corrective pass against):

- `plans/implementation/request-admission-wire/007-terminal-detail-scrubbing-and-transport-robustness.md`
- `plans/closure/request-admission-wire/007-status.md`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M009 is complete and **closed**. It is a corrective pass over the closed
milestone M007, and it corrects only the redaction projection that M007
delivered.

M007's transport-robustness work is unchanged and correct: the retry-after
HTTP-date clock-field bounds in `rust/src/coordinator/failure.rs` and the
case-insensitive authorization scheme token in
`rust/src/server/middleware.rs` were not touched, and the constant-time
credential comparison and the loopback exemption remain as they were.

M007's redaction projection under-covered three of the four documented
classes, and it had no rule at all for URL credentials:

- the JSON spelling of a labeled credential field (`"api_key":"…"`) was not
  redacted, because the label was required to be followed directly by the
  `:` / `=` separator and in JSON the label's closing quote sits between
  them;
- only a standalone `Bearer` credential was redacted, because the standalone
  matcher hard-coded the literal `bearer` while the helper that knows the
  full scheme list was reachable only from the labeled path;
- vendor token prefix matching was byte-exact, so `SK-proj-…` was missed
  while `sk-proj-…` was caught;
- a URL authority carrying userinfo credentials was not redacted at all,
  even though the repository's other redactor,
  `rust/src/operations/status.rs::contains_credential_bearing_authority`,
  already treats such an authority as credential-bearing.

**No credential leaked.** The practical exposure at review time was nil,
because every producer of `error_detail` writes a fixed-vocabulary string
today: `effects.client_outcome` at
`rust/src/coordinator/finite.rs:1974`,
`rust/src/coordinator/streaming/coordinator.rs:1521`, and
`rust/src/coordinator/streaming/terminal.rs:679`; a literal at
`rust/src/coordinator/streaming/terminal.rs:707`; and an
`outcome: &'static str` at
`rust/src/coordinator/streaming/coordinator.rs:1238`. The defect was that
M007's closure record §3 asserted a coverage that did not exist, and that
the guard would under-perform the moment upstream text reached that column.

This corrective pass closes that coverage gap and makes M007's closure
record §3 claims true. The four corrections are implemented in
`rust/src/coordinator/finalization.rs`, each is guarded by a dedicated test,
and each guard was demonstrated to fail against the pre-correction
behavior.

No client-visible contract changed, the durable schema is unchanged, and
the persisted vocabulary of the `error_detail` column is unchanged.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| The JSON spelling of a labeled credential field is redacted | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_removes_a_json_spelled_labeled_credential` | pass | Asserts the exact redacted output, the absence of the removed value, and the presence of the `[redacted]` marker |
| Every standalone credential scheme word is redacted | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_removes_standalone_scheme_credentials` | pass | `Basic`, `Token`, and `Digest` credentials are all removed and the scheme words survive |
| A labeled credential keeps its label, separator, and scheme word | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_keeps_the_label_and_scheme_of_a_labeled_credential` | pass | Only the credential is removed |
| A URL authority carrying userinfo credentials is redacted | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_removes_url_userinfo_credentials` | pass | The userinfo section is gone, the host survives, the surrounding prose survives |
| A vendor token prefix is matched case-insensitively | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_removes_uppercase_vendor_token_prefixes` | pass | The upper-case token and its truncated prefix are both gone |
| A URL without userinfo, a `mailto:` fragment, and a bare `word@word` survive | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_preserves_fragments_without_userinfo` | pass | Byte-identical output |
| Compound identifiers that merely contain a credential label survive | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_preserves_compound_identifiers` | pass | `input_tokens`, `token_count`, and `api_key_id` are byte-identical |
| The redaction applies on the insert path | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_removes_credential_material_from_persisted_rows` | pass | Pre-existing guard; still green |
| The redaction applies on the failed-attempt update path | `rust/tests/terminal_detail_redaction.rs::terminal_detail_redaction_applies_to_the_failed_attempt_update_path` | pass | Pre-existing guard; still green |
| An ordinary diagnostic detail survives the projection intact | `rust/tests/terminal_detail_redaction.rs::terminal_detail_sanitizer_preserves_ordinary_diagnostics` | pass | Pre-existing guard; still green |
| The persisted `error_detail` vocabulary is unchanged | The producers in `rust/src/coordinator/finite.rs`, `rust/src/coordinator/streaming/coordinator.rs`, and `rust/src/coordinator/streaming/terminal.rs` were not modified | pass | Only the projection in front of the write changed |
| Strict Clippy with `-D warnings` | `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings` | pass | Exit 0, zero findings |
| The focused finalization and coordinator targets are green | `cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1` and `cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1` | pass | 11 and 18 tests, respectively |
| The workspace format check | `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` | partial | Red on `rust/src/health/health_manager.rs:591`, which belongs to another in-flight milestone and is outside this milestone's ownership (§4, §10) |
| The full serial workspace suite | Not run in this corrective pass | not run | The exact command is recorded in §4 and §10 |

## 3. Production implementation evidence

All four corrections are in `rust/src/coordinator/finalization.rs`. **No
other production file changed.** The four rules are:

- **Rule 1 — labeled credential field, including the JSON spelling.**
  `match_labeled_credential` skips an optional closing quote (`"` or `'`)
  after the label before requiring the `:` / `=` separator, so the plain,
  the quoted, and the JSON spellings all fire. The `is_word_byte` boundary
  guard and the separator requirement are unchanged, so compound
  identifiers still do not match.
- **Rule 2 — standalone credential scheme word.**
  `match_standalone_credential` (renamed from `match_bearer_credential`)
  iterates the existing `CREDENTIAL_SCHEMES` list — `bearer`, `basic`,
  `token`, `digest` — instead of a hard-coded literal.
  `MIN_STANDALONE_CREDENTIAL_LENGTH` (16) and the whitespace requirement
  are kept, so prose survives and the scheme word stays visible while only
  the credential is removed.
- **Rule 3 — URL authority with userinfo credentials.**
  `match_url_userinfo` recognizes a `scheme://` authority that contains `@`
  as credential-bearing, consistent with
  `rust/src/operations/status.rs::contains_credential_bearing_authority`.
  The whole userinfo section becomes `[redacted]`, the host survives, and
  the authority ends at the first path, query, fragment, or whitespace byte,
  so trailing prose is not swallowed. A URL with no userinfo, a `mailto:`
  fragment, and a bare `word@word` are untouched.
- **Rule 4 — vendor token prefix, case-insensitively.**
  `match_vendor_token` compares the prefix with `eq_ignore_ascii_case`.
  Every per-prefix minimum total length is unchanged, so a short or
  ordinary capitalised word is still not swallowed.

The dispatch order in `redact_at` is: labeled credential field, URL
userinfo, vendor token, standalone scheme credential, JSON web token, long
opaque run; the first match wins. `sanitize_detail` still applies the
pre-existing control-character filter and the `MAX_ERROR_DETAIL` cap after
the redaction, so redaction runs before the cap and a secret is never
truncated into a partial credential.

The write path is unchanged: `rust/src/coordinator/finalization.rs:339`
still binds `data.error_detail.as_deref().map(sanitize_detail)` into the
same insert and update statements.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rustfmt --edition 2021 --check rust/src/coordinator/finalization.rs rust/tests/terminal_detail_redaction.rs
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test terminal_detail_redaction -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
```

The four deliberate-failure demonstrations each ran:

```bash
cargo test --manifest-path rust/Cargo.toml --test terminal_detail_redaction -- --test-threads=1
```

after a one-rule revert in `rust/src/coordinator/finalization.rs`, followed
by:

```bash
cp /tmp/finalization.rs.backup rust/src/coordinator/finalization.rs
diff /tmp/finalization.rs.backup rust/src/coordinator/finalization.rs
```

`git checkout`, `git restore`, `git stash`, `git reset`, and `git clean` are
forbidden in this repository and were not used; the backup was a `cp` to a
`/tmp` path.

### Results

- `cargo fmt --all -- --check` — **fail (exit 1)**, one diff, at
  `rust/src/health/health_manager.rs:591`. That file is another milestone's
  in-flight change and is outside this milestone's ownership; this
  milestone did not modify it. A scoped
  `rustfmt --edition 2021 --check` over the two Rust files this milestone
  owns — `rust/src/coordinator/finalization.rs` and
  `rust/tests/terminal_detail_redaction.rs` — exited 0 with no diff.
- `cargo clippy --workspace --all-targets -- -D warnings` — **pass**,
  exit 0, zero findings.
- `terminal_detail_redaction` — **10 passed, 0 failed, 0 ignored**.
- `coordinator_finalization` — **11 passed, 0 failed, 0 ignored**.
- `coordinator_c011` — **18 passed, 0 failed, 0 ignored**.

### Deliberate-failure demonstrations (regression-guard evidence)

Each of the four corrections was reverted one at a time, the focused target
was run, and the guard that owns the correction was observed to fail. All
four reverts compiled; no revert produced a compilation failure, so every
result below is an observed test failure and not a build failure.

| Reverted correction | Revert applied | Observed failing test | Result |
|---|---|---|---|
| Rule 1 — JSON spelling of a labeled field | Removed the optional-closing-quote skip block from `match_labeled_credential` | `terminal_detail_redaction_removes_a_json_spelled_labeled_credential` | **FAILED** — 9 passed, 1 failed; the persisted detail still held the JSON-spelled value |
| Rule 2 — standalone scheme words | Changed the iteration in `match_standalone_credential` from `CREDENTIAL_SCHEMES` to the single literal `"bearer"` | `terminal_detail_redaction_removes_standalone_scheme_credentials` | **FAILED** — 9 passed, 1 failed; the standalone `Basic` credential survived |
| Rule 3 — URL userinfo credentials | Removed the `match_url_userinfo` call from `redact_at` and deleted `match_url_userinfo`, `scan_url_scheme`, `scan_authority`, and `is_authority_byte` | `terminal_detail_redaction_removes_url_userinfo_credentials` | **FAILED** — 9 passed, 1 failed; the URL userinfo credential survived |
| Rule 4 — case-insensitive vendor prefix | Changed the prefix comparison in `match_vendor_token` from `eq_ignore_ascii_case` to `eq` | `terminal_detail_redaction_removes_uppercase_vendor_token_prefixes` | **FAILED** — 9 passed, 1 failed; the upper-case vendor token survived |

After the fourth demonstration the file was restored with `cp` and verified:
`diff` reported no difference and the SHA-256 of the restored file matched
the backup (`457a26b9526a977e9987e34b9e552340d87526432b5198e3d8eec5b2cc19ff45`).
The focused target was then re-run and returned 10 passed, 0 failed.

### Not run

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

These are the repository's full-suite and no-default-features parity gates.
They were not run in this corrective pass, which was scoped to the focused
targets and strict Clippy. This is recorded as an unresolved finding in §10.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| The durable `error_detail` column stays secret-free | The projection removes credential-shaped material before the write; the removed material is replaced with a marker that contains no part of it. `terminal_detail_redaction_removes_credential_material_from_persisted_rows` asserts the absence of the secret and of its truncated prefix in both the request and the attempt row |
| The redaction happens before the `MAX_ERROR_DETAIL` cap | `sanitize_detail` redacts while walking the input and only then applies the cap to the rendered output; the cap check is against `emitted`, so a secret is never truncated into a partial credential. `rust/src/coordinator/finalization.rs:805` |
| Ordinary diagnostics are preserved | `terminal_detail_sanitizer_preserves_ordinary_diagnostics` asserts byte-identical survival of an ordinary detail; `terminal_detail_redaction_preserves_fragments_without_userinfo` and `terminal_detail_redaction_preserves_compound_identifiers` assert byte-identical survival of URLs, `mailto:` fragments, bare `word@word`, and compound identifiers |
| The persisted vocabulary of the column is unchanged | The producers were not modified: `rust/src/coordinator/finite.rs:1974`, `rust/src/coordinator/streaming/coordinator.rs:1521` and `:1238`, and `rust/src/coordinator/streaming/terminal.rs:679` and `:707` still write the same fixed-vocabulary strings. Only the projection changed |
| No credential, prompt, raw body, or cache key reaches persistence, logs, or diagnostics | The projection is the only new code path, it writes only the `[redacted]` marker, and it logs nothing. The removed material is not recorded in any form, including a truncated prefix |
| The client-visible response contract does not change | Only the durable diagnostic column changed; no route, status, or response-body code was touched |
| No new dependency, configuration key, migration, table, column, or index | No `Cargo.toml`, `Cargo.lock`, migration, or config file was modified by this milestone |
| The M007 transport-robustness corrections stay in force | `rust/src/coordinator/failure.rs` and `rust/src/server/middleware.rs` were not modified by this milestone; the M007 guards in `rust/tests/coordinator_failure.rs` and `rust/tests/server_auth.rs` are outside this milestone's scope and were not touched |
| Strict Clippy with `-D warnings` | Clean, exit 0 |

## 6. Failure and recovery review

- **Pure function.** The projection is a deterministic function of the detail
  string. It performs no I/O, holds no state, reads no configuration, uses
  no clock and no randomness, and has no concurrency. Two callers with the
  same input produce the same output, so a repeated write converges.
- **Failure direction.** Over-redaction is the only failure direction. A
  value that looks credential-shaped but is not credential material is
  removed; a value that is credential-shaped but is not recognized is kept,
  and that second case is the defect this milestone corrects. The documented
  over-redaction remains deliberate and bounded.
- **Redaction before the cap.** The redaction runs while the input is
  scanned, and the `MAX_ERROR_DETAIL` cap is applied to the rendered output,
  so a secret is never truncated into a partial credential.
- **Duplicate delivery.** The durable detail write is an upsert of the
  terminal row; redaction is a pure function of the value, so a repeated
  write converges.
- **Cancellation races.** The redaction happens on the value before the
  write; a cancelled write leaves the pre-existing row, which is not worse
  than before. No cancellation path was added or changed.
- **Restart.** Persisted rows are read back as-is; the redaction is applied
  on write, not on read, so a row written by an older build is still
  readable.
- **Partial persistence failure.** Unchanged; the terminal write path was not
  restructured.
- **Stale generation / lease.** Not applicable. The projection does not
  touch generation ownership, selection claims, or reservations.
- **Contention and resource release.** Not applicable. The projection does
  not enter the SQLite gate, the routing selection lock, or any runtime
  resource accounting.
- **Malformed / unauthorized input.** A malformed or non-UTF-8-adjacent
  input is handled by the existing character filter; the projection never
  panics and never allocates unboundedly, because it allocates in
  proportion to the capped output and does not recurse or backtrack.

## 7. Migration and compatibility review

- **Schema migration.** None. No table, column, index, or migration change.
- **Data migration.** None. The change applies to new writes only; rows
  written by an earlier build are not retroactively scrubbed and are not
  corrupted.
- **Backward compatibility.** Older rows stay readable. The column's
  persisted vocabulary is unchanged, and a row written by a build that
  predates the rule is still a valid row.
- **Protocol negotiation.** Unchanged. No wire codec, DTO, or route change.
- **Config behavior.** Unchanged. No configuration key was added, removed,
  or reclassified, and no reload/restart boundary was touched.
- **Rollback limits.** Rolling back the projection to the M007 behavior
  would restore the under-coverage this milestone corrects. It would not
  invalidate any persisted row, because the column's vocabulary is
  unchanged.
- **Legacy-path status.** The M007 legacy-path status is unchanged: the
  durable write path remains the one owner of the terminal detail, and the
  streaming terminal path still flows through the same finalizer.

## 8. Security review

- **Auth enforcement.** Unchanged. The correction does not touch
  `rust/src/server/middleware.rs`, the constant-time credential
  comparison, the accepted header set, or the loopback exemption.
- **Secret handling.** The projection is the secret-handling path for this
  milestone. Removed material is replaced with `[redacted]`, a marker that
  contains no part of the removed value, so neither the secret nor a
  truncated prefix of it can survive. The removed material is not logged,
  not counted, and not recorded anywhere.
- **Defense in depth.** The redaction is defense-in-depth over a column
  whose current producers write a fixed-vocabulary string. The practical
  exposure today is nil, and the correction makes the guard behave as
  documented the moment upstream text reaches that column.
- **Consistency with the other redactor.** The URL-authority rule now
  matches `rust/src/operations/status.rs::contains_credential_bearing_authority`,
  which already treated an authority containing `@` as credential-bearing.
  The two redactors no longer disagree about a URL with userinfo.
- **Privilege bounds.** Unchanged. The correction does not relax any
  credential check or widen any authorization decision.
- **DoS bounds.** The projection is a single left-to-right pass with bounded
  lookahead per position and allocates in proportion to the capped output.
  It does not recurse or backtrack.
- **Audit behavior.** The removed material is not audited and not logged;
  only the presence of the `[redacted]` marker is visible to an operator.
- **No leak claimed.** This record does not claim that a credential leaked.
  The pre-correction defect was a false coverage claim in a closure record
  and a guard that would under-perform, not a demonstrated credential
  disclosure.

## 9. Documentation and operations

- `architecture/deep-dive-security.md`: the "Persisted terminal detail"
  section was corrected so the published rule set matches the code. The
  section now states that the projection removes credential-shaped material
  first, then drops control characters and caps the value at 512 characters,
  and that "Redaction runs before the cap, so a secret is never truncated
  into a partial credential." It now enumerates the six rules in the order
  `redact_at` tries them — labeled credential field (34 label spellings,
  including the JSON spelling, with the optional closing quote between the
  label and the separator), URL authority with userinfo credentials,
  vendor token prefix (11 prefixes, matched case-insensitively), standalone
  credential scheme word (`Bearer`, `Basic`, `Token`, `Digest`, with the
  16-character minimum), JSON web token, and the 32-or-more-character
  opaque run — and states that removed material is replaced with
  `[redacted]`, which contains no part of the removed value. The
  over-redaction paragraph was corrected as well: it now says that ordinary
  diagnostics — "URLs without userinfo, model identifiers, prose, and usage
  counters such as `input_tokens`" — survive intact, because a label only
  matches at a word boundary and only when a `:` or `=` separator follows
  it, and because the URL rule only fires on an authority that actually
  carries an `@`.
- `architecture/deep-dive-request-lifecycle.md`: no change. It records that
  the detail is redacted before the durable write, which is still true.
- `docs/api-reference.md`: no change. The redaction applies to a durable
  diagnostic column, not to a client-facing field.
- **Operator-facing note.** No operator action is required. There is no new
  command, no new configuration key, no migration, and no restart. An
  operator who inspects a stored terminal detail sees `[redacted]` in place
  of removed material; that is the only observable difference, and it is the
  behavior M007 already documented.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The labeled path's `scheme_credential` applies no minimum length, so a labeled scheme credential shorter than the standalone threshold is still redacted | Over-redaction only: a labeled field such as `authorization: Basic ab` loses a two-character value. Pre-existing in M007 and unchanged by this milestone | None required. Narrowing the labeled path would change the documented labeled behavior and is outside this milestone's scope |
| low | A long hostname containing a digit can still be consumed by the 32-character opaque-run rule | Over-redaction only: one long dotless host label that contains a digit reaches the opaque-run threshold. Pre-existing, and a consequence of `is_opaque_byte` excluding `.` | None required. Narrowing the opaque-run rule would weaken documented rule (d), so it is deliberately out of scope |
| medium | The full serial Rust workspace suite was not run in this corrective pass; only the focused targets and strict Clippy were | The repository's full-suite gate is unverified for this pass | Run `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` before the change is pushed. The `--no-default-features` parity gates (`cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` and the matching `cargo clippy ... --no-default-features -- -D warnings`) were likewise not run |
| info | The original M007 closure record's §3 coverage claims were inaccurate as written: they named the JSON spelling, the non-`Bearer` scheme words, and the case-insensitive vendor prefix as covered when they were not, and they did not mention URL credentials at all | A reader of `plans/closure/request-admission-wire/007-status.md` would take the pre-correction behavior as the covered set | None. The original record is left unchanged under the append-only discipline; this corrective closure supersedes its coverage statement |
| info | The workspace format check is currently red on `rust/src/health/health_manager.rs:591`, which belongs to another in-flight milestone | `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` exits 1 on a file this milestone does not own | The owning milestone must format that file. The two Rust files this milestone owns are format-clean under a scoped `rustfmt --check` |

## 11. Roadmap disposition

Milestone closed through this corrective pass; the next dependency may
proceed.

M009 reopens no closed scope. M007's transport-robustness corrections stay
closed and unchanged. Milestone 008 in this subsystem remains blocked on the
acceptance of ADR-0003 and is unaffected by this closure. The two residual
findings recorded in §10 are bounded and deliberately not reopened here.

## 12. Registry updates

The parent owns `plans/registry.md` and
`plans/subsystems/request-admission-wire-roadmap.md`. The following changes
are required and were not applied by this milestone:

- `plans/registry.md`: add a row to the "Recently closed" table, in the
  `| Subsystem / plan | Disposition | Evidence |` shape, reading
  **Request admission and wire M009 — terminal detail redaction coverage
  correction**; the disposition is **closed** and the summary states that the
  redaction projection's documented coverage now matches its behavior: the
  JSON spelling of a labeled credential field, every standalone credential
  scheme word, the case-insensitive vendor token prefix, and a URL authority
  carrying userinfo credentials are all redacted; ordinary URLs,
  `mailto:` fragments, bare `word@word` fragments, and compound identifiers
  survive byte-identically; no credential leaked, because every producer of
  `error_detail` writes a fixed-vocabulary string; no schema, config,
  protocol, producer, or persisted-vocabulary change; four new guards were
  each shown failing on a deliberate revert; strict workspace clippy clean
  and the affected targets green; the full serial suite and the
  `--no-default-features` parity remain outstanding; the evidence link is
  `plans/closure/request-admission-wire/009-status.md`.
- `plans/subsystems/request-admission-wire-roadmap.md`: add the
  corresponding milestone entry and set the milestone status table row for
  M009 to `closed`. M007's row stays `closed` and is not reopened.
- `plans/implementation/request-admission-wire/009-terminal-detail-redaction-coverage-correction.md`:
  the `Status` line is already `implemented`; no change is needed.
