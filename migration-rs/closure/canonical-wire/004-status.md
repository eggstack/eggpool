# W004 Closure — OpenAI Chat Completions and Anthropic Messages Codecs

Status: closed

Recommendation: closed; W005 is promoted under the default serial handoff.

Implementation commit: [`f851f62`](https://github.com/eggstack/eggpool/commit/f851f62)

Plan: [W004 — OpenAI Chat Completions and Anthropic Messages codecs](../../implementation/canonical-wire/004-openai-chat-anthropic-messages-codecs.md)

Contract and oracle: [M6 canonical wire contract](../../canonical-wire-contract.md),
the W001 deterministic observations under `fixtures/canonical-wire/`, the W002
canonical IR/admission boundary, and the Python Chat/Messages codecs and
transcoder tests.

## Outcome

W004 adds concrete, pure Rust codecs for OpenAI Chat Completions and Anthropic
Messages under `rust/src/wire/codecs.rs`. Both codecs implement the W003
`WireCodec` contract for request decode/encode, finite response decode/encode,
provider-error evidence, and profile-specific stream-adapter identity. The
closed `builtin_codec_instance` dispatch returns the two finite codecs while
leaving SSE event conversion to W008.

Request decoding reuses W002's bounded canonical admission path. It preserves
ordered roles/content, multipart text and image content, tool definitions,
tool-call/result IDs, tool choice, generation controls, reasoning intent,
structured-output metadata, cache/metadata fields, and omitted/null/false/zero
presence. OpenAI `content: null` is accepted as explicit empty assistant
content for tool-call messages.

Finite response codecs preserve text, reasoning, refusal, tool-call arguments
and IDs, finish/stop reasons, response/model IDs, and canonical usage. OpenAI
and Anthropic finish/stop vocabularies are mapped only at the client-surface
boundary. Valid provider error envelopes become `ProviderErrorEvidence`; a
malformed success or error shape remains a typed codec error. Provider error
messages are bounded and are not included in codec error/debug metadata.

Cross-wire request and response conversion uses the canonical IR as the only
semantic bridge. Unsupported W007-owned media/document/audio target forms are
rejected explicitly. W006-owned structured-output, parallel-tool, and
effort-only reasoning differences produce bounded typed adaptation notices on
the Anthropic target rather than being silently dropped. No HTTP, retry,
negotiation, persistence, SSE buffering, response handoff, cancellation, or
finalization behavior was added.

## Requirement-to-evidence matrix

| W004 requirement | Evidence | Result |
|---|---|---|
| Native Chat request/response | `rust/tests/wire_codecs.rs::chat_codec_round_trips_native_controls_and_tool_linkage` covers controls, tools, IDs, presence, and finite response round-trip | Pass |
| Native Anthropic request/response | `anthropic_codec_preserves_system_blocks_thinking_and_tool_results` and the cross-wire response case cover system/content blocks, thinking, tools, results, usage, and stop reasons | Pass |
| Multipart/content identity | Shared W002 admission plus codec tests cover text, image/document admission, reasoning, refusal, tool-call, and tool-result blocks; W007-owned unsupported target forms fail explicitly | Pass |
| Tool definitions and linkage | Multiple tool-call/result paths retain call IDs, names, arguments/input, tool choice, and ordering | Pass |
| Reasoning and structured intent | Canonical reasoning modes are retained; Anthropic unsupported effort/structured/parallel fields emit typed adaptation notices | Pass |
| Cross-wire Chat ↔ Messages | `cross_wire_request_and_response_use_canonical_identity` proves canonical-only request and response conversion with tool identity and cache usage | Pass |
| Usage normalization | OpenAI nested cache/reasoning counters and Anthropic read/creation counters map to the shared canonical usage type, preserving missing versus zero | Pass |
| Provider errors versus malformed success | `valid_provider_errors_are_evidence_and_malformed_success_is_rejected` covers both outcomes | Pass |
| Resource/security boundary | W002 bounded admission is reused; codec errors/notices expose only stable fields/surfaces; no credentials, headers, raw bodies, or network state exist | Pass |
| M6/M7 boundary | Codec code is synchronous/pure and contains no provider transport, retry, negotiation, durable state, stream buffering, handoff, or finalization | Pass |

## Supported differences and deferred work

- W004 implements finite Chat and Messages transformations only. OpenAI
  Responses and Gemini remain W005; SSE framing, event conversion, and native
  terminal evidence remain W008.
- Documents, audio, and provider-sensitive media/cache adaptation remain W007
  responsibilities. W004 preserves them in canonical admission where W002
  supports them, then rejects an unrepresentable target form rather than
  serializing an opaque or misleading substitute.
- Structured-output, parallel-tool, and advanced reasoning loss/rejection
  policy is intentionally represented as typed adaptation metadata here and
  centralized across all families by W006.
- The finite codec does not choose a fallback profile, retry a request, infer
  provider capability, or mutate resolver/health/quota state.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1  # 6 passed
rtk cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1             # 16 passed
for target in build_manifest canonical_request catalog_refresh database_compatibility health model_router provider_transport quota routing_claims routing_domain routing_domain_d008 wire_codecs wire_profiles; do
  rtk cargo test --manifest-path rust/Cargo.toml --test "$target" -- --test-threads=1 || exit 1
done                                                                                  # 131 passed across all Rust targets
rtk uv sync --frozen --extra ci
rtk uv run pytest tests/migration_rs/test_w001_canonical_wire.py tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_transcoder/test_openai_to_anthropic_body.py tests/unit/test_transcoder/test_anthropic_to_openai_body.py tests/unit/test_transcoder/test_openai_to_anthropic_response.py tests/unit/test_transcoder/test_anthropic_to_openai_response.py -q --tb=short --maxfail=1  # 189 passed
rtk uv run ruff format --check src/ tests/ scripts/  # 723 files already formatted
rtk uv run ruff check src/ tests/ scripts/  # passed
rtk uv run pyright src/ scripts/  # 0 errors, 0 warnings, 0 informations
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check
```

No live provider call, credential, network inference, database migration, or
new dependency was used.

The aggregate `cargo test --all-targets -- --test-threads=1` diagnostic was
also run twice. It reported 28 passing suites and one failure in the existing
`provider_transport::extended_encrypted_proxy_cancellation_recovers_through_same_client`
assertion because Cargo runs integration binaries concurrently. The provider
transport target passes all 29 tests in isolation, and the sequential
all-target target run above passes all 131 tests. The failure is outside the
W004 codec files and does not reproduce when targets are serialized.

## Registry transition and future-plan audit

W004 moves from the dependency-ready table to the completed table in
`migration-rs/registry.md` with this accepted closure record. The canonical
wire roadmap, implementation index, and handoff sequence now promote W005 as
the sole dependency-ready plan because W004 is closed.

W006 remains blocked until both W004 and W005 close. W007 remains blocked on
W006; W008 remains blocked on W007; W009 remains blocked on W008; and W010
remains blocked on W009. No M7 implementation plan is unblocked: M7 remains
behind accepted W010 closure. No unresolved mandatory W004 requirement
remains.
