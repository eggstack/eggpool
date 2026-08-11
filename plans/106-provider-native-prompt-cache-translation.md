# Plan 106 — Provider-Native Prompt Cache Translation

Date: 2026-08-11
Status: implemented
Parent roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`
Depends on:

- `plans/105-openai-anthropic-transcode-parity.md`

## Purpose

Modernize EggPool prompt-cache transcoding so explicit provider-native cache boundaries survive OpenAI↔Anthropic translation where the two protocols genuinely overlap, while TTL, breakpoint-placement, automatic-cache, and tool-definition differences are surfaced explicitly rather than hidden behind synthetic EggPool behavior.

This plan is about protocol translation, not building a cache. EggPool must not store prompt bodies, implement a local semantic cache, or invent a provider-independent cache contract.

## Planning snapshot of current provider semantics

Before implementation, re-check official provider documentation because cache APIs can change. The review baseline on 2026-08-11 identified:

### Anthropic

- automatic/top-level prompt caching is available through `cache_control`;
- explicit block-level cache boundaries are supported;
- cache control can apply to tool definitions in Anthropic's protocol;
- default cache lifetime is approximately 5 minutes with a 1-hour option;
- explicit breakpoint count is bounded (current documented maximum: four);
- minimum cacheable prefix sizes vary by model.

### OpenAI

- prompt caching is implicit by default for supported models;
- current GPT-5.6-family APIs expose `prompt_cache_options.mode` including explicit mode;
- explicit content boundaries use `prompt_cache_breakpoint` on supported prompt/content blocks;
- `prompt_cache_key` is available for routing/cache affinity use cases;
- explicit cache writes are bounded (current documented maximum: four new writes per request);
- current GPT-5.6-family explicit TTL option is approximately 30 minutes;
- documented Chat Completions breakpoint locations do not provide a direct equivalent for Anthropic tool-definition `cache_control`.

These values are planning inputs, not permanent constants. Implement against the verified provider contract at execution time.

## Current EggPool gap

The existing synthetic cache policy is primarily Anthropic-oriented, uses an `ephemeral` TTL concept, allows system/tools placement, has a default minimum stable-token threshold, and may couple cache synthesis to compression policy. It predates current explicit OpenAI prompt-cache breakpoints.

The result is asymmetric:

- Anthropic→OpenAI currently treats cache controls as unsupported/lost even when some content-block boundaries can now be represented;
- OpenAI→Anthropic does not carry current explicit OpenAI cache-breakpoint intent into Anthropic block controls;
- TTL and tool-definition mismatches are not expressed through a modern provider-native translation model.

## Governing constraints

1. Do not implement a local prompt-response cache, prefix store, embedding store, or request-body persistence layer.
2. Do not synthesize `prompt_cache_key` unless a stable, explicit tenancy/prefix partitioning rule already exists and has a demonstrated need.
3. Do not hash full prompt bodies to manufacture cache keys.
4. Translate only provider-native cache intent that is representable at the target boundary.
5. Do not silently convert 5-minute, 30-minute, and 1-hour TTL semantics as if they were equivalent.
6. Do not silently map Anthropic tool-definition cache boundaries to an unrelated OpenAI message boundary.
7. Preserve existing loss-policy behavior for unrepresentable cache semantics.
8. Cache translation failure/loss is local preparation behavior and must never penalize a provider/account or trigger provider retry.
9. Keep cache controls optional. Requests without cache metadata must not pay new traversal/allocation costs beyond trivial capability checks.
10. Do not add a new dependency, database table/migration, background task, cache metrics subsystem, or CI job.
11. Do not persist prompt/cache-control bodies or keys beyond existing request metadata behavior.
12. Use the capability contract settled in Plan 105 rather than introducing a second cache-specific provider registry.

## Workstream A — Re-verify official provider contracts

Immediately before implementation, record the current official OpenAI and Anthropic documentation assumptions for:

- field names;
- eligible endpoint surfaces;
- supported content block types;
- breakpoint count;
- TTL values;
- whether tool definitions support explicit breakpoints;
- whether automatic/implicit caching requires any field;
- model-specific support/minimum prefix requirements.

If official behavior differs from this planning snapshot, follow the current official contract and update the plan closure accordingly. Do not broaden scope to undocumented-compatible providers.

## Workstream B — Inventory current EggPool cache-control surfaces

Use repository search:

```bash
rg -n \
  'cache_control|prompt_cache|cache_synthesis|cache_breakpoint|cache_key|ephemeral|min_stable_tokens|max_breakpoints' \
  src tests config*.toml architecture AGENTS.md docs
