# W011 Closure — SSE EOF UTF-8 Finalization Correction

Status: closed

Implementation commit: [`35cdd04`](https://github.com/eggstack/eggpool/commit/35cdd04)

Plan: [W011 — SSE EOF UTF-8 finalization correction](../../implementation/canonical-wire/011-sse-eof-utf8-correction.md)

Baseline: `cf9e47eb` (`main` before the implementation commit)

Contract and oracle: the W008 `SseDecoder` boundary in
`rust/src/wire/stream.rs`, Python `src/eggpool/proxy/sse.py` and
`src/eggpool/proxy/sse_observer.py`, the committed bounded oracle at
`fixtures/canonical-wire/w011-sse-utf8-observations.json`, and the W001-W010
canonical-wire closure evidence.

## Outcome

W011 corrects Rust EOF finalization for retained incomplete UTF-8 sequences.
Normal `feed()` calls still retain a valid-but-incomplete suffix until more
bytes arrive. `finish()` now uses an explicit final decoder path: any retained
suffix is emitted as one U+FFFD replacement, counted, and passed through the
existing SSE line/frame state machine. No framing state machine, transport
ownership, retry, handoff, timeout, cancellation, or finalization behavior was
changed.

The Python migration harness now generates a compact, secret-safe W011
observation artifact. Rust consumes that artifact in the stream qualification
test and compares frame data, fields, comment classification, emitted frame
count, replacement count, EOF framing, and discarded-frame evidence across the
declared feed modes. Provider-data cases additionally retain Python observer
classification evidence, while Rust tests assert the existing typed malformed
provider-event outcome and absence of terminal success.

## Failing-before and passing-after evidence

The controlled baseline probe used the pre-fix `finish()` call against the new
Python-derived fixture:

```text
rtk cargo test --manifest-path rust/Cargo.toml --test wire_stream python_w011_utf8_observations_match_rust_for_all_feed_modes -- --test-threads=1
```

It failed at the first incomplete-prefix case. Rust emitted an empty `data:`
value while the Python oracle required `data: �`; the pre-fix replacement
count was therefore zero instead of one. After restoring the final flush and
committing `35cdd04`, the same regression passed as part of the 14-test
`wire_stream` suite.

## Differential fixture matrix

The artifact contains 16 bounded cases and 35 feed executions: whole-buffer
and one-byte feeding for every case, plus every two-chunk split point for the
three complete scalar cases. Every case emits exactly one frame and has zero
discarded/incomplete-frame evidence.

| Cases | Replacement count | Final frame/classification |
|---|---:|---|
| `valid_2_byte_scalar_lf`, `valid_3_byte_scalar_crlf`, `valid_4_byte_scalar_lf` | 0 | One frame containing exactly `é`, `世`, or `🌍`; complete scalar is emitted once under every split point and one-byte feed. |
| `eof_incomplete_2_prefix_1`, `eof_incomplete_3_prefix_1`, `eof_incomplete_3_prefix_2`, `eof_incomplete_4_prefix_1`, `eof_incomplete_4_prefix_2`, `eof_incomplete_4_prefix_3` | 1 | One final `data: �` frame; the suffix is not silently discarded. |
| `invalid_continuation_after_prefix` | 1 | One frame with `data: �A`, matching incremental `errors="replace"`. |
| `invalid_standalone_before_newline`, `invalid_standalone_before_eof` | 1 each | One frame with `data: �`; LF and EOF final-line framing remain valid. |
| `invalid_comment_before_newline`, `truncated_comment_at_eof` | 1 each | One comment-only frame retaining ` ignored �`; ignored/comment content does not become a provider event. |
| `invalid_data_line`, `truncated_data_line_after_json_prefix` | 1 each | One frame with `{"choices":[]}�`; Python records one parser error and no terminal evidence. Rust reports the existing typed `MalformedProviderEvent` during feed for the invalid byte and during `finish()` for the retained EOF suffix, under both whole and one-byte feeding. |

The Rust direct tests also cover replacement after an otherwise valid JSON
prefix, malformed-event typing, no false terminal evidence, LF/CRLF framing,
and all incomplete prefixes for representative 2-, 3-, and 4-byte scalars.
Existing oversized-carry coverage remains green and continues to enforce the
64 KiB typed framing bound.

## Requirement-to-evidence matrix

| W011 requirement | Evidence | Result |
|---|---|---|
| Valid split scalars remain pending and emit once when complete | `python_w011_utf8_observations_match_rust_for_all_feed_modes`; `completed_split_utf8_scalars_are_emitted_once_without_replacements` | Pass |
| Invalid feed bytes follow replacement semantics and count evidence | W011 Python artifact plus `invalid_utf8_replacements_preserve_sse_framing_and_count` | Pass |
| Incomplete EOF suffix is replaced and participates in final SSE frame | `eof_flushes_incomplete_utf8_as_one_replacement_in_the_final_frame`; all six incomplete-prefix artifact cases | Pass |
| Replacement-caused malformed provider event is typed and cannot complete | `replacement_in_final_provider_data_is_a_typed_malformed_event` under whole/one-byte feed and feed/EOF failure points | Pass |
| Valid EOF framing is unchanged | Existing `eof_without_terminal_is_not_success_and_final_unterminated_event_is_flushed` plus W011 valid-frame cases | Pass |
| 64 KiB carry bound and no unbounded stream body | Existing `oversized_carry_is_a_typed_bounded_failure`; Rust all-target regression suite | Pass |
| Structural/redacted diagnostics and M6/M7 ownership boundary | Existing W008/W010 redaction checks; static review of `rust/src/wire/stream.rs` | Pass |
| No W010 oversized-frame difference reopened | Existing oversized-carry test remains unchanged and green | Pass |

## Verification commands and results

Passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1  # 14 passed
rtk cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 176 passed (20 suites)
rtk uv run pytest tests/migration_rs/test_w001_canonical_wire.py tests/migration_rs/test_w011_sse_utf8.py tests/unit/test_sse_decoder.py tests/unit/test_sse_observer.py -q --tb=short --maxfail=1  # 65 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 72 passed, 3 skipped
rtk uv run ruff format --check src/ tests/ scripts/  # 724 files already formatted
rtk uv run ruff check src/ tests/ scripts/  # all checks passed
rtk uv run pyright src/ scripts/  # 0 errors, 0 warnings, 0 informations
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check  # passed before closure-only edits
```

The focused Python W011 test independently passed 2 tests. No live provider,
paid inference, credential, database, or external network call was used.
Closure-only documentation changes were checked with a final `git diff
--check` before the closure commit.

## Resource, dependency, and security review

- No Cargo dependency or lockfile changed. The correction is a small explicit
  final-flush branch in the existing manual UTF-8 decoder.
- The decoder retains only its existing incomplete UTF-8 suffix and bounded
  line/frame state. The replacement is processed through the existing framing
  path; no complete stream or raw provider body is retained.
- The 64 KiB SSE bound and typed oversized behavior remain in force. W011 does
  not alter the already documented W008 oversized-discard supported
  difference.
- Fixtures use synthetic markers and store byte input as compact hex. No
  credentials, authorization headers, sessions, arbitrary provider bodies, or
  raw secret-bearing diagnostics are committed.
- The changed Rust wire module owns no provider HTTP, auth injection, SQLite,
  account/health/quota mutation, dynamic negotiation, retry, response handoff,
  timeout/cancellation policy, or durable finalization.

## Unresolved findings and supported differences

No unresolved correctness or security finding remains within W011's UTF-8/SSE
EOF scope. The W008 supported difference for oversized-frame discard-count
details remains unchanged. W012 remains responsible for the separate
cross-surface semantic qualification gap; W011 does not re-close aggregate M6
or make M7 eligible.

## Registry transition and future-plan audit

W011 is removed from the dependency-ready table and recorded in the completed
table in `migration-rs/registry.md`. Its plan header, canonical-wire
implementation index, handoff sequence, subsystem roadmap, and closure record
now show accepted closure.

W012 is the only future implementation plan directly unblocked by accepted
W011 closure under the repository's serial handoff policy. It is promoted from
blocked/planned to `dependency-ready; W011 closure accepted` in its plan header,
the registry, implementation index, handoff sequence, and roadmap.

No other future plan can be safely unblocked: M7 has no implementation plan and
still requires W012 aggregate M6 re-closure plus its own planning review; M8,
M9, M10, M11, and M12 remain sequenced behind those reviews. Aggregate M6
remains open for W012 only. Historical W010 remains append-only evidence and
was not rewritten.

Recommendation: closed; proceed with W012 only.
