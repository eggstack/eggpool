# Model-router configuration reference

Model routers are disabled unless at least one `[model_routers.<id>]` table is
configured. The mapping is validated structurally and compiled into each
runtime generation; it does not add a database migration, background task, or
provider client.

## Fields

`<id>` is the exact client-visible virtual model alias. It must be non-empty,
at most 128 UTF-8 bytes, free of control characters, and contain no `/`.

| Path | Type | Default | Validation |
|---|---|---:|---|
| `selector_model` | string | — | Required concrete reference, max 128 UTF-8 bytes |
| `default_model` | string | — | Required concrete reference and must match a route model |
| `routes.<label>.model` | string | — | Required concrete reference, max 128 UTF-8 bytes |
| `routes.<label>.description` | string | — | Required, max 512 UTF-8 bytes |
| `sticky` | boolean | `true` | Controls process-local affinity |
| `affinity_ttl_s` | float | `43200` | Inclusive range 1–604800 seconds |
| `selector_timeout_s` | float | `2.0` | Inclusive range 0.05–30 seconds |
| `max_input_bytes` | integer | `2048` | Inclusive range 128–16384 bytes |
| `repair_attempts` | integer | `1` | Only `0` or `1` |

At least one route is required. Route labels are non-empty, max 128 UTF-8
bytes, and must not contain control characters. Descriptions are normalized
for repeated ASCII whitespace during compilation. The compiled policy is
limited to 64 KiB. A selector or route target may use the normal
`model/provider` reference syntax; do not add a separate provider key.

Nested virtual aliases are rejected. Configuration validation deliberately does
not query the current catalog, so a provider/account change can be staged and
then checked through the ordinary request-time availability path.

## Reload and exposure

`model_routers` is one `LIVE` reload field. Rehash builds and validates the
complete registry before publication. The semantic fingerprint includes the
selector/default, routes, descriptions, and policy controls. Unchanged
fingerprints retain process-local affinity; changed fingerprints reclassify
the next request; removed aliases are no longer reachable. A process restart
clears affinity intentionally.

Configured aliases are appended to `/v1/models` as compact objects owned by
`eggpool`. They advertise only `virtual` and `model_router` metadata, not
context limits, capabilities, prices, route descriptions, or selector state.
If a virtual alias collides with an unsuffixed concrete catalog ID, the virtual
alias wins that exact exposure and the provider-qualified concrete entries
remain available.

See [Semantic Model Routing](model-routing.md) for examples and operator
guidance.
