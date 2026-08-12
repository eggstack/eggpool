# Cache and compression troubleshooting

Start with `GET /api/stats/request-shaping` and the compression observability
and runtime endpoints. These surfaces contain bounded counters only; request
content and credentials are not persisted.

Common outcomes:

- `mode = off`: compression is disabled by configuration.
- `mode = observe`: opportunities are recorded but the provider request is
  unchanged.
- `mode = safe`: only eligible volatile-suffix segments may change.
- `failed_fallback_count > 0`: safe compression failed its integrity check or
  encountered an unexpected local error; the original payload was retained.
- `policy_warning_count > 0`: a policy override was malformed or could not be
  applied; the global compression policy was used.
- `cache_counter_status = not_reported`: the provider did not return a
  recognized cache counter shape. This is not evidence of a cache miss.

If native cache fields are absent after transcoding, inspect the selected
provider/model capability contract. EggPool does not infer support from a
generic protocol name and does not synthesize cache breakpoints.

If an obsolete `[cache]`, `[compression.tuning]`, DNS-cache, static-prefix, or
non-`suffix_only` placement setting is present, remove it. Configuration uses
`extra = "forbid"` and fails clearly so stale operational profiles cannot be
silently ignored.
