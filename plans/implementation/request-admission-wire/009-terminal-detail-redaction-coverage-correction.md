# Request Admission and Wire Milestone 009 — Terminal detail redaction coverage correction

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-007--terminal-detail-scrubbing-and-transport-robustness-corrections`

Original plan and closure (corrective pass against):

- `plans/implementation/request-admission-wire/007-terminal-detail-scrubbing-and-transport-robustness.md`
- `plans/closure/request-admission-wire/007-status.md`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for admission errors and downstream observations
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. Every change is internal to EggPool: no public contract
  change, no new protocol, no dependency change, and no durable schema
  change.
- Stop for architecture review if secret redaction requires a new durable
  contract or a new operator-visible error field.

Primary class: invariant

## 1. Objective

Make the redaction projection's documented coverage match its actual
behavior: close the four gaps that the M007 closure record claimed were
already closed, and make every coverage claim in the closure record's §3
true.

The four gaps, all in `rust/src/coordinator/finalization.rs`:

1. the JSON spelling of a labeled credential field (`"api_key":"…"`) was not
   redacted;
2. only a standalone `Bearer` credential was redacted, so a standalone
   `Basic`, `Token`, or `Digest` credential passed through unredacted;
3. vendor token prefix matching was byte-exact, so `SK-proj-…` was missed
   while `sk-proj-…` was caught;
4. a URL authority carrying userinfo credentials
   (`https://user:secret@host`) was not redacted at all.

The M007 transport-robustness work — the retry-after clock-field bounds in
`rust/src/coordinator/failure.rs` and the case-insensitive authorization
scheme token in `rust/src/server/middleware.rs` — is correct and is not
touched by this milestone.

**Severity context, stated honestly.** At the time of the review every
producer of `error_detail` wrote a fixed-vocabulary string, so the practical
exposure was nil: `rust/src/coordinator/finite.rs:1974`,
`rust/src/coordinator/streaming/coordinator.rs:1521`, and
`rust/src/coordinator/streaming/terminal.rs:679` all write
`effects.client_outcome.clone()`;
`rust/src/coordinator/streaming/terminal.rs:707` writes a literal; and
`rust/src/coordinator/streaming/coordinator.rs:1238` writes an
`outcome: &'static str`. No credential leaked. The defect was that the
M007 closure's coverage claim was false and that the guard would
under-perform the moment upstream text reached that column. This milestone
closes the coverage gap; it does not claim a past leak and does not claim to
have remediated one.

## 2. Why this milestone is ready

No dependencies. This is a corrective pass over a closed milestone, not new
work: the redaction projection, its write path, and its test target all
exist, and every gap is a local defect in one function set with a
deterministic failing case.

It introduces no new dependency, no schema change, no configuration key, no
protocol change, and no new durable contract. The stable interface it relies
on — the `error_detail` column and the `sanitize_detail` write-path seam —
is unchanged by this milestone.

## 3. Current implementation evidence

### The write path and the projection

`rust/src/coordinator/finalization.rs:339` binds the durable value:

```rust
let detail = data.error_detail.as_deref().map(sanitize_detail);
```

`sanitize_detail` (`rust/src/coordinator/finalization.rs:805`) is a single
left-to-right pass that drops control characters other than newline and tab,
caps the result at `MAX_ERROR_DETAIL` (512), and — as of M007 — redacts
credential-shaped material. Redaction runs before the cap, so a secret is
never truncated into a partial credential.

At the start of this corrective pass the rule set was dispatched by
`redact_at` (`rust/src/coordinator/finalization.rs:905`) over four classes,
and the M007 closure record's §3 described those four classes as complete.
They were not.

### Defect 1 — the JSON spelling of a labeled field was not redacted

`match_labeled_credential` (`rust/src/coordinator/finalization.rs:916`)
required the byte immediately after the label, post-whitespace, to be `:` or
`=`. In the JSON spelling `"api_key":"<value>"` that byte is the label's
closing quote, so the separator requirement was never met. The quote skip
ran only *after* a separator had already been accepted, so it could not
help. The unquoted form `api_key: "<value>"` was redacted; the JSON form was
not. The defect was in the ordering of the quote skip relative to the
separator requirement, not in the label list.

### Defect 2 — only a standalone `bearer` was redacted

`match_bearer_credential` hard-coded the literal `bearer`. The helper
`scheme_credential` (`rust/src/coordinator/finalization.rs:1101`), which
knows the full `CREDENTIAL_SCHEMES` list (`bearer`, `basic`, `token`,
`digest`), was reachable only from the labeled path. A standalone
`Basic …`, `Token …`, or `Digest …` therefore passed through unredacted,
even though the M007 closure record's §3 named all four scheme words as
covered.

