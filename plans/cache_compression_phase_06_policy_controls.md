# Phase 6 Plan: Policy Controls and Safety Rails

Date: 2026-07-02

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Depends on:

- `plans/cache_compression_phase_01_cache_token_observability.md`
- `plans/cache_compression_phase_02_canonical_request_segmentation.md`
- `plans/cache_compression_phase_03_transcoder_cache_stability.md`
- `plans/cache_compression_phase_04_observe_mode_compression_accounting.md`
- `plans/cache_compression_phase_05_safe_suffix_compression.md`
- `plans/cache_compression_phase_05_corrective_pass.md`
- `plans/cache_compression_phase_05_anthropic_tool_result_closure.md`

## Summary

Phases 1-5 establish cache/token observability, canonical segmentation, cache-stable transcoding, observe-mode compression accounting, and safe suffix compression. Phase 6 turns those primitives into an operator-controllable policy layer.

The goal is to let operators tune compression and cache-preservation behavior per client, provider, model, protocol, and request class while preserving the core safety invariant:

> By default, EggPool preserves stable prefixes, compresses only eligible volatile suffix string leaves, and never uses compression/cache metrics to skew same-provider account routing.

Phase 6 should not introduce new transforms. It should make existing controls explicit, composable, inspectable, and safe to deploy incrementally.

## Non-goals

- Do not add learned/semantic compression.
- Do not add response caching.
- Do not synthesize provider cache controls; that is Phase 9.
- Do not change routing scoring based on compression/cache economics; Phase 8 will add guardrails and explicit non-regression coverage.
- Do not make compression default-on.
- Do not allow stable-prefix compression without an explicit, highly visible override.
- Do not require operators to configure per-client policy to retain current behavior.

## Design principles

1. **Global defaults remain safe.** The default config should continue to behave like today: compression disabled or observe-only unless explicitly enabled.
2. **More specific policy wins.** Model/client/provider overrides should compose deterministically.
3. **Stable-prefix preservation is sticky.** No lower-level override should accidentally enable static-prefix compression unless explicitly set at that level.
4. **Every applied policy is observable.** Final request records should include the resolved policy name/source and high-level knobs that affected behavior.
5. **Per-client controls must be operational, not brittle.** Match by authenticated client identity, configured client name, request header, source protocol, or provider/model, but avoid requiring prompt inspection.
6. **SBC-safe implementation.** Policy resolution must be simple dictionary/pattern matching with no expensive runtime scanning.

## Proposed config shape

Keep the existing `[compression]` table as the global default. Add optional policy tables that override a subset of keys.

Example:

```toml
[compression]
enabled = false
mode = "observe" # observe | safe | balanced (future)
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25

[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true

[[compression.policies]]
name = "opencode-safe"
match_clients = ["opencode", "opencode-go"]
match_protocols = ["openai", "anthropic"]
enabled = true
mode = "safe"
placement = "suffix_only"
min_candidate_tokens = 1024
min_savings_tokens = 512
max_compression_latency_ms = 20

[[compression.policies]]
name = "anthropic-cache-preserve"
match_provider_kinds = ["anthropic"]
enabled = true
mode = "observe"
respect_cache_boundaries = true
compress_static_prefix = false

[[compression.policies]]
name = "no-compression-for-provider-x"
match_provider_ids = ["provider-x-0001"]
enabled = false
```

Supported match fields:

- `match_clients`: configured client names, auth labels, or stable client IDs.
- `match_provider_ids`: exact provider/account IDs.
- `match_provider_kinds`: provider implementation names/kinds.
- `match_models`: requested model IDs after model rewrite, if known.
- `match_requested_models`: client-supplied model IDs before rewrite.
- `match_protocols`: `openai` or `anthropic` source protocol.
- `match_transcoded`: `true` / `false`, optional.
- `match_routes`: optional route class if EggPool already exposes one.

Avoid regex initially unless an existing config subsystem already supports it. Exact strings and simple `*` suffix/prefix globbing are enough for Phase 6.

## Resolved policy model

Add a small immutable result model, for example:

```python
@dataclass(frozen=True, slots=True)
class ResolvedCompressionPolicy:
    name: str
    source: str # global | policy:<name> | disabled
    config: CompressionConfig
    matched_policy_names: tuple[str, ...]
    warnings: tuple[str, ...]
```

Resolution order:

1. Start with global `[compression]` config.
2. Evaluate `[[compression.policies]]` in file order.
3. Every matching policy overlays non-null fields onto the current config.
4. Later matching policies win for scalar values.
5. Transform overrides merge by field, not wholesale table replacement.
6. Validate the final config with the same safety rules as global config.
7. If validation fails, fail closed to global disabled/observe behavior and emit a warning.

For static-prefix compression:

- `compress_static_prefix = true` must be explicitly present in the matching policy that enables it.
- It must not be inherited implicitly from global config unless global config itself explicitly set it.
- Consider requiring a second flag such as `allow_static_prefix_compression = true` if current validation is too permissive.

## Request context inputs

Define the policy resolver inputs explicitly.

Suggested context:

```python
@dataclass(frozen=True, slots=True)
class CompressionPolicyContext:
    client_id: str | None
    client_name: str | None
    source_protocol: str
    target_protocol: str | None
    requested_model: str | None
    resolved_model: str | None
    provider_id: str | None
    provider_kind: str | None
    transcoded: bool
```

Populate context in `handle_proxy_request` after route/provider selection if provider-specific policy is needed. If compression currently happens before model rewrite/provider dispatch, split resolution:

