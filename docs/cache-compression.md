# Cache and compression

EggPool keeps provider cache behavior explicit and provider-bound. Native
cache boundaries are translated only when the selected provider/model contract
declares the target field, supported TTL, and boundary limit. Generic
OpenAI-compatible endpoints do not receive guessed cache fields.

Compression is optional and has two modes:

- `observe` records bounded opportunity metrics without changing the request.
- `safe` applies deterministic transforms only to eligible `volatile_suffix`
  segments. Placement is always `suffix_only`; stable prefixes and protected
  cache boundaries are never mutated.

`[[compression.policies]]` can select different observe/safe thresholds by
client, protocol, provider, or model. Policy resolution is deterministic and
content-private. There is no synthetic cache insertion or recommendation-only
tuning runtime surface.

Routing remains load-based. Cache and compression metrics are reporting-only
and never enter `QuotaFairScorer`. The `/cache` dashboard page and these JSON
endpoints expose bounded diagnostics:

- `/api/stats/cache-observability`
- `/api/stats/cache-stability`
- `/api/stats/compression-observability`
- `/api/stats/compression-runtime`
- `/api/stats/compression-policies`
- `/api/stats/request-shaping`

The shipped configuration leaves compression disabled. Enable it deliberately
after reviewing provider cache contracts and the bounded request-shaping
metrics. See [cache-compression-profiles.md](cache-compression-profiles.md)
for supported examples and [cache-compression-troubleshooting.md](cache-compression-troubleshooting.md)
for diagnosis.