### Defect 3 — vendor token prefix matching was byte-exact

`match_vendor_token` (`rust/src/coordinator/finalization.rs:968`) compared
the prefix with an exact byte comparison, so `SK-proj-…` did not match the
`sk-` prefix while `sk-proj-…` did. This was inconsistent with the label
rule, which has always matched case-insensitively, and it made the coverage
of the vendor-prefix class depend on the case of the token.

### Defect 4 — no URL-authority rule existed

There was no URL rule of any kind. The repository's other redactor,
`contains_credential_bearing_authority` in `rust/src/operations/status.rs:1215`
(called from `contains_secret_material` at
`rust/src/operations/status.rs:1082`), already treats an authority that
contains `@` as credential-bearing. The terminal-detail projection did not,
so a URL with userinfo credentials reached the durable column intact.

### Why the M007 verification missed all four

M007's closure was written against the rule's *description* rather than
against the rule's *behavior*, and its requirement-to-evidence matrix
asserted coverage at the class level without a per-class failing-case
fixture. The three guards that existed exercised the shapes the
implementation happened to handle — an unquoted label, a `Bearer` scheme
word, and a lower-case vendor prefix — so the guards could not distinguish
"the class is covered" from "this spelling is covered". No test enumerated
the boundaries of a class: the JSON spelling of a label, a non-`Bearer`
scheme word, a case variant of a vendor prefix, and a URL authority were
never fixtures.

## 4. Invariants that must not regress

- The durable `error_detail` column stays secret-free. A credential-shaped
  value written through the terminal detail path does not appear in the
  persisted row, in any form, including a truncated prefix.
- The redaction happens **before** the `MAX_ERROR_DETAIL` cap, so a secret is
  never truncated into a partial credential.
- Ordinary diagnostics are preserved. Prose, model identifiers, URLs without
  userinfo, `mailto:` fragments, bare `word@word` fragments, and compound
  identifiers such as `input_tokens`, `token_count`, and `api_key_id` survive
  byte-identically.
- The persisted vocabulary of the `error_detail` column is unchanged. This
  milestone changes only the projection, not the producers, so the column
  still holds the same fixed-vocabulary strings it held before.
- No credential, prompt, raw body, or cache key reaches persistence, logs,
  or diagnostics. The removed material is not recorded anywhere.
- The client-visible response contract does not change. The redaction applies
  to a durable diagnostic column only.
- No new dependency, configuration key, migration, table, column, or index.
- The M007 transport-robustness corrections stay in force: the retry-after
  clock-field bounds in `rust/src/coordinator/failure.rs` and the
  case-insensitive authorization scheme token in
  `rust/src/server/middleware.rs`, with the constant-time credential
  comparison and the loopback exemption unchanged.

## 5. Scope

### In scope

- Gap 1 — the JSON spelling of a labeled credential field in
  `match_labeled_credential`.
- Gap 2 — the standalone credential scheme words in the standalone-scheme
  matcher.
- Gap 3 — the case-insensitive comparison of a vendor token prefix in
  `match_vendor_token`.
- Gap 4 — the URL-authority userinfo rule, consistent with
  `rust/src/operations/status.rs`.
- The regression guards that prove each gap is closed, in
  `rust/tests/terminal_detail_redaction.rs`.
- The correction of the rule description in
  `architecture/deep-dive-security.md`.

### Explicitly out of scope

- Changing the `error_detail` producers. The column's vocabulary, the
  producer set, and the fixed-vocabulary contract are unchanged.
- Changing the durable schema, the migration chain, the write path, the
  `MAX_ERROR_DETAIL` cap, or the control-character filter.
- The transport-robustness work M007 already delivered correctly: the
  retry-after clock-field bounds and the case-insensitive authorization
  scheme token. These are not reopened.
- The two residual findings recorded in the closure record's §10: the
  labeled path's `scheme_credential` applies no minimum length, and a long
  hostname containing a digit can still be consumed by the 32-character
  opaque-run rule. Narrowing either would change documented behavior that
  this milestone does not reopen.
- Retroactive scrubbing of durable rows written before the rule existed.
- The status projection's own redactor in `rust/src/operations/status.rs`.
  It is a reference for the URL-authority rule, not a target of this change.

## 6. Required production changes

All four rules live in `rust/src/coordinator/finalization.rs`, in the
`sanitize_detail` projection and its matchers. No other production file
changes.

