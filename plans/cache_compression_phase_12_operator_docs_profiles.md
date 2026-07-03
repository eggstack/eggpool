# Phase 12 Plan: Operator Documentation, Profiles, and Rollout Guide

Date: 2026-07-03

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Depends on:

- Phase 1 cache/token observability
- Phase 2 canonical request segmentation
- Phase 3 transcoder cache stability
- Phase 4 observe-mode compression accounting
- Phase 5 safe suffix compression
- Phase 6 policy controls
- Phase 7 dashboard/runtime views
- Phase 8 routing guardrails
- Phase 9 synthetic cache controls
- Phase 10 threshold tuning recommendations
- Phase 11 replay fixtures and regression tests

## Summary

Phase 12 turns the cache-preserving deterministic compression line of work into an operator-facing feature set. The repo now has enough primitives and tests; the remaining gap is usability. Operators need clear profiles, safe defaults, dashboard interpretation guidance, and rollback instructions.

This phase should produce practical documentation and config examples for EggPool's intended deployment shape: local/LAN routing for coding agents, often on small SBCs, with multiple same-provider subscriptions, provider transcoding, SQLite accounting, and conservative request shaping.

The documentation must be explicit about what is safe by default, what is experimental, and what never affects routing.

## Non-goals

- Do not change runtime behavior unless documentation uncovers a small mismatch.
- Do not enable compression or synthetic cache controls by default.
- Do not create a marketing document.
- Do not document unsupported provider cache features as if they work.
- Do not imply Phase 10 apply mode is live.
- Do not publish raw example prompts copied from real usage.

## Documentation inventory

Review and update:

- `README.md`
- `architecture/README.md`
- `AGENTS.md`
- `.opencode/skills/architecture/SKILL.md`
- `config.example.toml`
- `src/eggpool/_share/config.example.toml`
- dashboard/runtime docs if present
- `docs/transcoding.md`
- any CLI docs that mention config setup or runtime stats

Add new docs if useful:

```text
docs/cache-compression.md
docs/cache-compression-profiles.md
docs/cache-compression-troubleshooting.md
```

Prefer one main doc plus profile/troubleshooting sections unless docs are becoming too large.

## Core operator model

Document the feature stack in plain operational terms:

1. **Observe cache counters.** Providers may report cached token counters. Unknown is not zero.
2. **Segment requests.** EggPool classifies stable prefix, semi-stable context, and volatile suffix without mutating payloads.
3. **Preserve provider cacheability.** Transcoding and compression should not disturb stable prefixes or native cache controls.
4. **Observe compression opportunities.** Observe mode estimates deterministic suffix-compression savings without mutation.
5. **Apply safe suffix compression.** Safe mode mutates only eligible volatile suffix string leaves and fails closed if stable prefix changes.
6. **Apply policy controls.** Operators can scope behavior by client/protocol/model/provider/policy.
7. **Inspect runtime views.** Dashboard/API expose counters and warnings without raw prompts.
8. **Keep routing separate.** Cache/compression/synthetic/tuning metrics never affect same-provider routing by default.
9. **Synthetic cache controls.** Optional, dry-run-first, Anthropic-only, post-route provider-bound mutation.
10. **Threshold tuning.** Recommendation-only. No automatic runtime override today.

## Profiles

Add documented configuration profiles. These should be copy-pasteable snippets with comments.

### Profile 1: Baseline / disabled

Purpose: preserve current behavior while collecting only existing request stats.

```toml
[compression]
enabled = false
mode = "observe"

[cache.synthetic_cache_controls]
enabled = false
dry_run = true

[compression.tuning]
enabled = false
mode = "recommend"
```

Explain: no request mutation from compression or synthetic cache controls.

### Profile 2: Observe-only diagnostics

Purpose: learn whether compression would help without mutating requests.

```toml
[compression]
enabled = true
mode = "observe"
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 2048
min_savings_tokens = 1024

[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true
```

Explain dashboard fields:

- candidate requests;
- estimated savings;
- suppressed reasons;
- analyzer latency;
- no request mutation.

### Profile 3: Safe suffix compression for coding agents

Purpose: reduce large volatile tool/log/search output while preserving stable prompts and cache controls.

```toml
[compression]
enabled = true
mode = "safe"
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 1024
min_savings_tokens = 512
max_compression_latency_ms = 25
```

Add example policy scoped to Opencode-like clients:

