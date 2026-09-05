# W005 Closure — OpenAI Responses and Gemini Codecs

Status: closed

Implementation commit: [`42200327`](https://github.com/eggstack/eggpool/commit/42200327aadc866c2bad263ffe11a1c3a5045a6a)

Plan: [W005 — OpenAI Responses and Gemini generateContent codecs](../../implementation/canonical-wire/005-openai-responses-gemini-codecs.md)

## Outcome

W005 adds concrete, synchronous Rust finite codecs for OpenAI Responses,
Gemini Interactions, and Gemini `generateContent` under
`rust/src/wire/additional_codecs.rs`. The extra Interactions implementation is
intentional: the frozen W003 registry contains five concrete profile identities
and the Python oracle already treats both Gemini profiles as executable codecs.

All three codecs consume and produce the shared W002 canonical request,
response, usage, and output-block types. Responses preserves instructions,
message content, function-call/function-output item identity, reasoning
summaries, structured-output format, controls, and usage. Gemini preserves
system instructions, ordered text/reasoning/tool parts, function arguments and
responses, tool declarations/configuration, generation controls, structured
output schema, and usage. Cross-surface response encoding goes through the
same canonical output and existing W004 Chat/Messages encoders.

Malformed provider success/error shapes remain typed codec failures. Valid
provider error envelopes become `DecodedProviderPayload::Error`. Gemini
blocked responses are rejected as explicit typed non-success outcomes rather
than becoming empty successful text. Provider error messages are bounded and
codec diagnostics do not contain body content, credentials, headers, or
network state.

The W002 admission boundary was extended narrowly so Responses input arrays
can contain `function_call` and `function_call_output` items and `instructions`
remain canonical system semantics. No second IR, coordinator behavior,
provider submission, retry, negotiation, persistence, SSE framing, or terminal
evidence was added.

## Requirement-to-evidence matrix

| W005 requirement | Evidence | Result |
|---|---|---|
| Native Responses request/response | `responses_codec_preserves_native_items_controls_and_usage` | Pass |
| Native Gemini generateContent request/response | `generate_content_codec_maps_parts_tools_reasoning_and_schema` | Pass |
| Gemini Interactions registry representative | `gemini_interactions_and_all_profiles_have_concrete_dispatch` | Pass |
| System/developer and system-instruction mapping | Responses `instructions`; Gemini `systemInstruction`; canonical source roles | Pass |
| Function/tool declaration, calls, results, IDs | Responses function items and Gemini function-call/function-response parts | Pass |
| Reasoning and structured output | Responses reasoning summaries; Gemini thought parts, thinking budget, JSON MIME/schema | Pass |
| Cross-family conversion | Additional codecs use existing Chat/Messages encoders and Responses canonical encoder | Pass |
| Omitted/null/empty/zero semantics | Shared W002 presence values are emitted by Responses and Gemini controls | Pass |
| Provider errors vs malformed payloads | `provider_errors_and_blocked_gemini_responses_never_become_success` plus W004 error matrix | Pass |
| Bounded and secret-free behavior | Shared admission limits, bounded error text, pure value-only codec types | Pass |
| M6/M7 boundary | No async, HTTP, retry, resolver, durable state, SSE buffering, or finalization code | Pass |

## Python oracle and differential evidence

The committed Python oracle fixtures remain the semantic reference. The
focused Python run passed 189 tests, including the W001 canonical observations,
wire IR/codec tests, and the existing cross-protocol transcoder suites. The
Rust wire suite directly exercises the corresponding native and cross-family
shapes rather than comparing normalized raw JSON, so function/reasoning
identity and malformed-shape differences remain observable.

## Verification commands actually run

Passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1  # 10 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1    # 135 passed
rtk uv sync --frozen --extra ci
rtk uv run pytest tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_transcoder/test_openai_to_anthropic_body.py tests/unit/test_transcoder/test_anthropic_to_openai_body.py tests/unit/test_transcoder/test_openai_to_anthropic_response.py tests/unit/test_transcoder/test_anthropic_to_openai_response.py tests/migration_rs/test_w001_canonical_wire.py -q --tb=short --maxfail=1  # 189 passed
rtk uv run ruff format --check src/ tests/ scripts/  # 723 files already formatted
rtk uv run ruff check src/ tests/ scripts/  # passed
rtk uv run pyright src/ scripts/  # passed with no diagnostics
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check
```

No live provider inference, credential, database migration, network call,
background task, or dependency change was used.

## Supported differences and deferred work

- SSE framing, incremental event conversion, usage event extraction, and
  native terminal evidence remain W008. The codec trait exposes only the
  profile stream identity here.
- Centralized reasoning/tool/structured-output loss policy remains W006;
  W005 emits bounded adaptation notices for controls that cannot be carried
  faithfully by a target surface and does not guess budgets from effort names.
- Documents, audio, advanced media capability policy, and cache adaptation
  remain W007 responsibilities. Unsupported target forms fail explicitly.
- M7 still owns profile negotiation, provider submission, retries, health
  effects, response handoff, cancellation, and finalization.

No unresolved mandatory W005 requirement remains.

## Registry transition and future-plan audit

W005 is removed from the dependency-ready table and added to the completed
table in `migration-rs/registry.md` with implementation commit
`42200327aadc866c2bad263ffe11a1c3a5045a6a`. The canonical-wire roadmap,
implementation index, and handoff sequence promote W006 as the sole
dependency-ready M6 plan because both W004 and W005 are now closed.

W007 remains blocked on W006; W008 remains blocked on W007; W009 remains
blocked on W008; and W010 remains blocked on W009. No M7 implementation plan
is unblocked: M7 remains behind accepted W010 closure. This is the complete
future-plan audit; W006 is the only plan whose hard dependencies changed.
