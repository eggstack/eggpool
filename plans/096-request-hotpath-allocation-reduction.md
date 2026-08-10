# Plan 096 — Request Hot-Path Allocation Reduction

Date: 2026-08-10
Status: complete
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`

## Purpose

Reduce CPU and temporary allocation in the ordinary request preparation path on Raspberry Pi/SBC targets without changing routing, context-limit semantics, provider payloads, or introducing a tokenizer/runtime dependency.

This plan targets three narrow costs already visible in production code:

1. Python character-by-character scanning of ASCII-heavy strings during context estimation;
2. synthetic zero-filled `bytes` allocation used only to make a translated body appear larger to the context estimator;
3. repeated construction of immutable provider/trusted-proxy lookup containers per request.

## Relevant code

Primary files/functions:

- `src/eggpool/request/limits.py`
  - `_estimate_string_tokens()`
  - `_estimate_json_value_tokens()`
  - `estimate_context_input_tokens()`
  - `check_context_limits()`
- `src/eggpool/request/proxy_request.py`
  - `_prepare_transcode_preflight()`
  - `_tool_token_padding()`
  - `_handle_proxy_request_inner()`
  - provider parsing / trusted-proxy attribution call sites
- `src/eggpool/runtime_manager.py` and/or immutable request-generation state definitions
- `src/eggpool/runtime_generation_factory.py` for generation-owned precomputation
- focused request-limit/transcoding/proxy tests.

## Governing constraints

1. Do not add tiktoken, tokenizers, Rust/Python extensions, NumPy, or another token-counting dependency.
2. Keep current conservative estimation semantics unless this plan explicitly removes an implementation artifact while preserving the mathematical result.
3. Context-limit enforcement remains a guardrail; upstream provider enforcement remains authoritative.
4. Synthetic padding must never be transmitted upstream.
5. Preserve one parsed request payload and the existing `ParsedRequestPayload` / `ProviderBoundRequest` ownership model.
6. Preserve generation consistency: precomputed immutable collections must belong to the leased generation, not mutable global app state.
7. Do not add microbenchmarks to CI.

## Workstream A — ASCII fast path for string estimation

`_estimate_string_tokens()` currently loops over every Unicode code point to count ASCII characters and UTF-8 bytes for non-ASCII characters. Coding-agent payloads are typically dominated by ASCII code, JSON, tool schemas, logs, and English text.

Implement a fast path equivalent to:

```python
if value.isascii():
    return _ceil_div(len(value), ESTIMATED_TEXT_CHARS_PER_TOKEN)