```

Classify each surface as:

- source-protocol native field parsing;
- target-protocol native field emission;
- EggPool synthetic cache policy;
- compression/cache coupling;
- observability/loss warning;
- test-only historical behavior.

Do not edit the synthetic-policy subsystem yet except where necessary to avoid duplicate native and synthetic controls. Plan 108 owns broader simplification.

## Workstream C — Minimal cache capability facts

Reuse the Plan 105 capability representation to encode only the facts required to avoid sending unsupported native cache fields, for example:

- supports explicit prompt-cache breakpoints;
- supports automatic/implicit prompt caching if that needs to affect translation behavior;
- supported breakpoint placement family if existing capability representation can express it simply.

Do not encode volatile numeric TTL/minimum-token tables globally unless EggPool must validate a field before dispatch. Prefer preserving provider-native values and letting the provider enforce model-specific minimum prefix sizes where safe.

If EggPool must reject structurally impossible breakpoint placement, do so locally. Do not duplicate every provider cache heuristic.

## Workstream D — OpenAI explicit breakpoint → Anthropic block `cache_control`

For OpenAI source content blocks carrying the verified explicit breakpoint field:

1. preserve normal content translation first;
2. if the translated Anthropic block is an eligible cache boundary and target capability supports explicit caching, attach Anthropic block-level cache control;
3. preserve the number/order of explicit boundaries subject to target protocol maximum;
4. if source supplies more breakpoints than target supports, follow loss policy rather than silently truncating without a signal;
5. do not add synthetic extra boundaries merely to reach a target count.

### TTL handling

If OpenAI source specifies an explicit TTL that has no exact Anthropic equivalent:

- classify the TTL portion as lossy;
- under `reject`, reject before dispatch;
- under `warn`, translate the boundary only if existing loss-policy semantics permit dropping the incompatible TTL, and emit bounded metadata describing source/target TTL mismatch without prompt content;
- under permissive/ignore behavior, preserve the boundary but do not claim TTL equivalence.

If current provider docs introduce an exact common TTL by implementation time, map it directly and update closure evidence.

## Workstream E — Anthropic content-block `cache_control` → OpenAI explicit breakpoint

For Anthropic source message/system content blocks whose translated OpenAI block type supports explicit prompt-cache breakpoints:

1. emit the verified OpenAI breakpoint field at the corresponding translated boundary;
2. preserve order and at most the target-supported explicit boundary count;
3. keep the boundary on the semantically corresponding content block rather than moving it to a convenient later message;
4. classify unsupported block types explicitly.

### Tool-definition boundary

If an Anthropic tool definition carries `cache_control` and current OpenAI endpoint semantics still lack an equivalent tool-definition breakpoint:

- do not move the boundary onto the first/last message;
- do not synthesize a `prompt_cache_key` to pretend equivalence;
- classify the tool-definition cache control as unrepresentable under configured loss policy;
- still translate the tool definition itself if otherwise valid.

If official OpenAI semantics later add tool-definition breakpoints, use the verified native mapping and record the change in closure.

## Workstream F — Automatic/implicit cache intent

Treat automatic caching as provider optimization intent, not response semantics.

### Anthropic top-level automatic cache intent → OpenAI

If Anthropic source requests top-level automatic caching and OpenAI target caching is already implicit by default:

- avoid emitting redundant fields unless OpenAI requires an explicit option to preserve source intent;
- do not manufacture breakpoints;
- if Anthropic source requests an explicit TTL that cannot match OpenAI target behavior, classify only that retention-policy difference as loss.

### OpenAI implicit/default caching → Anthropic

Do not add Anthropic automatic cache controls to every translated request merely because OpenAI may cache prompts implicitly. The absence of an explicit OpenAI cache request is not sufficient source intent for EggPool to change target provider billing/cache-write behavior.

Only translate explicit source cache intent.

This asymmetry is acceptable and should be documented.

## Workstream G — `prompt_cache_key` policy

Audit whether EggPool currently parses or forwards `prompt_cache_key`.

Rules:

- same-protocol OpenAI passthrough should preserve a valid source-provided key under existing unknown-field policy/capability handling;
- OpenAI→Anthropic should not attempt to translate the key if Anthropic has no equivalent;
- Anthropic→OpenAI should not synthesize a key from model/account/prompt content;
- if a source-provided key is dropped during cross-protocol translation, classify it through the normal loss-policy path without logging the key value.

Do not persist or log the key value.

## Workstream H — Interaction with synthetic cache policy

Prevent double application:

- if a request already carries explicit source cache boundaries that translate natively, synthetic cache insertion must not add a second conflicting set;
- native source intent wins over EggPool heuristics;
- if target native translation is impossible and loss policy permits dropping source cache metadata, do not automatically replace it with synthetic breakpoints unless that behavior is already an explicitly documented policy;
- record which synthetic-policy branches become redundant candidates for Plan 108.

Do not perform the broad deletion in this plan unless the code path must be changed to avoid duplicate controls.

## Workstream I — Interaction with compression

Cache boundaries and compression transforms can interact because prefix stability matters.

Requirements:

- provider-native source cache boundaries must remain attached to the semantically corresponding content after any supported safe compression transform;
- a compression transform must not move/delete a native boundary silently;
- if a transform would invalidate a boundary, follow the existing compression/cache policy or loss rule explicitly;
- do not add a new cache-aware compression optimizer.

Plan 108 will simplify obsolete coupling after this native path exists.

## Workstream J — Bounded observability

Loss/warning metadata may include:

- source protocol;
- target protocol;
- cache feature category (`breakpoint`, `ttl`, `tool_definition`, `cache_key`);
- source/target TTL labels if they are non-secret static values;
- source/target breakpoint counts;
- reason code.

It must not include:

- prompt content;
- tool definitions/schemas except a non-content identifier if already safe;
- cache key values;
- document/image bytes.

Follow Plan 104 redaction conventions.

## Workstream K — Focused compatibility tests

Update existing transcode/cache suites. Minimum semantic cases:

1. OpenAI explicit text breakpoint → corresponding Anthropic block cache control.
2. Multiple source breakpoints preserve order up to target limit.
3. Source breakpoint count beyond target limit follows warn/reject policy.
4. OpenAI explicit TTL with no Anthropic exact TTL reports mismatch; reject policy rejects.
5. Anthropic message/system block cache control → corresponding OpenAI explicit breakpoint.
6. Anthropic tool-definition cache control remains an explicit loss when no OpenAI equivalent exists.
7. Anthropic 5-minute/1-hour TTL mismatch against OpenAI target TTL is explicit.
8. Source-provided OpenAI cache key is not logged and is explicitly lost cross-protocol when unrepresentable.
9. Same-protocol passthrough preserves supported native cache fields.
10. Generic compatible target without cache capability receives no unsupported native fields.
11. Native translated boundaries suppress conflicting synthetic insertion.
12. Requests without cache controls retain the current cheap path and payload shape.
13. Loss warnings contain no prompt/cache-key/tool-body content.
14. Compression interaction preserves native cache boundary ownership where supported.

Do not create a provider × model × TTL Cartesian matrix.

## Documentation

Update active transcode/cache configuration documentation to state:

- native cache translation is best-effort only across the common representable subset;
- TTL equivalence is not assumed;
- Anthropic tool-definition cache control may be unrepresentable on OpenAI Chat Completions depending on current provider API;
- OpenAI implicit caching does not cause EggPool to opt Anthropic requests into caching automatically;
- synthetic cache policy is secondary to explicit source intent and may be simplified by Plan 108.

## Verification

Run focused transcode/cache/compression-policy tests identified by search, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

If capability/static-model fields change, also run:

```bash
uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -q --tb=short --maxfail=1
```

Live provider requests are optional manual confidence checks only.

## Acceptance criteria

- [x] Current official OpenAI/Anthropic cache semantics are re-verified and the closure record names the documentation date/assumptions used.
- [x] OpenAI explicit prompt-cache boundaries translate to semantically corresponding Anthropic block cache controls for verified capable targets.
- [x] Anthropic eligible content-block cache controls translate to semantically corresponding OpenAI explicit breakpoints for verified capable targets.
- [x] Breakpoint count overflow is never silently truncated without loss handling.
- [x] TTL values are mapped only when exactly representable; otherwise mismatch follows configured loss policy.
- [x] Anthropic tool-definition cache control is not moved to an unrelated OpenAI message boundary when no native equivalent exists.
- [x] OpenAI `prompt_cache_key` is never synthesized for cross-protocol translation and source key values are never logged/persisted by new code.
- [x] Same-protocol native cache fields remain supported/preserved under existing passthrough rules.
- [x] OpenAI implicit/default caching does not cause EggPool to opt every Anthropic translated request into caching.
- [x] Explicit source cache intent takes precedence over synthetic EggPool cache insertion.
- [x] Native cache boundaries survive supported compression transforms without silent movement/deletion.
- [x] Generic compatible providers do not receive unsupported cache fields solely based on protocol family.
- [x] Requests with no cache controls retain a cheap path and do not acquire full-payload extra scans merely for cache translation.
- [x] Loss observability is bounded and contains no prompt/tool/document/cache-key content.
- [x] No local cache store, body hash/key synthesis scheme, DB migration/table, background task, new dependency, or CI job is added.
- [x] Focused cache/transcode/compression tests pass.
- [x] Ruff, Pyright, smoke tests, and both config checks pass.

## Closure record

Implementation adds a capability-gated native boundary translator. The only
new capability fact is `transcoding.prompt_cache_breakpoints`, keyed by target
protocol. OpenAI explicit content markers map to Anthropic `cache_control` on
eligible translated blocks; Anthropic message/system block controls map to
OpenAI `prompt_cache_breakpoint`. Both directions cap translated boundaries at
four and surface overflow through the configured loss policy. Anthropic tool
definition controls remain unrepresentable on OpenAI Chat Completions and are
never relocated. `prompt_cache_key` is reported as unrepresentable without
including its value in diagnostics. The existing synthetic cache policy stays
secondary and Plan 108 remains responsible for broader simplification.

Provider documentation was re-verified on 2026-08-11 from the official
[OpenAI prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
and [Anthropic prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
The verified assumptions are OpenAI explicit content breakpoints and
`prompt_cache_options.ttl = "30m"` for GPT-5.6-family models, four new cache
writes per request, and Anthropic block/top-level `cache_control`, 5-minute or
1-hour TTLs, and up to four explicit breakpoints. These are provider facts,
not EggPool TTL equivalence rules.

## Rejection conditions

Reject the implementation if:

- EggPool claims TTL equivalence by silently converting 5m/30m/1h retention policies;
- Anthropic tool-definition cache boundaries are moved to arbitrary OpenAI message boundaries;
- prompt bodies are hashed/persisted to synthesize cache identity;
- `prompt_cache_key` values appear in logs or new persistence;
- OpenAI implicit caching causes Anthropic cache controls to be injected on requests with no explicit cache intent;
- synthetic and native cache systems both add conflicting boundaries;
- cache handling adds an unconditional full request traversal to requests without cache metadata;
- a local cache/database/background subsystem or generic provider cache framework is introduced;
- CI/dependencies expand.

## GPT-5.6 Luna implementation sequence

1. Read Plan 103, Plan 105 closure/capability implementation, this plan, current cache synthesis policy, compression/cache interaction, and active docs.
2. Re-check official provider cache documentation and record current semantics.
3. Inventory all source/native/synthetic cache-control paths with `rg`.
4. Reuse Plan 105 capability facts; add only the minimum cache capability flag if needed.
5. Implement OpenAI explicit breakpoint → Anthropic block mapping with bounded count and TTL-loss handling.
6. Implement Anthropic eligible block → OpenAI breakpoint mapping and explicit tool-definition mismatch handling.
7. Preserve same-protocol/source cache keys without logging them; never synthesize cross-protocol keys.
8. Make explicit native source intent suppress conflicting synthetic insertion.
9. Verify compression interaction without adding an optimizer.
10. Add compact semantic fixtures and safe warning assertions.
11. Run focused tests and ordinary gate.
12. Record implementation SHA, supported/unrepresentable mappings, provider-doc assumptions/date, and verification results in this plan.
13. Leave broader synthetic-policy deletion to Plan 108 and stop.
