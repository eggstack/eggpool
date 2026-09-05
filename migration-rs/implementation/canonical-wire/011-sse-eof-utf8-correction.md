# W011 — SSE EOF UTF-8 Finalization Correction

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w011--sse-eof-utf8-finalization-correction`

Primary class: invariant/corrective

Repository baseline: `fb36054278817de63b5c516c82202184c9200be7`

Hard dependencies: W001-W010 retain their historical closure records. W010 aggregate M6 closure is reopened only for the findings in W011/W012.

## 1. Objective

Correct the Rust incremental SSE decoder so EOF handling matches the live Python UTF-8 replacement semantics instead of silently dropping an incomplete trailing multibyte sequence. Add regression evidence that would have caught the post-W010 defect.

This is a narrow streaming-parser correction. Do not redesign the SSE/event layer, change transport ownership, or begin M7 coordinator work.

## 2. Why this corrective pass exists

W008 required parity for invalid UTF-8 handling and EOF flush behavior. W010 then declared the integrated stream boundary qualified, but its Rust stream tests exercised valid UTF-8 fragmentation, malformed JSON, premature EOF, and oversized carry without exercising a truncated UTF-8 code point at EOF.

The live Python oracle uses an incremental UTF-8 decoder with `errors="replace"` and, in `SSEDecoder.finish()`, calls `decode(b"", True)`. Any incomplete final code unit sequence is therefore flushed as U+FFFD and counted in `invalid_utf8_replacements` before the final line/event is processed.

At the W010 baseline, Rust `SseDecoder::decode_utf8()` retains an incomplete suffix when `std::str::from_utf8()` returns `error_len() == None`. `SseDecoder::finish()` calls that same routine with an empty slice but does not force the pending suffix through EOF replacement. The pending bytes can therefore disappear without replacement/count evidence.

That is a behavioral mismatch at a frozen W008 boundary and invalidates the W010 aggregate conclusion for this case only.

## 3. Python oracle

Primary sources:

- `src/eggpool/proxy/sse.py::SSEDecoder.feed`;
- `src/eggpool/proxy/sse.py::SSEDecoder.finish`;
- `tests/migration_rs/canonical_wire_fixtures.py`;
- `tests/unit/test_sse_decoder.py` and any existing SSE observer tests;
- `migration-rs/canonical-wire-contract.md`;
- W008 and W010 plan/closure records.

The implementation agent must observe the Python behavior directly for the EOF-invalid sequences added by this plan. Do not infer replacement counts from Rust implementation convenience.

## 4. Exact behavioral contract

The corrected decoder must satisfy all of the following:

1. A valid multibyte UTF-8 scalar split across arbitrary `feed()` calls remains pending until complete and emits no replacement.
2. Once the missing continuation bytes arrive, the scalar is emitted exactly once and `invalid_utf8_replacements` does not increment.
3. A definitely invalid byte sequence during `feed()` follows Python `errors="replace"` behavior and increments replacement evidence consistently with the oracle.
4. EOF with an incomplete multibyte suffix flushes replacement text/evidence exactly as the Python incremental decoder does.
5. The flushed replacement participates in the final SSE line/event rather than being silently discarded.
6. If that replacement makes a provider payload malformed JSON, the provider stream layer must report the existing typed malformed-event outcome. It must not invent terminal success.
7. If the final frame is otherwise valid text/SSE, EOF framing behavior remains unchanged.
8. The 64 KiB carry/frame bound remains effective. No unbounded copy or retained raw stream body is allowed.
9. Debug/error output remains structural and redacted.
10. Existing W008 supported oversized-frame behavior is not reopened by this pass.

## 5. Required implementation shape

Prefer a small explicit EOF-flush path in `rust/src/wire/stream.rs` rather than adding a Unicode/streaming dependency. The current manual incremental decoder is sufficient if EOF can distinguish "need more bytes during feed" from "final incomplete sequence at end of stream."

It is acceptable to factor the UTF-8 routine into normal-feed and final-flush helpers if that makes the state transition explicit. Do not duplicate the complete SSE framing state machine.

No Cargo dependency is expected or authorized without new evidence.

## 6. Required regression matrix

Add direct Rust/Python differential fixtures for at least:

- every split point through one valid 2-byte scalar;
- every split point through one valid 3-byte scalar;
- every split point through one valid 4-byte scalar;
- EOF after each incomplete prefix of those scalars;
- invalid continuation after a retained prefix;
- invalid standalone bytes before a newline and before EOF;
- invalid/truncated bytes inside a `data:` line;
- invalid/truncated bytes in an ignored/comment field;
- incomplete UTF-8 after an otherwise valid JSON prefix;
- one-byte feeding of the same malformed fixtures;
- LF and CRLF where line ending affects finalization.

For each fixture compare final frame data, replacement count, emitted frame count, and downstream stream classification where applicable.

## 7. W001/W008 oracle extension

Extend the migration observation harness with bounded synthetic invalid-UTF-8 cases. The committed artifact may store a compact semantic projection, but it must include enough evidence to distinguish:

- completed split scalar;
- invalid replacement during feed;
- incomplete suffix replaced at EOF;
- malformed provider event caused by replacement;
- no false terminal evidence.

Do not store arbitrary user/provider body content.

## 8. Failure and lifecycle semantics

`finish()` remains caller-driven and synchronous. It owns parser EOF only, not socket EOF policy, timeouts, cancellation, retry, response handoff, or durable finalization.

After a framing/parser failure, preserve existing terminal-state behavior. Do not make `finish()` recover a stream that has already failed structurally merely to improve parity metrics.

## 9. Out of scope

- request/response cross-surface qualification (W012);
- provider HTTP or auth;
- wire negotiation/preference/retry;
- routing/claim/health effects;
- database writes;
- downstream response ownership;
- cancellation/timeout policy;
- M7 coordinator/finalization;
- broad streaming refactors;
- changing the documented oversized-SSE supported difference.

## 10. Verification

Minimum commands:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest tests/migration_rs/test_w001_canonical_wire.py tests/unit/test_sse_decoder.py tests/unit/test_sse_observer.py -q --tb=short --maxfail=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk git diff --check
```

If test names differ at implementation time, record the exact equivalent commands in closure evidence.

## 11. Acceptance criteria

W011 closes only when:

- the EOF-truncated UTF-8 mismatch is reproduced against Python before the fix and passes after the fix;
- valid split multibyte input remains chunk-boundary independent;
- invalid replacement counts and final frame/event semantics match the oracle for the new matrix;
- malformed final provider events remain typed failures with no false terminal success;
- the SSE state bound and redaction posture remain intact;
- no M7 behavior or new network/durable-state ownership enters `rust/src/wire/`;
- all prior W008/W010 streaming regressions remain green.

## 12. Stop conditions

Stop and create another bounded corrective plan rather than expanding W011 if the new fixtures reveal a different semantic defect outside UTF-8/SSE EOF finalization, such as provider-family event translation, usage accounting, or client event encoding.

## 13. Closure evidence and registry transition

Create `migration-rs/closure/canonical-wire/011-status.md` containing:

- failing-before/passing-after Python/Rust EOF fixtures;
- exact replacement-count/frame/classification matrix;
- implementation commit(s);
- verification commands/results;
- dependency/resource/security review;
- unresolved findings.

Accepted W011 closure promotes W012 as the sole dependency-ready M6 plan. It does **not** by itself re-close aggregate M6 or unblock M7.