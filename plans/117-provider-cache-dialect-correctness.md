# Plan 117 — Provider Cache Dialect Correctness

Date: 2026-08-11
Status: complete
Parent roadmap: `plans/113-sbc-hotpath-reduction-and-protocol-clarity-roadmap.md`
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`

## Purpose

Correct the protocol abstraction around prompt-cache controls so EggPool distinguishes first-party OpenAI/Anthropic semantics from provider-specific OpenAI-compatible extensions.

This is a correctness/clarity pass, not a new cache framework and not an OpenAI Responses API implementation.

The current translator supports content-level `prompt_cache_breakpoint` / top-level `prompt_cache_options` behavior under an `openai` protocol capability. As of the planning review on 2026-08-11, current first-party OpenAI documentation exposes automatic prompt caching plus fields including `prompt_cache_key` and `prompt_cache_retention`; the review did not establish first-party Chat Completions documentation for the explicit content-level breakpoint fields. Those breakpoint fields may still be valid for specific OpenAI-compatible providers and should remain supportable where an explicit provider/model contract verifies them.

Anthropic has distinct provider-native cache-control placement and TTL semantics. Cross-protocol translation must surface loss when semantics cannot be represented rather than implying protocol-wide equivalence.

Because provider APIs change frequently, the implementation MUST re-verify official documentation on its execution date. The execution-time docs are authoritative over this planning snapshot.

## Governing constraints

1. Do not add OpenAI Responses API endpoints or request/stream adapters in this plan.
2. Do not remove provider-specific explicit breakpoint support if a configured provider actually supports it.
3. Do not infer provider-extension support solely from `protocols = ["openai"]`.
4. Do not infer Anthropic cache semantics solely from a generic `anthropic`-compatible label when a third-party provider does not verify those controls.
5. Capability/dialect handling must remain explicit and small; do not build runtime capability discovery, schema introspection, or a generic provider feature registry.
6. Preserve Plan 112's corrected absent-versus-malformed and mapped-versus-handled semantics.
7. Preserve cache-key privacy. Never log/persist cache keys, prompt content, cache-control content, or raw malformed values.
8. Preserve the existing bounded breakpoint behavior for providers that genuinely support explicit breakpoints unless current provider documentation establishes a different verified bound for that provider.
9. Preserve explicit loss/reject behavior for unrepresentable TTL/location/tool-definition semantics.
10. Do not create synthetic equivalence between automatic cache bucketing and explicit content-block cache boundaries.
11. Provider-native fields may be emitted only when capability/dialect contract verifies them.
12. No new core dependency, database migration, background task, or network probe.
13. Keep cache translation deterministic and request-local.
14. Do not change routing/finalization/database/retry behavior.

## Workstream A — Re-verify first-party provider contracts

Immediately before implementation, consult current official primary documentation for:

### OpenAI

Verify separately for:

- Chat Completions request fields;
- Responses API request fields, only to avoid conflating them with Chat Completions — do not implement Responses here;
- prompt caching behavior;
- `prompt_cache_key` semantics;
- `prompt_cache_retention` allowed values/defaults;
- whether any first-party endpoint currently documents content-level explicit breakpoints or `prompt_cache_options`;
- usage/cache token reporting relevant to existing normalizers.

### Anthropic

Verify:

- Messages API cache-control placement;
- automatic versus explicit caching behavior if both exist;
- supported `cache_control` shape;
- supported TTL values/defaults;
- maximum explicit cache breakpoints/boundaries if documented;
- tool-definition/system/message block support;
- usage cache read/write fields relevant to existing normalizers.

Record a concise dated closure note with the field names and semantics actually used. Do not paste large documentation excerpts.

## Workstream B — Inventory current capability representation

Locate all production uses of:

- `prompt_cache_breakpoints` capability;
- `prompt_cache_breakpoint` field;
- `prompt_cache_options` field;
- `prompt_cache_key`;
- `prompt_cache_retention`;
- Anthropic `cache_control`;
- cache TTL warning metadata;
- provider/model capability overrides related to caching;
- synthetic cache controls that insert provider-native fields.

Determine which providers/models currently set the explicit-breakpoint capability and why.

Classify each as:

1. first-party OpenAI standard behavior;
2. first-party Anthropic standard behavior;
3. verified provider-specific OpenAI-compatible extension;
4. EggPool synthetic/internal concept;
5. stale/unsupported behavior.

Do not commit a permanent matrix. Summarize the final classification in plan closure and active docs.

## Workstream C — Separate protocol semantics from provider-extension semantics

### Required contract

A provider speaking OpenAI-shaped Chat Completions JSON does not automatically support every field an OpenAI-compatible vendor may invent.

Refactor the smallest capability boundary so the translator can answer questions such as:

```text
Does this selected provider/model accept explicit content cache breakpoints?
Does this selected provider/model accept a cache retention control?
Does this selected provider/model support only automatic cache behavior?
```

The exact field names are implementation-dependent. Prefer a small capability flag/enum already compatible with `TranscodingCapabilities` / model capability overrides.

Do not build a generic dialect class hierarchy.

If the existing `prompt_cache_breakpoints` capability can be retained but made explicitly provider/model-sourced instead of protocol-family-derived, that may be sufficient. Rename only if the current name materially misleads callers/docs.

## Workstream D — OpenAI source semantics

For first-party-standard OpenAI fields verified on execution date:

- preserve/pass through same-protocol fields when the selected upstream supports them;
- never expose cache keys in logs or diagnostics;
- when translating to Anthropic, distinguish grouping/bucketing keys from content cache boundaries;
- do not synthesize an Anthropic boundary from `prompt_cache_key` alone;
- map retention only if semantics are genuinely representable; otherwise record bounded TTL/retention loss metadata;
- keep first-party Responses-only fields out of Chat Completions if they are not valid there.

If current first-party Chat Completions no longer supports a field EggPool currently treats as standard, remove/reject/ignore it according to the established loss policy rather than forwarding it to OpenAI merely because the client sent it.

## Workstream E — Anthropic → OpenAI-compatible explicit breakpoint extension

For a provider/model that explicitly verifies content-level breakpoint support:

- Anthropic `cache_control` may map to the provider extension only for representable placements;
- emit any required provider-specific top-level mode/options only after at least one actual target breakpoint was emitted;
- preserve Plan 112's successful-mapping return semantics;
- use provider-specific verified TTL/bound values, not a generic hard-coded OpenAI TTL label;
- reject/warn explicitly for tool-definition boundaries or other source placements not representable on that provider extension;
- generic/unverified OpenAI-compatible targets receive no extension fields.

If no currently configured/shipped provider is verified to use these extension fields, retain the capability-gated translator only if there is a clear intended external contract; otherwise consider removing the dead surface under Plan 118 rather than pretending it is first-party OpenAI behavior.

## Workstream F — OpenAI-compatible extension → Anthropic

When a client sends explicit breakpoint extension fields:

- treat absence as no-op, preserving Plan 112;
- malformed presence remains a real bounded loss;
- translate only if the selected Anthropic provider/model explicitly supports the target cache-control semantics;
- remove source-only extension fields from the target body;
- do not classify the source extension as first-party OpenAI semantics in docs/logs/capability names unless execution-time first-party docs actually establish that fact.

## Workstream G — TTL/retention loss metadata

Audit hard-coded warning labels such as target TTL strings.

Requirements:

- warning metadata must reflect the selected target capability/provider contract, not a generic stale literal;
- if the target has multiple allowed TTLs/default behavior, report a bounded semantic label rather than inventing a single equivalent;
- a TTL mismatch must not silently alter the source request's intended retention;
- loss-policy `reject` remains able to reject genuine TTL/retention mismatches where existing policy considers them protected losses;
- warning metadata contains no prompt/cache-key content.

Do not add dynamic provider-doc lookups at runtime.

## Workstream H — Synthetic cache precedence

Audit synthetic cache controls after the dialect correction.

Rules:

- native source intent wins over synthetic insertion;
- synthetic cache controls may insert a provider field only when the selected provider/model explicitly verifies that field;
- automatic first-party caching should not cause EggPool to synthesize artificial explicit boundaries merely to create symmetry;
- if synthetic controls become redundant for all actively supported providers, Plan 118 may remove them; do not perform broad deletion here unless it is a direct consequence of fixing a wrong field.

## Workstream I — Documentation terminology

Update active docs/config comments so they distinguish:

- OpenAI Chat Completions protocol;
- Anthropic Messages protocol;
- provider/model capabilities;
- OpenAI-compatible extension fields;
- automatic cache behavior versus explicit cache boundaries;
- cache grouping key versus cache boundary versus retention/TTL.

Do not add a long provider standards document. Update the existing transcoding/provider/config documentation only.

## Focused tests

At minimum cover:

- generic OpenAI-compatible provider with no explicit-breakpoint capability receives no `prompt_cache_breakpoint` or `prompt_cache_options` from Anthropic translation;
- provider/model explicitly configured for the extension receives mapped breakpoint fields for supported placements;
- capability does not appear merely because protocol is `openai`;
- same-protocol first-party-standard OpenAI cache fields pass through only where valid for that endpoint/provider contract;
- `prompt_cache_key` does not create Anthropic `cache_control` by itself;
- retention/TTL mismatch produces bounded correct loss metadata;
- no stale generic target TTL such as an unsupported hard-coded value remains where capability metadata should decide it;
- ordinary content with no cache marker remains warning-free under `loss_policy=reject`;
- malformed marker remains a real rejectable loss;
- successful mapping alone enables any target explicit-cache mode/options;
- zero successful mappings emit no target mode/options;
- Anthropic tool-definition cache boundary remains explicit loss when target cannot represent it;
- source payload remains unmodified;
- cache keys/raw content never appear in logs captured by privacy tests.

Avoid provider × policy × placement Cartesian matrices. One test per distinct semantic branch is enough.

## Verification

Run existing cache translation, OpenAI→Anthropic, Anthropic→OpenAI, capability override, synthetic cache, loss-policy, privacy/log-redaction, and same-protocol passthrough focused suites.

Then run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No full retained-suite requirement and no live provider requirement for deterministic mapping correctness.

## Explicit acceptance criteria

- [x] Implementation closure records execution-date official OpenAI and Anthropic cache field/TTL semantics used for the mapping.
- [x] First-party protocol semantics and provider-specific compatible extensions are explicitly distinguished in production capability logic.
- [x] Generic `openai` protocol compatibility alone does not enable explicit content cache-breakpoint fields unless current first-party docs verify them as standard.
- [x] Generic OpenAI-compatible providers with unknown capability receive no `prompt_cache_breakpoint`/`prompt_cache_options` extension fields.
- [x] Verified provider/model extension capability can still enable those fields where intentionally supported.
- [x] `prompt_cache_key` remains a grouping/cache-key concept and is not silently converted into a content cache boundary.
- [x] Retention/TTL controls map only where semantically representable; otherwise loss is explicit.
- [x] Hard-coded target TTL warning metadata is corrected to selected-provider semantics or a bounded non-equivalence label.
- [x] Anthropic `cache_control` remains capability-gated and placement-aware.
- [x] Plan 112 absence/malformed and successful-mapping semantics remain intact.
- [x] Native source cache intent takes precedence over synthetic cache insertion.
- [x] Synthetic controls cannot emit an unverified provider extension.
- [x] Cache keys, prompt content, raw malformed cache values, and credentials are absent from logs/persistence/diagnostics.
- [x] OpenAI Responses API is not added or partially emulated by this plan.
- [x] No runtime capability-discovery service, database migration, dependency, or background task is added.
- [x] Active docs no longer describe provider-extension cache fields as generic first-party OpenAI behavior unless execution-date docs establish that fact.
- [x] Focused cache/transcode/capability/privacy tests pass.
- [x] Ruff, Pyright, 14 smoke tests, and both config checks pass.

## Closure record

Implementation commit: `21f5ba0`.

Implementation introduces `PromptCacheCapability` entries under
`TranscodingCapabilities.prompt_cache_breakpoints`. Each selected
provider/model contract declares `dialect = "first_party"` or
`"compatible_extension"`, verified TTL labels, and a maximum of four explicit
boundaries. A bare OpenAI-compatible protocol label cannot enable cache fields;
the old persisted protocol-list shape is treated as unknown during cache
hydration. Synthetic Anthropic `cache_control` insertion uses the same selected
target contract and reports `capability_unverified` without mutating the body
when the contract is absent.

Provider documentation was re-verified on 2026-08-11 from the official
[OpenAI prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching),
[OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat),
and [Anthropic prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
The OpenAI guide documents automatic caching, `prompt_cache_key` as a cache
grouping/routing hint, and GPT-5.6-family explicit content breakpoints using
`prompt_cache_breakpoint` and `prompt_cache_options`; the documented breakpoint
TTL is `30m`, with up to four new writes per request. The Chat Completions
reference lists supported breakpoint content parts. Anthropic documents
block-level `cache_control`, a 5-minute default, optional `1h` TTL, and up to
four explicit breakpoints across tools, system, and messages. These facts are
represented as provider/model contracts; no automatic-to-explicit equivalence
or TTL conversion is inferred.

Verification completed locally on 2026-08-11:

- `uv sync --frozen --extra ci`: passed.
- Focused cache/transcoder/capability suites: **632 unit tests passed**;
  cache/prepared/provider-bound suites: **129 passed**; transcoding
  integrations: **35 passed**.
- `ruff format --check src/ tests/ scripts/`: passed; 711 files formatted.
- `ruff check src/ tests/ scripts/`: passed.
- `pyright src/ scripts/`: passed with 0 errors, warnings, or information.
- `PYTHONHASHSEED=0 TZ=UTC pytest tests/smoke/ -q --tb=short --maxfail=1`:
  passed (14 smoke tests).
- Both shipped `check-config` commands passed.

No Responses API implementation, dependency, migration, runtime discovery,
background task, live provider request, hardware, benchmark, soak, or full
retained-suite evidence is claimed.

## Rejection conditions

Reject the implementation if:

- protocol name remains the only reason an extension field is emitted;
- provider-specific cache behavior is hard-coded globally as "OpenAI" behavior without a capability source;
- automatic caching and explicit boundaries are treated as equivalent;
- cache key values are logged/persisted;
- runtime reaches out to provider docs/capability endpoints to decide fields;
- a generic dialect/plugin framework is introduced;
- Responses API support is partially grafted onto Chat Completions;
- Plan 112 regressions return;
- loss behavior silently drops an unrepresentable TTL/boundary that previously had protected warning/reject semantics.

## Handoff sequence

1. Read Plan 113, this plan, completed Plans 106/111/112, cache translator helpers, capability models, provider config overrides, synthetic cache code, and owning tests.
2. Re-check current official OpenAI/Anthropic primary docs and note execution date/fields.
3. Inventory which explicit cache fields are first-party standard versus provider extension.
4. Correct the smallest capability boundary; avoid a class hierarchy.
5. Update Anthropic↔OpenAI-compatible translation and TTL labels.
6. Verify synthetic cache precedence/capability gating.
7. Update existing docs terminology.
8. Run focused semantic/privacy tests and ordinary gate.
9. Record implementation SHA, provider semantics snapshot, final capability representation, intentionally unsupported mappings, and exact verification results.
10. Stop. Responses API and unrelated provider feature parity require separate future planning.
