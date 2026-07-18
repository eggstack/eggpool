# Deep Dive: Cache & Compression

Back to [Overview](overview.md)

## Purpose

A 12-phase cache-preserving deterministic compression stack that observes, analyzes, and optionally mutates request bodies to improve cache hit rates and reduce token costs. All phases are observational by default; mutation requires explicit operator opt-in.

## Architecture

```
┌─────────────────────────────────────┐
│         Request Body                 │
└──────────────┬──────────────────────┘
               │
    ┌──────────▼──────────┐
    │ Phase 2: Segmentation│
    │ stable/semi/volatile │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Phase 3: Cache      │
    │ Stability Tracker   │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Phase 4: Observe    │
    │ Compression Analyze │
    │ (no mutation)       │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Phase 5: Safe Mode  │
    │ Compression Apply   │
    │ (opt-in mutation)   │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Phase 6: Policy     │
    │ Overrides           │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Phase 9: Synthetic  │
    │ Cache Controls      │
    │ (opt-in annotation) │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Phase 10: Threshold │
    │ Tuning              │
    │ (recommendation)    │
    └─────────────────────┘
```

## Key Modules

### `transcoder/segmentation.py`

`segment_request()` — structural segmentation into:
- **`stable_prefix`** — system/developer messages, tool schemas, cache_control (protected)
- **`semi_stable_context`** — assistant messages, prior turns (conservative default)
- **`volatile_suffix`** — tool results, command output, latest user turn (compressible)

Content-private: hashes use structural descriptors, never raw text.

### `transcoder/cache_stability.py`

`CacheBoundaryTracker` — records cache_control annotation events during transcoding. Append-only, bounded (64/request).

### `transcoder/compression/analyzer.py`

`analyze_compression()` — observe-mode analysis:
- Walks every segment, runs enabled transforms
- Produces `CompressionCandidate` per transform
- Policy filtering with reason codes
- Latency budget check
- Never mutates request body

### `transcoder/compression/apply.py`

`apply_safe_compression()` — safe-mode deterministic mutation:
- Only mutates eligible `volatile_suffix` segments
- Path-level copy-on-write (no full deep copy)
- Pre/post stable-prefix content hash verification
- Fail-closed on hash mismatch
- Deterministic markers for each transform

### `transcoder/compression/policy.py`

`CompressionConfig`, `CompressionTransforms` — configuration:
- `enabled`, `mode` (observe/safe), `placement`
- `respect_cache_boundaries`, `compress_static_prefix`
- `min_candidate_tokens`, `min_savings_tokens`
- `max_compression_latency_ms`
- Six transform toggles

### `transcoder/compression/policy_resolver.py`

`resolve_compression_policy()` — per-request policy override resolution:
- Walks `[[compression.policies]]` overrides in file order
- Match fields: client, protocol, model, provider
- Merges overlay fields onto base config
- Never raises; falls back on validation error

### `transcoder/compression/markers.py`

Deterministic compression markers:
```
[EggPool compression: <transform> | segment=<id> | lines=<n> | tokens=<n> | sha256=<digest>]
```

### `transcoder/compression/tuning.py`

Phase 10 closed-loop threshold tuning:
- `compute_recommendation()` — advisory suggestions
- `apply_runtime_override()` — dormant (no production path yet)
- `RuntimeCompressionPolicyOverrideRegistry` — forward compatibility

### `transcoder/cache_synthesis.py` / `cache_synthesis_policy.py`

Phase 9 synthetic cache controls:
- Post-route, provider-bound `cache_control` annotations
- Dry-run by default when enabled
- Structural-diff safety validation
- Anthropic-style `ephemeral` TTL only

## Compression Transforms

| Transform | Description |
|-----------|-------------|
| `fold_repeated_lines` | Collapse repeated log lines |
| `compact_logs` | Compact log output |
| `compact_search_results` | Compact search results |
| `elide_base64_blobs` | Elide base64 encoded data |
| `minify_machine_json` | Minify JSON output |
| `compact_stack_traces` | Compact stack traces |

## Routing Non-Interference

Hardcoded invariant: `QuotaFairScorer` never consumes cache/compression fields. Routing stays load-based:
- Request count
- Token count
- Active count
- Health

Runtime diagnostic flags (exposed via `/api/stats/runtime`):
```json
{
  "routing_cache_compression_mode": "reporting_only",
  "routing_uses_cache_metrics": false,
  "routing_uses_compression_metrics": false,
  "routing_uses_stable_prefix_hash": false,
  "routing_uses_compression_policy": false
}
```

## What Is Safe by Default

With shipped defaults, the entire stack is observability-only:
- Phase 1 cache counters recorded, never affects quota scoring
- Phase 2 segmentation annotates durable columns
- Phase 3 cache stability records boundary events
- Phase 4 observe mode runs analyzer, never mutates
- Phase 5 safe compression defaults to `mode = "observe"`
- Phase 6 policy overrides default to `policies = []`
- Phase 9 synthetic cache defaults to `enabled = false`
- Phase 10 tuning defaults to `enabled = false`

## What Is Experimental

Behind explicit operator opt-in:
- Phase 5 `mode = "safe"` — mutates eligible volatile-suffix segments
- Phase 9 synthetic cache `apply` mode — adds cache_control annotations
- Phase 10 `mode = "apply"` — accepted but currently dormant

## Key Invariants

- Segmentation is observational: never affects request bodies, routing, or eligibility
- `analyze_compression` is total: never raises on malformed input
- `apply_safe_compression` is total: never raises; failures surface as `failed_fallback=True`
- Pre/post stable-prefix content hash MUST match when `compress_static_prefix` is False
- Context-limit checks happen before compression
- `QuotaFairScorer` never consumes cache/compression fields
- Same-provider fairness preserved across adversarial cache/compression profiles
- No raw prompts in any cache/compression surface
