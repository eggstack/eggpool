# W006 Closure — Reasoning, Tools, Structured Output, and Loss Policy

Status: closed

Implementation commit: [`2835e8c`](https://github.com/eggstack/eggpool/commit/2835e8c)

Plan: [W006 — Reasoning, tools, structured output, and loss policy](../../implementation/canonical-wire/006-reasoning-tools-structured-output-and-loss-policy.md)

Contract and oracle: [M6 canonical wire contract](../../canonical-wire-contract.md),
the W001 observations under `fixtures/canonical-wire/`, W002 canonical
admission/IR, the W004/W005 finite codecs, and the Python transcoder policy,
reasoning, tool, structured-output, and loss-policy tests.

## Outcome

W006 centralizes pure semantic adaptation in
`rust/src/wire/adaptation.rs`. Every finite codec reaches the same bounded
`AdaptationPolicy`: warn mode returns structural notices, reject mode returns a
typed `LossRejected` codec error, and notice count is capped before diagnostics
can grow. Notices contain stable semantic codes, structural fields, and
source/target surfaces only; they never retain prompts, schemas, provider
bodies, credentials, or runtime error history.

Canonical reasoning intent now distinguishes omitted, explicit disable, effort,
fixed budget, adaptive, and toggle controls. Target grammars preserve exact
controls where representable and otherwise return explicit reasoning loss
notices. Capability status is an explicit caller/M5 input with pure
supported/unsupported/unknown/mixed policy evaluation; codecs do not infer it
from network activity or provider errors. Historical reasoning blocks remain
response content and are encoded by each target grammar.

Tool declarations, choices, calls, and results share one admission and
adaptation boundary. Empty, duplicate, or missing call IDs/names are malformed
source input. Responses mixed text/tool ordering is emitted in canonical order
where the target grammar permits it. Gemini generateContent, which has no
native call ID field, reports the loss explicitly and provider response calls
receive a repeatable hash-derived compatibility ID rather than a random or
empty ID. Tool arguments remain JSON objects on targets that require them.

Structured `json_object` and `json_schema` intent remains formal request
metadata. Chat, Responses, and generateContent preserve the supported
representations; unsupported Anthropic/Interactions representations produce
typed notices. No codec converts a schema into a prompt instruction.

No retry, profile negotiation, provider transport, health/quarantine effect,
database write, downstream handoff, cancellation policy, or finalization logic
was added.

## Cross-wire semantic matrix

| Semantic area | OpenAI Chat | OpenAI Responses | Anthropic Messages | Gemini Interactions | Gemini generateContent |
|---|---|---|---|---|---|
| Reasoning effort | exact field; unsupported budget/toggle warns | exact effort/explicit `none`; unsupported budget/toggle warns | budget/adaptive/disable exact; effort warns | effort/disable exact; unsupported budget/toggle warns | budget/disable exact; effort/adaptive warns |
| Historical reasoning | `reasoning_content` | reasoning summary item | `thinking` block | thought step | `thought` part |
| Tools and choice | native declarations/calls/results and stable IDs | ordered function items and stable IDs | `tool_use`/`tool_result` and stable IDs | function steps and IDs | declarations/calls/results; missing native IDs are explicit compatibility loss |
| Structured output | formal response format preserved | formal `text.format` preserved | typed non-representable notice | typed schema notice | JSON MIME/schema preserved |
| Loss result | shared exact/adapted/rejected policy | shared exact/adapted/rejected policy | shared exact/adapted/rejected policy | shared exact/adapted/rejected policy | shared exact/adapted/rejected policy |

The matrix records supported semantic differences rather than normalizing raw
JSON. W007 owns media/document/cache adaptation, W008 owns SSE/event/usage/
terminal behavior, and W009 owns the selected-profile facade.

## Requirement-to-evidence matrix

| W006 requirement | Evidence | Result |
|---|---|---|
| One loss/adaptation policy across finite codecs | `AdaptationPolicy`, `AdaptationOutcome`, `WireCodec::{encode_request,encode_response}_with_policy`; all five finite codecs use shared request notices | Pass |
| Reasoning omitted/disabled/effort/budget/toggle/adaptive | `explicit_disable_is_not_silently_dropped`; focused existing codec and Python thinking suites | Pass |
| Capability supported/unsupported/unknown/mixed input | `reasoning_capability_notices` and `capability_status_is_an_explicit_pure_input` | Pass |
| Tool definitions, choices, calls, results, IDs, and ordering | admission identity validation, Responses ordered emission, W004/W005 tool tests, `malformed_and_duplicate_tool_identity_is_rejected_at_admission` | Pass |
| Deterministic identity where provider IDs are absent | `provider_without_ids_gets_repeatable_compatibility_identity` | Pass |
| Structured JSON object/schema/strictness | shared structured validation plus W004/W005 native schema tests and Python structured-output suite | Pass |
| Warn/reject outcomes and bounded redaction-safe diagnostics | `one_policy_rejects_every_family_loss_without_raw_content`, `warn_policy_is_bounded_and_structural` | Pass |
| Pure rejection with no M7 coupling | synchronous codec/adaptation modules contain no transport, retry, resolver, health, DB, or finalization path | Pass |
| W007 extension point | adaptation policy is generic over notices and target profiles; media/document/cache validation remains in W007-owned paths | Pass |

## Verification commands actually run

Passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 141 passed
rtk uv run pytest tests/migration_rs tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_transcoder/test_thinking.py tests/unit/test_transcoder/test_structured_outputs.py tests/unit/test_transcoder/test_openai_to_anthropic_body.py tests/unit/test_transcoder/test_anthropic_to_openai_body.py tests/unit/test_transcoder/test_openai_to_anthropic_response.py tests/unit/test_transcoder/test_anthropic_to_openai_response.py tests/unit/test_thinking_control_contract.py tests/unit/test_thinking_control_contracts.py tests/unit/test_transcoder/test_budget_resolver.py tests/unit/test_transcoder/test_policy.py tests/contract/test_transcoder_contract.py -q --tb=short --maxfail=1  # 381 passed, 3 skipped
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check
```

No live provider call, credential, network inference, database migration, or
new dependency was used.

## Supported differences and deferred work

- Target-specific reasoning fields are not fabricated. An effort label is not
  guessed into a budget; unsupported material controls warn or reject under the
  explicit policy.
- Anthropic and Gemini Interactions do not receive a prompt-based substitute
  for a formal structured-output constraint.
- Gemini generateContent has no native call-ID field. Its explicit notice and
  deterministic response identity are the bounded compatibility behavior;
  W008 owns incremental assembly of those identities.
- Media, documents, cache controls, provider-sensitive body adaptation, SSE,
  usage merging, terminal evidence, and the selected-profile runtime facade
  remain W007-W009 responsibilities.
- M7 remains responsible for dynamic profile choice, retry, provider send,
  health/failure effects, handoff, cancellation/timeouts, persistence, and
  finalization.

No unresolved mandatory W006 requirement remains.

## Registry transition and future-plan audit

W006 is removed from the dependency-ready table and recorded in the completed
table in `migration-rs/registry.md`. Its implementation plan is marked closed
and this accepted closure record is the historical evidence.

W007 is the only future plan unblocked by W006 under the repository's serial
handoff policy, so it is promoted from planned/blocked to
`dependency-ready; W006 closure accepted` in the plan header, implementation
index, roadmap, handoff sequence, and registry. W008 remains blocked on W007;
W009 remains blocked on W008; W010 remains blocked on W009. M7 implementation
handoff remains blocked on accepted W010 closure. No other future plan can be
safely unblocked by W006 alone.
