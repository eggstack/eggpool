# W009 Closure — Selected-Profile Codec Runtime Boundary

Status: closed

Implementation commit: [`0acbccbf`](https://github.com/eggstack/eggpool/commit/0acbccbf73e61342fccf393a6afde9e2478f4ac5)

Plan: [W009 — selected-profile codec runtime boundary](../../implementation/canonical-wire/009-selected-profile-codec-runtime-boundary.md)

Contract and oracle: [M6 canonical wire contract](../../canonical-wire-contract.md),
the W001 observations under `fixtures/canonical-wire/`, W002-W008 canonical
types/codecs/adaptation/streaming, and the Python wire/transcoder/request and
M5 routing/affinity modules.

## Outcome

W009 adds `rust/src/wire/runtime.rs`, the single caller-selected-profile
facade for M7. `WireRuntime` owns an immutable, shareable
`WireProfileRegistry`; `WireRuntimeContext` carries only bounded, secret-free
provider/profile/model facts, adaptation policy, static profile flags, and
body limits. The context is validated against the registry before any
operation, and no runtime history or alternate profile is consulted.

The facade admits raw client bytes exactly once into W002 canonical IR,
prepares a fresh selected-profile provider body from the source IR while
rewriting only the selected upstream model identity, and returns structural
content metadata, adaptation kind/notices, stream intent, profile/model
identity, and byte facts. Native same-surface requests preserve caller bytes
when the model identity also matches; alternate profiles are compactly
encoded with the configured loss policy.

Finite responses accept bounded provider bytes and return one of three typed
outcomes: canonical success, valid provider-error evidence, or malformed
provider response evidence. Success includes canonical content metadata,
shared usage, adaptation notices, and an optional client body. Client response
encoding is also available as a separate pure operation.

`WireStream` wraps W008's bounded incremental decoder. It exposes caller-driven
push/finalize, usage, terminal evidence, cumulative byte facts, and client
event encoding without sockets, async tasks, downstream ownership, or
finalization. The M5 routing and affinity bridges are direct pure adapters.

## Requirement-to-evidence matrix

| W009 requirement | Evidence | Result |
|---|---|---|
| One stable facade for admission, request/response transformation, and streams | `rust/src/wire/runtime.rs`; seven focused facade tests | Pass |
| Explicit selected profile is mandatory and caller-owned | Registry definition equality validation; `selected_profile_mismatch_is_typed_and_cannot_fall_back` | Pass |
| Secret-free bounded adaptation context | `WireRuntimeContext` limits/debug implementation; identity contains only profile/model/provider facts | Pass |
| Request preparation to every built-in profile | `one_facade_prepares_every_selected_profile_without_changing_source_ir` across all five `WireSurface` values | Pass |
| Native passthrough and source-IR ownership | `native_same_surface_request_uses_caller_bytes_without_reencoding`; source canonical model remains unchanged while upstream identity is selected | Pass |
| Finite success/provider-error/malformed separation | `finite_results_separate_success_provider_error_and_malformed_response` across all five profiles | Pass |
| W006 warnings/rejections and typed adaptation layers | Existing W006 policy suite plus `RequestAdaptation`/`ResponseAdaptation` typed errors | Pass |
| W007 bounds and semantic content metadata | W002/W007 admission and multimodal suites; `SemanticContentMetadata` is structural and body limits are enforced before encoding | Pass |
| W008 stream bounds, usage, terminal evidence, and client events | `stream_instances_are_independent_and_expose_terminal_usage_evidence`; existing W008 stream suite | Pass |
| Pure M5 bridges and selector-style payload readiness | `m5_bridges_and_selector_style_payload_are_pure_and_redacted`; routing/affinity target suites | Pass |
| Independent instances and diagnostics redaction | `runtime_and_context_are_shareable_without_mutable_global_codec_state`; redacted `Debug` assertions | Pass |
| No M7 ownership leakage | Static review of runtime module: no DB, HTTP, credentials, resolver, retry, health, timeout, cancellation, handoff, or finalization paths | Pass |

## Error-layer evidence

The facade keeps client admission failures (`ClientAdmission` with a typed
`CodecError`), selected-profile/configuration mismatch (`ProfileMismatch`),
request loss/adaptation rejection (`RequestAdaptation`), client response
adaptation rejection (`ResponseAdaptation`), bounded body failures, and W008
stream framing/event failures (`Stream`) distinct. Finite provider decoding
maps invalid JSON or codec rejection into `FiniteResponseOutcome::Malformed`
and preserves valid provider envelopes as `ProviderError`; it does not expose
retryability, health, or final HTTP decisions.

No raw prompt, media, schema, session, credential, proxy, or provider body is
included in the default `Debug` output or typed runtime errors. Usage and
terminal summaries remain the W008 typed evidence consumed by M7.

## Verification

Passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 164 passed (19 suites)
rtk uv run pytest tests/migration_rs tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_wire_profiles.py tests/unit/test_sse_decoder.py tests/unit/test_sse_observer.py tests/unit/test_normalized_usage.py tests/unit/test_transcoder/test_usage_canonical.py tests/unit/test_transcoder/test_streaming_fixtures.py tests/unit/test_transcoder/test_streaming_error_events.py tests/contract/test_transcoder_contract.py -q --tb=short --maxfail=1  # 230 passed, 3 skipped
rtk uv run pytest tests/unit/test_prepared_transcode.py tests/unit/test_prepared_transcode_reuse.py tests/unit/test_routing.py tests/unit/test_routing_transcode_eligibility.py tests/unit/test_model_router_affinity.py -q --tb=short --maxfail=1  # 102 passed
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check
```

The Rust focused runtime suite itself passed 7 tests. No live provider,
credential, network, database migration, or paid inference call was used.
No Cargo dependency or lockfile change was needed.

## Resource, dependency, and security review

- Registry and codec definitions are immutable/shareable through `Arc`; each
  request and stream owns independent bounded state.
- Request/provider bodies are bounded before parse/encode, compact encoding is
  bounded, and stream state remains W008 incremental rather than buffering a
  complete response.
- Diagnostics expose only profile/model/provider identities, structural counts,
  reason codes, usage/terminal status, and byte counts. Custom debug output
  omits JSON values and body text; canonical IR retains its existing redacted
  debug contract.
- The implementation adds no HTTP/TLS client, async runtime behavior, DB
  access, filesystem access, retry budget, preference learning, health effect,
  downstream handoff, or finalization state.
- Cargo dependency posture is unchanged and remains compatible with the local
  and SBC migration boundary.

## Supported differences and deferred work

- Native request passthrough is intentionally conditional on exact same-surface
  compatibility and equal canonical/upstream model identities. A selected
  upstream model rewrite uses the canonical codec so the body identity is
  explicit and deterministic.
- Malformed finite provider JSON is represented as bounded typed evidence; the
  facade does not retain or replay discarded bytes. M7 chooses the client HTTP
  status and retry/finalization consequence.
- Dynamic wire negotiation, learned preferences, alternate-wire retry,
  provider submission/auth headers, timeout/cancellation policy, downstream
  response handoff, health/failure effects, durable attempts, and finalization
  remain M7 responsibilities.
- The semantic selector is not invoked. Selector-shaped canonical requests and
  responses are proven to pass through the ordinary supplied-profile operations;
  M7 owns selector inference and coordinator composition.

No unresolved mandatory W009 requirement remains. No corrective pass is
required.

## Registry transition and future-plan audit

W009 is removed from the dependency-ready table and recorded in the completed
table in `migration-rs/registry.md`. Its plan, implementation index, handoff
sequence, roadmap, and status header are marked closed.

W010 is the only future plan unblocked by accepted W009 closure under the
repository's serial handoff policy, so it is promoted to
`dependency-ready; W009 closure accepted` in its plan header, the canonical
wire implementation index, handoff sequence, roadmap, and registry. M6
remains active at W010 pending its integrated qualification and aggregate
closure.

M7 coordinator/retry/finalization implementation handoff remains blocked on
accepted W010 closure and its own planning review. M8-M12 retain their stated
roadmap sequencing. No other future plan is safely unblocked by W009 alone.

Recommendation: closed; proceed with W010 only.
