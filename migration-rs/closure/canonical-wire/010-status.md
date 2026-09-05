# W010 Closure — Differential Qualification and M6 Closure

Status: closed

Implementation commit: [`77e4dde`](https://github.com/eggstack/eggpool/commit/77e4ddeffb2b37b593725e329b57a311ec217e52)

Qualification baseline: `90208a07c070de794af43d31097c9afdb3c4c2f9`

Qualified implementation head: `77e4ddeffb2b37b593725e329b57a311ec217e52`

Plan: [W010 — Differential qualification and M6 closure](../../implementation/canonical-wire/010-differential-qualification-and-m6-closure.md)

Contract and oracle: [M6 canonical wire contract](../../canonical-wire-contract.md),
the committed W001 observations under `fixtures/canonical-wire/`, accepted
W001-W009 closure records, the Rust `WireRuntime`, and the Python migration
harness.

## Outcome

W010 qualifies the integrated M6 transformation boundary and closes M6. The
Rust runtime now matches the W001 finite-response semantic projection for all
five built-in profiles, including OpenAI Chat `reasoning_content`, and selects
the client-surface codec when adapting a decoded provider response. That
decode-upstream/encode-client split is required for cross-surface Responses
and Messages clients and was caught and fixed by this qualification pass.

The new `rust/tests/wire_qualification.rs` suite drives the runtime through
the same bounded semantic boundary used by W001. It covers all three public
client surfaces, all five static upstream profiles, all 15 client/profile
request pairings, all 15 finite response adaptations, all 15 stream/client
adaptations, and the pure M5 routing/affinity bridge. Unsupported document
adaptation remains an explicit typed outcome; it is never silently flattened.

No provider network, credentials, database write, retry, health effect,
downstream handoff, timeout/cancellation policy, finalization, or semantic
model-router selector behavior was added.

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| W001 oracle and exact/semantic parity rules remain authoritative | `w001-fixture-matrix.json`, `w001-sse-fixture-inventory.json`, `w001-python-observations.json`, Python repeatability tests, and Rust fixture projection assertions | Pass |
| W002 canonical admission, limits, IR, and pure M5 bridge | `rust/tests/canonical_request.rs`, `rust/tests/wire_qualification.rs` (`canonical_m5_bridge_and_diagnostics_are_pure_and_redacted`), migration harness | Pass |
| W003 closed static profile registry and codec identity | `rust/tests/wire_profiles.rs`, runtime registry assertions against W001 inventory | Pass |
| W004 Chat/Messages finite codecs | `rust/tests/wire_codecs.rs`, finite response projection for Chat and Anthropic profiles | Pass |
| W005 Responses and both Gemini profiles | `rust/tests/wire_codecs.rs`, all-profile finite response projection and profile matrix | Pass |
| W006 reasoning, tools, structured output, and explicit loss policy | `rust/tests/wire_adaptation.rs`, strict-loss regression, feature-rich request matrix | Pass |
| W007 media/document/cache adaptation and bounds | `rust/tests/wire_multimodal.rs`, feature-rich image/document request, typed unsupported adaptation assertion | Pass |
| W008 incremental SSE, usage, and terminal evidence | `rust/tests/wire_stream.rs`, all five profile streams under one-byte fragmentation, LF/CRLF equality, EOF/oversize checks | Pass |
| W009 selected-profile runtime facade | `rust/tests/wire_runtime.rs`, all-profile runtime operations, plus the W010 cross-surface facade matrix | Pass |
| W010 integrated qualification and M6 closure | `rust/tests/wire_qualification.rs` (7 tests), this accepted closure record, registry/roadmap transition | Pass |

## Fixture, profile, and cross-wire evidence

- 3 public client surfaces: Chat Completions, Responses, and Messages.
- 5 built-in profiles: OpenAI Chat, OpenAI Responses, Anthropic Messages,
  Gemini Interactions, and Gemini generateContent.
- 15 request client/profile combinations attempted. Successful adaptations
  preserve model identity, ordered messages, stream intent, and structural
  metadata; unsupported document forms produce typed
  `UnsupportedSemanticFeature`/`LossRejected` outcomes.
- 15 finite upstream/client response combinations passed through the runtime;
  provider decoding and client encoding are deliberately separate operations.
- 15 stream upstream/client combinations decoded and re-encoded. Native
  terminal events always produce a client terminal marker; intentionally empty
  control/usage encodings remain successful typed adaptations.
- The W001 finite projection matches all five response profiles: exact output
  block kinds and finish categories.
- W001 stream event sequences match all five profile fixtures. One-byte
  fragmentation and CRLF framing produce the same event sequence as LF.
- Native terminal evidence is preserved as `openai_done`,
  `responses_completed`, `anthropic_message_stop`, or `gemini_completed`.
  Premature EOF remains `EofAfterPartialBody` with no terminal evidence.

## Adversarial and bounded behavior

The qualification suite covers malformed JSON, wrong top-level shape, missing
and blank model, request body limits, malformed provider JSON, valid provider
error envelopes, incomplete stream EOF, oversized SSE carry, strict loss
rejection, invalid/unsupported adaptation, explicit/null/zero/missing usage,
Unicode, ordered tools, inline image data, document identity, and schema/tool
redaction. Every failure is typed; no failure path is accepted as successful
terminal evidence.

SSE carry remains bounded at the 64 KiB W008 limit. The Rust boundary reports
an oversized carry as a typed framing failure; unlike the Python observation's
discard-count detail, Rust does not retain or replay discarded bytes. This is
the already documented bounded supported difference from W008, and it cannot
produce a false success. Media and document inputs remain bounded and are not
dereferenced or decoded through a network/filesystem side effect.

## Resource, dependency, and security audit

- W010 added no Cargo dependency and changed no lockfile. `cargo tree
  --depth 1` retains the existing Hyper/Rustls/Eggress transport posture; no
  second HTTP client, actor framework, schema/reflection stack, media/OCR
  stack, or generic streaming framework was introduced.
- Request and provider bodies are parsed/encoded within existing configured
  bounds; streams emit per-frame events and retain only incremental carry and
  usage/terminal summaries. No complete stream buffer or per-event task was
  added.
- `WireRuntime`, canonical IR, encoded-body, stream, provider-error, and M5
  bridge diagnostics expose structural metadata and byte lengths only. Tests
  use synthetic request/schema/provider/session sentinels and assert that raw
  content is absent from default debug output.
- Static boundary review of `rust/src/wire/` found no provider HTTP send,
  auth-secret injection, SQLite write, account claim mutation, dynamic wire
  learning/retry, health effect, response handoff, timeout/cancellation
  policy, finalization, or semantic selector invocation.
- The implementation remains synchronous/caller-driven and is suitable for
  local/SBC deployment under the existing dependency policy.

## Verification commands actually run

Passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 171 passed (20 suites)
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 70 passed, 3 skipped
rtk uv run pytest tests/migration_rs tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_wire_profiles.py tests/unit/test_sse_decoder.py tests/unit/test_sse_observer.py tests/unit/test_normalized_usage.py tests/unit/test_transcoder/test_usage_canonical.py tests/unit/test_transcoder/test_streaming_fixtures.py tests/unit/test_transcoder/test_streaming_error_events.py tests/unit/test_prepared_transcode.py tests/unit/test_prepared_transcode_reuse.py tests/unit/test_routing.py tests/unit/test_routing_transcode_eligibility.py tests/unit/test_model_router_affinity.py tests/contract/test_transcoder_contract.py -q --tb=short --maxfail=1  # 332 passed, 3 skipped
rtk uv run ruff format --check src/ tests/ scripts/  # 723 files already formatted
rtk uv run ruff check src/ tests/ scripts/  # all checks passed
rtk uv run pyright src/ scripts/  # 0 errors, 0 warnings, 0 informations
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check  # passed
```

All repository-wide gates remained green after the closure/governance edits.
No live provider, paid inference, credential, or external network call was
used.

## Unresolved findings and supported differences

No unresolved high- or medium-severity correctness/security finding remains.
The two findings discovered during W010 were corrected in the implementation
commit: OpenAI Chat `reasoning_content` is retained in canonical output, and
client response adaptation uses the client-surface codec rather than reusing
the upstream codec.

The only recorded supported difference is the W008 oversized-SSE discard-count
detail: Rust fails closed with typed framing evidence without retaining or
replaying discarded bytes. This preserves the security/resource invariant and
does not alter terminal success semantics. No new ADR is required; the
behavior is within the frozen W001/W008 normalization and bounded-resource
rules, with ADR-0001 through ADR-0003 still applicable.

## Registry transition and future-plan audit

W010 moves from dependency-ready to completed in `migration-rs/registry.md`.
The canonical-wire implementation index, handoff sequence, subsystem roadmap,
and plan header now mark W010 closed. M6 is closed after accepted W010
evidence.

M7 has all hard M6 prerequisites satisfied and is eligible for its own planning
review. There is no M7 implementation plan in the registry to promote, and no
future implementation plan is dependency-ready automatically. M8-M12 remain
sequenced behind M7 and their own planning/dependency reviews. No plan status
beyond M6 can be safely changed by this closure.

Recommendation: accept W010 closure; begin M7 planning review.