```toml
[[compression.policies]]
name = "opencode-safe-suffix"
match_clients = ["opencode"]
enabled = true
mode = "safe"
placement = "suffix_only"
min_candidate_tokens = 1024
min_savings_tokens = 512
```

Explain: this does not alter route selection and does not compress system/tool schema prefixes.

### Profile 4: Anthropic synthetic cache dry-run

Purpose: discover whether provider-bound Anthropic requests have stable prefix blocks that could be annotated.

```toml
[cache.synthetic_cache_controls]
enabled = true
dry_run = true
require_policy = true
ttl = "ephemeral"
min_stable_tokens = 1024
max_breakpoints = 4
placements = ["system", "tools"]

[[compression.policies]]
name = "anthropic-cache-dry-run"
match_provider_kinds = ["anthropic"]
synthetic_cache_controls = true
synthetic_cache_dry_run = true
synthetic_cache_min_stable_tokens = 1024
synthetic_cache_max_breakpoints = 4
```

Explain:

- post-route only;
- provider-bound Anthropic payload only;
- no mutation in dry-run;
- use dashboard synthetic-cache status before apply.

### Profile 5: Anthropic synthetic cache apply mode

Purpose: opt-in provider-bound mutation after dry-run confidence.

```toml
[cache.synthetic_cache_controls]
enabled = true
dry_run = false
require_policy = true
ttl = "ephemeral"
min_stable_tokens = 1024
max_breakpoints = 4
placements = ["system", "tools"]

[[compression.policies]]
name = "anthropic-cache-apply"
match_provider_kinds = ["anthropic"]
synthetic_cache_controls = true
synthetic_cache_dry_run = false
synthetic_cache_min_stable_tokens = 1024
synthetic_cache_max_breakpoints = 4
```

Add strong warnings:

- apply mode mutates provider-bound payload by adding `cache_control` keys only;
- native cache controls preserved;
- volatile content never annotated;
- leave disabled or dry-run if provider behavior/billing is uncertain.

### Profile 6: Threshold tuning recommendation-only

Purpose: surface suggested threshold changes but never alter runtime policy.

```toml
[compression.tuning]
enabled = true
mode = "recommend"
window_requests = 500
min_window_requests = 50
update_interval_s = 300
max_adjustment_pct = 25
cooldown_s = 900
persist_recommendations = true
```

Explain: `mode="apply"` is currently dormant/reserved; no background task registers runtime overrides today.

## Dashboard interpretation guide

Create a table or prose section for each dashboard card/API endpoint.

### Cache observability

Explain:

- Reported cache counters mean provider surfaced cache data.
- Missing/unknown counters are not zero.
- Known-only cache ratio excludes unreported rows.

### Segmentation

Explain:

- Stable prefix estimated tokens.
- Semi-stable context estimated tokens.
- Volatile suffix estimated tokens.
- Request shape hash vs stable-prefix structural hash vs exact stable-prefix content hash.

### Compression observability

Explain:

- Candidate count.
- Eligible vs suppressed count.
- Reason codes.
- Estimated savings.
- Analyzer latency.

### Safe compression runtime

Explain:

- Applied count.
- Actual savings.
- Stable prefix preserved.
- Failed fallback.
- Transform counts.
- Latency p95/max.

### Policy rollups

Explain:

- `<global>` sentinel.
- `policy:<name>` source.
- Warning counts.
- Matched policy order / last scalar wins.

### Synthetic cache controls

Explain:

- disabled;
- policy_required;
- provider_unsupported;
- no_candidates;
- dry_run;
- applied;
- failed_fallback if implemented in summary/status.

Explain warning codes:

- `synthetic_cache_control_disabled`
- `synthetic_cache_control_dry_run`
- `synthetic_cache_control_provider_unsupported`
- `synthetic_cache_control_no_stable_candidate`
- `synthetic_cache_control_below_min_tokens`
- `synthetic_cache_control_limit_reached`
- `synthetic_cache_control_existing_native_preserved`
- `synthetic_cache_control_safety_diff_failed`

### Tuning recommendations

Explain:

- recommendation-only;
- insufficient data;
- high latency warning rate;
- high fallback rate;
- low positive savings rate;
- strong savings low latency;
- cooldown active.

## Troubleshooting guide

Add concrete symptom-to-cause sections.

### Symptom: compression never applies

Check:

- `[compression] enabled=true`.
- `mode="safe"` for mutation.
- request has volatile suffix candidates.
- candidates exceed `min_candidate_tokens` and `min_savings_tokens`.
- transforms are enabled.
- stable-prefix preservation did not fail closed.
- context-limit check did not reject before compression.

