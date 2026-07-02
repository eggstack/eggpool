# Phase 9 Plan: Opt-In Synthetic Provider Cache Controls

Date: 2026-07-02

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Depends on:

- Phase 1 cache/token observability
- Phase 2 canonical request segmentation
- Phase 3 transcoder cache stability
- Phase 5 safe suffix compression
- Phase 6 policy controls
- Phase 8 routing guardrails

## Summary

Earlier phases preserve provider-native cache controls when present and keep compression boundary-aware. Phase 9 adds opt-in synthetic cache controls for providers/protocols that support explicit cache boundary hints, primarily Anthropic-style `cache_control` annotations.

This phase must be conservative. Synthetic cache controls change provider-visible request bodies. They can improve cache reuse for clients that do not natively emit cache annotations, but they can also change billing behavior and provider semantics. Therefore synthetic cache controls must be disabled by default, policy-gated, auditable, and never used to influence routing by default.

Core rule:

> Synthesize cache controls only around stable, protected, provider-cacheable regions that EggPool already knows how to preserve exactly. Never synthesize around volatile suffix content or compressed content.

## Non-goals

- Do not synthesize cache controls by default.
- Do not synthesize cache controls for providers/protocols without explicit support.
- Do not alter routing based on expected cache hits.
- Do not compress stable prefixes to improve synthetic cache placement.
- Do not synthesize cache controls into OpenAI payloads unless OpenAI-compatible supported fields are explicitly modeled later.
- Do not guess semantic cache boundaries from raw prompt text.
- Do not exceed provider cache-control limits.

## Provider scope

Initial target:

- OpenAI-compatible client request routed to Anthropic-compatible provider through the existing transcoder.
- Anthropic-compatible client request to Anthropic-compatible provider when client did not provide cache controls.

Initial non-targets:

- Anthropic -> OpenAI synthetic mapping.
- Gemini-specific cache controls.
- OpenAI prompt cache key/retention handling unless the repo already has first-class support and tests.
- Any provider where cache controls are undocumented or unknown.

## Config shape

Add under existing `[cache]` or `[transcoder]` config, preferably `[cache]` if it exists from the roadmap.

Suggested config:

```toml
[cache]
mode = "preserve" # off | preserve | optimize
synthetic_cache_controls = false
synthetic_cache_control_provider_kinds = ["anthropic"]
synthetic_cache_control_ttl = "ephemeral" # provider-specific; initially Anthropic ephemeral only
synthetic_cache_min_stable_tokens = 1024
synthetic_cache_max_breakpoints = 4
synthetic_cache_require_policy = true
synthetic_cache_dry_run = true
```

Per-policy override from Phase 6:

```toml
[[compression.policies]]
name = "anthropic-cache-controls-for-opencode"
match_clients = ["opencode"]
match_provider_kinds = ["anthropic"]
synthetic_cache_controls = true
synthetic_cache_dry_run = false
```

If mixing cache controls into compression policies feels wrong, create `[[cache.policies]]` with the same match context. Prefer reuse of the Phase 6 policy resolver mechanics.

## Boundary selection strategy

The segmenter already identifies:

- `stable_prefix`: system/developer instructions, tools, cache-control blocks, thinking blocks where protected, persistent prefix content.
- `semi_stable_context`: rolling history.
- `volatile_suffix`: latest tool results/logs/search/results.

Synthetic cache-control placement should only consider stable-prefix segments.

Initial placement algorithm:

1. Run segmentation.
2. Collect stable-prefix segments with concrete paths that provider/transcoder can represent as Anthropic cacheable blocks.
3. Exclude any segment already carrying provider-native `cache_control`.
4. Exclude stable segments below `synthetic_cache_min_stable_tokens` unless combined with adjacent stable segments.
5. Choose at most `synthetic_cache_max_breakpoints` cache boundaries.
6. Prefer the last stable prefix block before semi-stable/volatile content.
7. Do not annotate compressed volatile suffix content.
8. Do not move content solely to improve cacheability.

For Anthropic, the practical cache breakpoint is often a `cache_control` annotation on a content block or tool definition. Respect provider limits.

## Transcoder integration

Phase 3 introduced cache-stability tracking and loss accounting. Phase 9 should extend that machinery rather than bypassing it.

Add a synthetic boundary status, if not already reserved:

```python
CacheBoundaryStatus.SYNTHESIZED = "synthesized"
```

For each synthetic annotation, record:

- source: `synthetic`.
- reason: `stable_prefix_candidate` / `tool_schema_candidate` / `system_candidate`.
- provider target.
- original path.
- provider-visible path after transcoding.
- policy name/source.
- dry-run versus applied.

Loss/warning codes:

- `synthetic_cache_control_disabled`
- `synthetic_cache_control_dry_run`
- `synthetic_cache_control_provider_unsupported`
- `synthetic_cache_control_no_stable_candidate`
- `synthetic_cache_control_below_min_tokens`
- `synthetic_cache_control_limit_reached`
- `synthetic_cache_control_synthesized`
- `synthetic_cache_control_existing_native_preserved`

## Dry-run mode

Dry-run mode should be implemented first.

When `synthetic_cache_dry_run = true`:

- Do not mutate provider-bound body.
- Run candidate selection.
- Record what would have been annotated.
- Surface counts in dashboard/API.
- Emit warnings/reasons.

This lets operators assess whether synthetic controls would apply before changing billing/provider behavior.

## Apply mode

When dry-run is false and synthetic controls are enabled:

- Mutate only provider-bound payload after transcoding target format is known.
- Add cache controls only to supported fields/blocks.
- Recompute provider-visible stable-prefix hash after mutation.
- Record synthetic boundary tracker entries.
- Preserve native cache controls and never duplicate them on the same block.
- If mutation fails validation, send original provider-bound payload without synthetic controls and record fallback warning.

## Implementation tasks

### 1. Add config and policy fields

Add cache config fields and optional per-policy overrides.

Validation:

- `synthetic_cache_controls=false` by default.
- `synthetic_cache_dry_run=true` by default if synthetic enabled globally for the first release.
- Max breakpoints > 0 and <= provider maximum.
- TTL values limited to known provider-supported options.

### 2. Add candidate selector

Create `src/eggpool/transcoder/cache_synthesis.py` or similar.

Inputs:

- payload,
- protocol/source protocol,
- target protocol/provider kind,
- segmentation,
- resolved cache policy,
- transcoder cache boundary tracker.

Outputs:

```python
@dataclass(frozen=True, slots=True)
class SyntheticCachePlan:
    status: str
    dry_run: bool
    candidates: tuple[SyntheticCacheCandidate, ...]
    applied_count: int
    warnings: tuple[str, ...]
```

### 3. Add Anthropic mutator

Implement a minimal mutator for Anthropic-compatible payloads.

Supported placements:

- top-level `system` list text blocks,
- `tools[]` definitions if provider supports tool cache control and current transcoder supports it,
- possibly final stable-prefix message text block if represented in Anthropic Messages.

If top-level `system` is a string and synthetic cache controls require block annotations, either:

- convert to block form only if existing transcoder semantics already support string-to-block equivalence, or
- skip with a warning in Phase 9 to avoid subtle behavior changes.

Prefer skipping conversion initially unless already proven safe.

### 4. Integrate with transcoder cache tracker

Record synthetic boundaries through the same tracker used in Phase 3.

Ensure dashboard can distinguish:

- native preserved,
- native dropped,
- synthetic dry-run candidate,
- synthetic applied.

### 5. Persist metadata

Add columns or include in cache-stability summary JSON:

- `synthetic_cache_status`.
- `synthetic_cache_candidate_count`.
- `synthetic_cache_applied_count`.
- `synthetic_cache_warning_count`.
- `synthetic_cache_summary_json`.

Prefer JSON summary first if dashboard can aggregate cheaply; add columns for counts if needed.

### 6. Dashboard/API updates

Extend Phase 7 cache-stability cards:

- dry-run candidates,
- applied synthetic controls,
- provider unsupported counts,
- below-threshold counts,
- existing native controls preserved.

### 7. Documentation

Document:

- synthetic cache controls are disabled by default,
- dry-run behavior,
- billing implications,
- supported providers/protocols,
- how to verify with cache-read/write counters,
- how to disable quickly.

## Test plan

### Unit tests: candidate selection

- No stable prefix -> no candidate.
- Stable prefix below min tokens -> suppressed.
- Existing native cache control -> preserved, not duplicated.
- Stable system block -> candidate.
- Stable tool schema -> candidate if supported.
- Volatile suffix -> never candidate.
- Compressed volatile content -> never candidate.
- Breakpoint limit enforced.

### Unit tests: dry-run

- Dry-run returns candidates but does not mutate payload.
- Dry-run records warnings/reasons.
- Dry-run summary has no raw prompt content.

### Unit tests: Anthropic apply

- Adds `cache_control` to supported Anthropic block.
- Does not duplicate existing `cache_control`.
- Does not convert unsupported shapes unless explicitly allowed.
- Unsupported provider returns provider-unsupported warning and no mutation.
- Mutation failure falls back without synthetic controls.

### Integration tests

- OpenAI client -> Anthropic provider can synthesize in dry-run.
- Anthropic client -> Anthropic provider can synthesize in dry-run/apply.
- Anthropic native cache controls are preserved and not duplicated.
- Synthetic controls are recorded in cache-boundary tracker.
- Compression remains suffix-only and does not compress synthetic-controlled stable prefix.
- Routing remains unchanged.

## Acceptance criteria

- Synthetic cache controls are disabled by default.
- Dry-run mode works and is observable without mutating provider-bound requests.
- Apply mode only mutates supported Anthropic-compatible stable-prefix blocks.
- Native cache controls are preserved and not duplicated.
- Volatile suffix and compressed content never receive synthetic cache controls.
- Boundary tracker records synthetic events.
- Dashboard/API can show dry-run/applied counts.
- Routing remains unaffected.
- Tests pass.

## Rollback notes

Disable synthetic cache controls globally:

```toml
[cache]
synthetic_cache_controls = false
```

If apply mode causes billing or provider issues, switch to dry-run:

```toml
[cache]
synthetic_cache_controls = true
synthetic_cache_dry_run = true
```

Leave nullable metadata columns in place during rollback.