**Rule 1 — labeled credential field, including the JSON spelling.**
`match_labeled_credential` must skip an optional closing quote (`"` or `'`)
after the label *before* the `:` / `=` separator is required, so the plain,
the quoted, and the JSON spellings all fire. The `is_word_byte` boundary
guard is unchanged and the separator requirement is unchanged, so compound
identifiers (`input_tokens`, `token_count`, `api_key_id`) still do not
match.

**Rule 2 — standalone credential scheme word.**
The standalone matcher must iterate the existing `CREDENTIAL_SCHEMES` list
(`bearer`, `basic`, `token`, `digest`) instead of a hard-coded literal, and
must be renamed accordingly. `MIN_STANDALONE_CREDENTIAL_LENGTH` (16) and the
whitespace requirement are kept, so prose survives and the scheme word stays
visible while only the credential is removed.

**Rule 3 — URL authority with userinfo credentials.**
A new matcher must treat a `scheme://` authority that contains `@` as
credential-bearing, consistent with
`rust/src/operations/status.rs::contains_credential_bearing_authority`. The
whole userinfo section becomes `[redacted]`; the host survives. The
authority ends at the first path, query, fragment, or whitespace byte, so
trailing prose is not swallowed. A URL with no userinfo, a `mailto:`
fragment, and a bare `word@word` are untouched.

**Rule 4 — vendor token prefix, case-insensitively.**
The prefix comparison must use `eq_ignore_ascii_case`, like the field
label. The per-prefix minimum total length is unchanged, so a short or
ordinary capitalised word is still not swallowed.

The dispatch order in `redact_at` is: labeled credential field, URL
userinfo, vendor token, standalone scheme credential, JSON web token, long
opaque run. The first match wins.

## 7. Ordered work packages

### Work package A — JSON spelling of a labeled credential field

Intent:
Make the labeled rule cover the spelling that the JSON form of a credential
field actually uses.

Required changes:
In `match_labeled_credential`
(`rust/src/coordinator/finalization.rs`), skip an optional closing quote
after the label before requiring the `:` / `=` separator. Keep the word
boundary guard and the separator requirement as they are.

Acceptance evidence:
`terminal_detail_redaction_removes_a_json_spelled_labeled_credential` in
`rust/tests/terminal_detail_redaction.rs` — asserts the exact redacted
output, the absence of the removed value, and the presence of the
`[redacted]` marker.

### Work package B — every standalone credential scheme word

Intent:
Make the standalone scheme rule cover every scheme word the labeled path
already knew about.

Required changes:
Replace the hard-coded `bearer` literal with an iteration over
`CREDENTIAL_SCHEMES`, and rename the matcher from `match_bearer_credential`
to `match_standalone_credential`. Keep `MIN_STANDALONE_CREDENTIAL_LENGTH`
and the whitespace requirement.

Acceptance evidence:
`terminal_detail_redaction_removes_standalone_scheme_credentials` in
`rust/tests/terminal_detail_redaction.rs` — asserts that a standalone
`Basic`, `Token`, and `Digest` credential are all removed while the scheme
words survive.
`terminal_detail_redaction_keeps_the_label_and_scheme_of_a_labeled_credential`
— asserts that the labeled path keeps the label, the separator, and the
scheme word and loses only the credential.

### Work package C — URL authority with userinfo credentials

Intent:
Make the terminal-detail projection agree with the repository's other
redactor about a credential-bearing URL authority.

Required changes:
Add a URL-userinfo matcher to `rust/src/coordinator/finalization.rs` that
recognizes a `scheme://` authority containing `@`, replaces the userinfo
section with `[redacted]`, keeps the host, and ends the authority at the
first path, query, fragment, or whitespace byte. Wire it into the dispatch
order in `redact_at`.

Acceptance evidence:
`terminal_detail_redaction_removes_url_userinfo_credentials` in
`rust/tests/terminal_detail_redaction.rs` — asserts that the userinfo
credential and the userinfo section are gone, that the host survives, and
that the surrounding prose survives.
`terminal_detail_redaction_preserves_fragments_without_userinfo` — asserts
that an ordinary URL, a `mailto:` fragment, and a bare `word@word` survive
byte-identically.

### Work package D — case-insensitive vendor token prefix

Intent:
Make the vendor-prefix rule case-insensitive, like the label rule.

Required changes:
In `match_vendor_token` (`rust/src/coordinator/finalization.rs`), compare
the prefix with `eq_ignore_ascii_case`. Keep every per-prefix minimum
total length unchanged.