### Symptom: observe mode sees candidates but safe mode does not mutate

Check:

- policy mode is safe for that client/provider.
- candidate paths resolve to string leaves.
- latency budget not exceeded.
- placement is suffix-only and segment is volatile suffix.
- transform-specific thresholds.

### Symptom: synthetic cache controls show provider_unsupported

Check:

- selected provider kind is Anthropic.
- target protocol is Anthropic after routing/transcoding.
- request actually routes to Anthropic provider.
- provider kind metadata exists.
- policy matches post-route provider kind.

### Symptom: synthetic cache controls show policy_required

Check:

- `require_policy=true` means a matching policy must set `synthetic_cache_*` fields.
- provider-specific policy matchers only fire post-route.
- client/model/protocol matches use exact strings.

### Symptom: synthetic cache controls show no_candidates

Check:

- stable prefix below `min_stable_tokens`.
- placements exclude available stable blocks.
- native cache controls already present.
- payload shape is unsupported.

### Symptom: failed_fallback

Check:

- structural diff reported a mutation outside allowed cache-control additions.
- native cache controls were unexpectedly touched.
- transform/mutator bug.

### Symptom: routing seems uneven

Explain:

- cache/compression metrics do not affect routing;
- inspect health state, active request count, quota windows, model eligibility, missing account catalog support, and stale account refresh;
- do not use cache hit ratio as a routing diagnostic unless a future explicit cache-aware mode exists.

## Rollout guide

Recommended staged rollout:

1. Baseline disabled.
2. Enable observe-only compression for 24-48 hours.
3. Inspect dashboard: candidate rate, estimated savings, analyzer latency, suppressed reasons.
4. Enable safe suffix compression for one client/policy.
5. Inspect stable-prefix preserved count and failed fallback count.
6. Expand safe compression to additional clients if stable.
7. Enable synthetic cache dry-run for Anthropic providers only.
8. Inspect candidate/applied dry-run counts and native-preserved warnings.
9. Enable synthetic apply for one Anthropic provider/client only if dry-run is clean.
10. Keep Phase 10 tuning in recommendation-only mode.

## Safety and privacy section

Document what is never shown/persisted:

- raw prompts;
- raw tool outputs;
- auth headers;
- provider API keys;
- request bodies;
- provider response bodies in cache/compression summaries.

Document what is shown:

- token counts;
- byte counts;
- hashes;
- structural paths;
- warning codes;
- policy names;
- status counters.

## Config validation notes

Document:

- only `ttl="ephemeral"` is supported for synthetic cache controls;
- static-prefix compression remains blocked unless explicitly allowed;
- `mode="apply"` for tuning is reserved/dormant today;
- `compress_static_prefix=false` is the normal setting;
- `context-limit` checks currently happen before compression.

## CLI/configsetup implications

If `eggpool configsetup` or generated configs include compression sections, ensure they default to safe disabled/observe snippets. Do not generate synthetic apply configs by default.

Recommended configsetup additions:

- `--compression-profile baseline`
- `--compression-profile observe`
- `--compression-profile safe-coding-agent`
- `--synthetic-cache-profile off`
- `--synthetic-cache-profile anthropic-dry-run`

Only add these flags if configsetup already supports profile-like generation. Otherwise document manual snippets only.

## Acceptance criteria

- Main docs explain phases 1-12 in operator terms.
- Config examples include baseline, observe, safe suffix, synthetic dry-run, synthetic apply, and tuning recommendation-only profiles.
- Docs state synthetic cache is disabled by default and dry-run by default.
- Docs state synthetic cache runs post-route on provider-bound Anthropic payloads.
- Docs state TTL support is `ephemeral` only.
- Docs state Phase 10 apply lifecycle is dormant/reserved.
- Dashboard/API guide explains all major counters and warning codes.
- Troubleshooting guide covers common no-op and fallback cases.
- Rollout guide gives a conservative sequence.
- Privacy section clearly states no raw prompts/tool outputs/auth data are exposed.
- Full tests, docs lint if present, ruff, and pyright pass.

## Rollback notes

Operator rollback should be a documented config-only change:

```toml
[compression]
enabled = false
mode = "observe"

[cache.synthetic_cache_controls]
enabled = false
dry_run = true

[compression.tuning]
enabled = false
mode = "recommend"
```

No schema rollback is required; added columns/tables are additive audit surfaces.