- Pre-route policy: only client/source protocol/requested model controls.
- Post-route policy: provider/model controls.

Prefer a single resolution point immediately after route selection and before safe compression. If request segmentation currently happens before routing, keep segmentation before routing, but resolve policy and apply safe compression after route selection so provider-specific policy can apply. Preserve current behavior when no provider-specific policy is configured.

## Implementation tasks

### 1. Add config models

Extend `src/eggpool/models/config.py`:

- Add `CompressionPolicyOverride` model.
- Add `policies: list[CompressionPolicyOverride] = Field(default_factory=list)` to `CompressionConfig` or a sibling table.
- Ensure each override field is optional so absent keys do not reset global defaults.
- Reuse the existing transform config model with optional booleans for overrides.
- Validate policy names as non-empty and unique.

Validation rules:

- Unknown compression modes rejected.
- Unknown placements rejected.
- `compress_static_prefix=true` rejected in observe mode.
- `max_compression_latency_ms` must be positive and bounded.
- `min_candidate_tokens` and `min_savings_tokens` must be non-negative.
- Empty match policies are allowed only if named `default` or explicitly documented; otherwise warn or reject to avoid accidental global overrides.

### 2. Implement resolver module

Add `src/eggpool/transcoder/compression/policy_resolver.py`.

Responsibilities:

- Match context against overrides.
- Merge override fields into an effective `CompressionConfig`.
- Return `ResolvedCompressionPolicy`.
- Never inspect raw prompt/request content.
- Never raise on malformed policy; return global safe fallback plus warning.

### 3. Wire request path

Update `src/eggpool/api/proxy_request.py` or the existing request coordinator/finalizer path:

- Build `CompressionPolicyContext` from request metadata and route/provider metadata.
- Resolve compression policy before `analyze_compression` / `apply_safe_compression`.
- Use the resolved config for both analyzer and applier.
- Record the resolved policy name/source in request finalization metadata.

If the current code runs analyzer before routing, either:

- Keep analyzer pre-route with global/pre-route policy and run a second post-route eligibility check before applying compression, or
- Move analyzer after route selection so observe/apply use the same resolved policy.

Prefer one analyzer pass with the final resolved policy, unless moving it would disrupt stats semantics.

### 4. Persist resolved policy metadata

Add nullable columns via a new migration if not already present:

```sql
ALTER TABLE requests ADD COLUMN compression_policy_name TEXT;
ALTER TABLE requests ADD COLUMN compression_policy_source TEXT;
ALTER TABLE requests ADD COLUMN compression_policy_warnings_json TEXT;
```

Update repository/finalizer code to persist these values.

If schema churn is undesirable, include policy metadata in existing compression summary JSON first, then add columns later. Columns are better for dashboard filtering.

### 5. Stats/API/dashboard exposure

Expose policy rollups:

- Count by resolved policy name.
- Compression enabled/observe/safe distribution by policy.
- Candidate/applied/fallback counts by policy.
- Warning counts by policy.

Keep raw prompt content out of summaries.

### 6. Documentation

Update README/architecture docs:

- Explain global defaults.
- Explain policy match fields and precedence.
- Explain that provider-specific policy resolution may occur after routing but must not affect routing.
- Explain safety rails around stable-prefix compression.
- Include example configs for `opencode-safe`, `observe-only`, and `disable-for-provider`.

## Test plan

### Unit tests: config validation

- Empty/global config preserves existing defaults.
- Valid policy with `mode=safe` and suffix-only accepted.
- Duplicate policy names rejected.
- Invalid mode rejected.
- Invalid placement rejected.
- Negative thresholds rejected.
- Static-prefix compression in observe mode rejected.
- Transform overrides merge field-by-field.

### Unit tests: resolver matching

- No matching policy returns global config.
- Client match overrides global config.
- Provider ID match overrides client match if later in file.
- Provider kind match works.
- Requested model and resolved model matches are distinct.
- Protocol match works.
- Transcoded match works.
- Multiple matches merge deterministically in file order.
- Malformed policy fails closed and emits a warning.

### Integration tests: request path

- Safe policy for a test client applies compression.
- Observe policy for a different client records observations but does not mutate.
- Disable policy for provider ID prevents compression even when global safe is enabled.
- Provider-specific policy does not change selected provider/account.
- Same request under two policies records different policy names in finalization metadata.

### Routing non-regression

Add explicit tests proving policy resolution cannot influence account selection:

- Construct two same-provider accounts.
- Give one account a policy that would enable safe compression and the other one disabled, if matched post-route.
- Assert route selection happens according to existing fairness/health/quota logic, not policy savings or cache hit history.

If policy must be resolved after route selection for provider-specific matching, assert no retry/re-route occurs after compression unless the provider is unhealthy or the existing dispatch path already supports retries.

## Acceptance criteria

- Existing compression behavior is unchanged when no `[[compression.policies]]` entries exist.
- Operators can enable safe compression for one client without enabling it globally.
- Operators can disable compression for one provider/model without changing global defaults.
- Effective policy is deterministic and persisted or included in compression summary JSON.
- Observe and safe modes use the same resolved policy for a given request.
- Static-prefix compression remains blocked unless explicitly and deliberately enabled.
- Same-provider account routing fairness is unaffected.
- All new config/resolver/request-path tests pass.
- Full test suite, ruff, and pyright pass.

## Rollback notes

This phase should be rollback-safe by removing or ignoring `[[compression.policies]]` entries. If policy resolution causes issues in production, set global compression disabled:

```toml
[compression]
enabled = false
```

Do not remove added nullable DB columns during rollback; leave them unused.