```

Requirements:

- empty-string behavior remains unchanged;
- ASCII strings produce exactly the same token estimate as before;
- non-ASCII strings continue through the existing weighted UTF-8 path unless a comparably simple native-code path can preserve exact semantics;
- do not replace the estimator with a more complex statistical/tokenizer model.

Add focused equivalence tests over representative ASCII strings, including long code/tool-schema text, plus existing multilingual/non-ASCII cases.

## Workstream B — Eliminate synthetic zero-byte padding allocation

Current translated context-limit validation may:

1. encode the translated payload;
2. compute `tool_token_padding`;
3. calculate a padded length;
4. allocate `encoded_translated_body + b"\x00" * N`;
5. call `check_context_limits()` solely so `len(body)` reflects the extra allowance.

Those zero bytes are an estimator artifact and can become large with tool-heavy prompts.

Replace the materialized padding with explicit arithmetic. Preferred API shape:

- add an optional `extra_input_tokens: int = 0` or equivalently named parameter at the context-estimation/check boundary;
- `estimate_context_input_tokens()` adds the extra estimate mathematically to the decoded-payload estimate and/or the appropriate floor calculation;
- preserve the exact conservative result currently produced by the padding scheme for all practical inputs;
- keep the actual encoded translated body unchanged and reuse it for the eventual provider-bound request where applicable.

Before choosing the formula, write equivalence tests that compare old synthetic-padding results against the new arithmetic behavior across:

- no tool padding;
- small padding;
- large padding;
- body byte floor dominating;
- payload-estimate dominating;
- context/max-input rejection boundaries.

The test may implement the old formula locally as a reference helper; production must not retain the allocation.

## Workstream C — Avoid repeated immutable container construction

Inspect per-request code for generation-invariant collections such as:

```python
set(lease.runtime.immutable_request_state.provider_ids)
tuple(config.security.trusted_proxies)
```

If the underlying runtime state already stores a tuple/frozenset appropriate for lookup, use it directly. Otherwise precompute the best representation once when constructing the immutable generation state.

Preferred representation:

- provider identifiers: `frozenset[str]` if membership dominates;
- trusted proxies: `frozenset[str]` if exact membership is the only operation;
- retain tuple only where deterministic ordered output is externally visible.

Do not duplicate the same collection in multiple generation-owned objects unless necessary for ownership boundaries.

Verify rehash swaps these values with the generation so requests holding an old lease continue using old-generation configuration consistently.

## Workstream D — Avoid incidental regressions in translated request ownership

The optimization must not cause a second serialization or parse pass.

Verify:

- translated payload is encoded no more often than before;
- `PreparedTranscode` still receives the correct encoded body;
- model rewrite still removes provider suffixes correctly;
- compression/synthetic-cache hooks continue operating on the same provider-bound payload generation;
- stream/non-stream paths remain equivalent for context-limit admission.

Do not refactor compression/transcoding architecture in this plan.

## Workstream E — Focused resource observation

A permanent benchmark is not required. If useful during implementation, use a short local one-shot comparison with a large ASCII prompt/tool schema to confirm:

- no synthetic padding allocation proportional to `tool_token_padding` remains;
- the ASCII estimator path avoids the per-character Python loop;
- request results remain identical.

Do not commit benchmark scripts or thresholds solely for this plan.

## Tests

Add/update focused tests for:

1. exact ASCII estimator equivalence for empty, short, large, code-like, and JSON-like strings;
2. unchanged non-ASCII behavior for representative Unicode scripts/emoji;
3. old padding-reference versus new arithmetic equivalence across boundary cases;
4. no padded `bytes` object construction in the translated context-check path (test observable API/output rather than patching `bytes` globally);
5. provider/trusted-proxy membership behavior before and after rehash/generation swap;
6. context-limit acceptance/rejection parity for OpenAI and Anthropic-compatible requests;
7. transcoded tool-bearing request parity.

## Verification

Run affected request-limit/proxy/transcoding tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

No network/live-provider test is required.

## Completion record

Implementation commit: recorded in the follow-up documentation commit.

Implemented the ASCII estimator fast path, allocation-free translated tool
padding arithmetic, generation-owned provider/trusted-proxy lookup sets, and
the read-only provider collection contract. Updated request-limit, runtime,
architecture, operator, project-guidance, and skill documentation. Also
repaired the manually wired transcoding-routing fixture so it installs the
generation finalization supervisor required by the current coordinator
contract.

Verification completed locally:

- `uv sync --frozen --extra ci`
- `uv run ruff format --check src/ tests/ scripts/`
- `uv run ruff check src/ tests/ scripts/`
- `uv run pyright src/ scripts/`
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed
- Focused estimator/state/proxy/transcoding suites — passed
- `uv run pytest tests/unit/test_hotpath_equivalence.py tests/unit/test_payload_utils.py tests/perf/test_hot_path_performance.py -q --tb=short --maxfail=1` — 60 passed
- `uv run pytest tests/integration/test_transcode_routing.py -q --tb=short --maxfail=1` — 3 passed
- `uv run eggpool --config config.example.toml check-config` — passed
- `uv run eggpool --config config.sbc.example.toml check-config` — passed

No benchmark or target-SBC measurement was added; permanent performance
thresholds remain out of CI.

## Acceptance criteria

- [x] ASCII-only strings bypass the Python per-character counting loop.
- [x] ASCII token estimates are exactly identical to the pre-change estimator for the same strings.
- [x] Non-ASCII estimation behavior remains unchanged unless an explicitly equivalent native-code optimization is proven.
- [x] Tool-aware translated context checks no longer allocate zero-filled padding bytes proportional to `tool_token_padding`.
- [x] New arithmetic padding produces the same admission/rejection result as the previous synthetic-padding formula across focused boundary cases.
- [x] Synthetic padding is never included in the actual upstream/provider-bound body.
- [x] Provider-ID membership does not construct a new `set` on every request when immutable generation state can provide the lookup set directly.
- [x] Trusted-proxy membership does not construct a new tuple/set on every request when immutable generation state can provide the lookup collection directly.
- [x] Rehash/generation leases preserve configuration consistency for these precomputed values.
- [x] No extra JSON parse or serialization pass is introduced.
- [x] OpenAI/Anthropic, streaming/non-streaming, and transcoded context-limit behavior remains correct.
- [x] No tokenizer/native/runtime dependency or permanent benchmark infrastructure is added.
- [x] Focused and smoke verification passes.

## Rejection conditions

Reject the implementation if:

- estimator outputs drift merely to obtain speed;
- the new arithmetic padding underestimates requests compared with the previous policy;
- padding is implemented by another temporary string/bytes allocation;
- immutable collections are cached globally outside generation ownership and can become stale after rehash;
- optimization introduces a new parser/tokenizer dependency;
- compression/transcoding architecture is broadened beyond the named hot path;
- performance claims are based on synthetic timing thresholds added to CI.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093, this plan, request limit/proxy source, generation state definitions, and focused tests.
2. Write/reference parity tests for ASCII estimation and synthetic-padding arithmetic before changing behavior.
3. Add the ASCII fast path.
4. Replace physical padding with explicit estimator input.
5. Precompute/reuse immutable provider/trusted-proxy lookup collections within generation ownership.
6. Run focused limit/transcode/proxy tests.
7. Run ordinary lint/type/smoke gate.
8. Optionally perform one local non-retained resource comparison; do not add benchmark infrastructure.
9. Record exact verification and implementation commit in this plan.
10. Stop; leave unrelated request-path optimization for future evidence-driven work.
