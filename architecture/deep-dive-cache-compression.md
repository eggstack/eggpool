# Deep Dive: Cache and Compression

## Scope

This subsystem reports provider cache counters, preserves explicit native cache
boundaries during transcoding, and optionally performs deterministic compression
on safe request suffixes. It does not synthesize cache controls, run a tuning
registry, or maintain a custom DNS cache.

## Modules

- `transcoder/segmentation.py` classifies canonical request content into stable,
  semi-stable, and volatile regions without retaining request content.
- `transcoder/cache_stability.py` tracks explicit native cache-boundary mapping
  and bounded loss metadata.
- `transcoder/compression/analyzer.py` records bounded observe-mode opportunity
  metrics.
- `transcoder/compression/apply.py` applies safe deterministic transforms with
  path-level copy-on-write.
- `transcoder/compression/policy.py` defines the `observe`/`safe` policy and
  `suffix_only` placement. Unknown fields are rejected.
- `transcoder/compression/policy_resolver.py` applies deterministic,
  content-private policy overrides.

## Runtime behavior

Compression is disabled by default. Observe mode does not mutate provider
payloads. Safe mode considers only eligible `volatile_suffix` segments and
never mutates stable prefixes or protected cache-boundary content. A changed
stable-prefix integrity hash fails closed and returns the original payload.

Native cache fields are provider/model contract data. Protocol names alone do
not authorize cache fields, TTLs, or breakpoint counts. The transcoder preserves
explicit source boundaries only when the selected target contract supports the
mapping.

## Routing and persistence

Cache and compression metrics are reporting-only and are not inputs to
`QuotaFairScorer`. Request finalization persists bounded compression and cache
observability fields. Historical synthetic-cache migration columns remain in
the frozen `requests` schema for compatibility, but current code does not
populate or expose them.

## Invariants

- Config placement is exactly `suffix_only`.
- Compression never inspects or logs raw request content.
- Provider cache capabilities are explicit; no fields are guessed.
- Local transformation failures never trigger provider retry.
- Diagnostics contain counts, hashes, and bounded reason codes only.