Acceptance evidence:
`terminal_detail_redaction_removes_uppercase_vendor_token_prefixes` in
`rust/tests/terminal_detail_redaction.rs` — asserts that an upper-case
vendor token and its truncated prefix are both gone and that the
`[redacted]` marker is present.

### Work package E — documentation and regression guards

Intent:
Make the published rule description match the code, and make each of the
four corrections a regression guard rather than a one-off observation.

Required changes:
Update the "Persisted terminal detail" section of
`architecture/deep-dive-security.md` so the label spellings (including the
JSON spelling), the scheme word list, the vendor prefix list, the
URL-userinfo rule, the opaque-run rule, and the order the rules are tried in
are all described as implemented.

Acceptance evidence:
The seven guards in `rust/tests/terminal_detail_redaction.rs` listed in
§10, four of which are demonstrated to fail against the pre-correction
behavior in the closure record's §4.

## 8. Failure, cancellation, restart, contention semantics

The projection is a pure, deterministic function of the detail string. It
has no I/O, no state, no configuration, no clock, no randomness, and no
concurrency. Two callers with the same input produce the same output, and
the function is re-entrant by construction because it holds no shared
mutable state.

- **Failure direction.** Over-redaction is the only failure direction. A
  value that looks credential-shaped but is not is removed; a value that is
  credential-shaped but is not recognized is kept. The second case is the
  defect this milestone corrects.
- **Allocation and cost.** The projection is a single left-to-right pass
  with bounded lookahead per position. It allocates in proportion to the
  capped output and does not recurse or backtrack.
- **Cancellation.** No cancellation path is added or changed. The
  projection runs synchronously on the value before the durable write, so a
  cancelled write leaves the pre-existing row, which is not worse than
  before.
- **Restart.** Persisted rows are read back as-is; the projection is
  applied on write, not on read, so a row written by an older build stays
  readable.
- **Contention.** None. The projection does not touch the SQLite gate, the
  routing selection lock, or any runtime resource accounting.
- **Partial failure.** Unchanged. The terminal write path was not
  restructured; the redaction is a value transform ahead of the same
  statement.

## 9. Compatibility and migration

- No schema migration, no table, column, or index change.
- No configuration key is added, removed, or reclassified.
- No protocol or wire change. No client-visible HTTP status code, route, or
  response body shape changes.
- No public Rust API break.
- The persisted column's vocabulary is unchanged: the producers still write
  the same fixed-vocabulary strings, and only the projection in front of the
  write changed.
- Older rows stay readable. Rows written before the rule existed are not
  retroactively scrubbed and are not corrupted by this change.

## 10. Required tests

All in `rust/tests/terminal_detail_redaction.rs`, run with `--test-threads=1`:

- `terminal_detail_redaction_removes_a_json_spelled_labeled_credential` —
  work package A; exact redacted output, absence of the removed value,
  presence of the marker.
- `terminal_detail_redaction_removes_standalone_scheme_credentials` —
  work package B; every standalone scheme word.
- `terminal_detail_redaction_keeps_the_label_and_scheme_of_a_labeled_credential`
  — work package B; the label and the scheme word survive.
- `terminal_detail_redaction_removes_url_userinfo_credentials` — work
  package C; the userinfo is gone and the host survives.
- `terminal_detail_redaction_removes_uppercase_vendor_token_prefixes` —
  work package D; the upper-case prefix and its truncated prefix are gone.
- `terminal_detail_redaction_preserves_fragments_without_userinfo` —
  work package C; ordinary URL, `mailto:` fragment, and bare `word@word`
  survive byte-identically.
- `terminal_detail_redaction_preserves_compound_identifiers` — work
  packages A and B; `input_tokens`, `token_count`, and `api_key_id` survive
  byte-identically.

Pre-existing guards that must stay green:

- `terminal_detail_redaction_removes_credential_material_from_persisted_rows`
- `terminal_detail_redaction_applies_to_the_failed_attempt_update_path`
- `terminal_detail_sanitizer_preserves_ordinary_diagnostics`

Focused non-regression targets:

- `cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1`
- `cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1`

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test terminal_detail_redaction -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
```

Plus the deliberate-failure demonstration for each of the four corrections:
revert one rule in `rust/src/coordinator/finalization.rs`, show that its
guard fails, restore the file, and verify the restore with `diff`. The
repository forbids `git checkout`, `git restore`, `git stash`, `git reset`,
and `git clean`, so the backup is a `cp` to a `/tmp` path.

The full serial workspace suite is a closure gate for the repository, not
for this milestone; it was not run in this corrective pass and is recorded
as an unresolved finding in the closure record's §10.

## 12. Documentation updates

- `architecture/deep-dive-security.md` — the "Persisted terminal detail"
  section must describe the corrected rule set as implemented: the label
  spellings including the JSON spelling, the scheme word list, the vendor
  prefix list and its case-insensitive comparison, the URL-userinfo rule and
  its consistency with `rust/src/operations/status.rs`, the opaque-run rule,
  and the order the rules are tried in. Any statement in the file that the
  correction makes wrong is corrected rather than left standing.
- `architecture/deep-dive-request-lifecycle.md` — no change expected. It
  records that the detail is redacted before the durable write, which is
  still true; confirm only.
- `docs/api-reference.md` — no change expected. The redaction applies to a
  durable diagnostic column, not to a client-facing field.

## 13. Acceptance criteria

- A terminal detail that carries a credential-shaped value in the JSON
  spelling of a labeled field (`"api_key":"…"`) is persisted without that
  value.
- A terminal detail that carries a standalone `Basic`, `Token`, or `Digest`
  credential is persisted without that credential, and the scheme word
  remains visible.
- A terminal detail that carries a vendor token with an upper-case prefix is
  persisted without the token.
- A terminal detail that carries a URL with userinfo credentials is
  persisted without the userinfo, and the host remains visible.
- An ordinary URL, a `mailto:` fragment, a bare `word@word` fragment, a
  compound identifier, and an ordinary diagnostic prose detail are persisted
  byte-identically to the written value.
- A terminal detail that carries credential-shaped material does not persist
  that material in any form, including a truncated prefix, on both the insert
  and the failed-attempt update path.
- The durable `error_detail` column's persisted vocabulary is unchanged.
- No client-visible HTTP contract, routing-decision schema, or config
  classification changes.
- Each of the four corrections is demonstrated to fail against the
  pre-correction behavior and to pass after the correction.

## 14. Stop conditions

Stop and report rather than improvise when:

- The redaction cannot be made deterministic without a new durable contract
  or a new operator-visible field.
- The redaction would redact a large fraction of ordinary diagnostic detail,
  which would make the column useless.
- A correction would require changing the `error_detail` producers, the
  durable schema, or the write path, because those are out of scope.
- A correction would require reopening the M007 transport-robustness work,
  which was delivered correctly.
- A correction would require narrowing the documented opaque-run rule or
  adding a minimum length to the labeled scheme path, because both are
  recorded residual findings rather than scope.
- Evidence contradicts a security invariant stated in `AGENTS.md`.
- The scope expands into another subsystem, in particular the status
  projection's own redactor in `rust/src/operations/status.rs`.

## 15. Closure evidence required

- The implementation commits, or an explicit statement that the work is in
  the working tree and uncommitted.
- A requirement-to-evidence matrix with one row per corrected class, each
  citing the exact test name and its result.
- The exact verification commands run and their real outcomes, labeled
  honestly between local and CI runs, including any command that failed for
  a reason outside this milestone's ownership.
- The deliberate-failure demonstration for each of the four corrections,
  with the observed failing test names, and proof that the production file
  was restored.
- The production implementation evidence: the four rules with their file and
  function names, and a statement that no other production file changed.
- An invariant review covering each invariant in §4.
- A failure and recovery review covering the pure-function argument, the
  over-redaction direction, and the redaction-before-cap ordering.
- A migration and compatibility review.
- A security review covering secret-freedom, the defense-in-depth posture
  over a fixed-vocabulary column, and the consistency of the URL-authority
  rule with `rust/src/operations/status.rs`.
- The documentation update, with an explicit statement that no operator
  action is required.
- Every unresolved finding, severity-tagged, including the findings that
  this milestone deliberately did not close.

## 16. Handoff notes

Rust tests must run serially with `--test-threads=1`. Strict Clippy with
`-D warnings` is an invariant of this repository and no baseline allowlist
is acceptable.

No test may use a fixed millisecond sleep or a `yield_now()` count loop as
a readiness proof. These guards are synchronous and deterministic: they
write to a seeded temporary database and read the durable row back, so they
need no timing assumption at all.

The four corrections are independent. If one is in doubt, revert only that
rule, run the focused target, and observe which guard fails.

Preserve unrelated user changes in the working tree. This milestone owns
`rust/src/coordinator/finalization.rs` and
`rust/tests/terminal_detail_redaction.rs`; other files in the working tree
belong to other in-flight milestones and must not be touched, and the
repository-wide format check is currently red on one such file.

Do not commit from this milestone. The parent owns the registry and roadmap
updates.
