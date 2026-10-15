# Model Context Limits

EggPool supports configurable effective context limits for individual models on individual providers. This lets operators advertise a smaller context window than the provider physically supports.

## Why Effective Limits Exist

Some models support very large context windows (e.g., 1M tokens), but the cost and latency at that scale can be prohibitive. An operator may want to cap the advertised context window so that OpenCode's compaction machinery triggers earlier, keeping requests in a more efficient regime.

## Configuration

### Global Model Limits

Apply to all providers unless overridden per-provider:

```toml
[model_overrides."model-id"]
max_context_tokens = 200000
max_input_tokens = 180000
max_output_tokens = 16384
enforce_context_limit = true
```

### Provider-Specific Model Limits

Override global limits for a specific provider:

```toml
[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
max_output_tokens = 16384
enforce_context_limit = true
```

### Precedence

Each field is resolved independently:

1. Provider-specific override
2. Global override
3. Upstream-reported metadata
4. Unknown (None)

### Cross-Field Validation

- `max_input_tokens` must not exceed `max_context_tokens`
- `max_output_tokens` must not exceed `max_context_tokens`

## Model ID Matching

Override keys match the **base model ID**, not the provider-suffixed exposed ID:

```toml
# Correct: base model ID
[providers.opencode-go.model_overrides."MiniMax-M3"]

# Wrong: provider-suffixed ID
[providers.opencode-go.model_overrides."MiniMax-M3/opencode-go"]
```

## Unsuffixed vs Provider-Suffixed Models

When the same model is served by multiple providers:

- **Unsuffixed** (`MiniMax-M3`): Uses the conservative minimum across all providers
- **Provider-suffixed** (`MiniMax-M3/opencode-go`): Uses that provider's exact limit

## OpenCode Integration

Generate an OpenCode configuration with model limits:

```bash
eggpool configsetup opencode > opencode-config.json
```

This produces a JSON file with explicit `limit.context`, `limit.input`, and `limit.output` values for each model. OpenCode uses these to trigger compaction before exceeding the configured window.

## Server-Side Enforcement

When `enforce_context_limit = true` (the default), EggPool rejects requests that exceed the configured context limit. This is a defensive guardrail, not the primary compaction mechanism. The primary mechanism is OpenCode's native compaction driven by the advertised limits.

Enforcement returns HTTP 400 with protocol-appropriate error envelopes for OpenAI and Anthropic endpoints.

For translated requests, tool-schema allowance is included in the context
estimate mathematically. It is not appended to the encoded provider body, so
the upstream payload remains unchanged.

## Reload behavior

`model_capabilities` and `model_overrides` are live-reloadable: edit the
config and run `eggpool rehash` to apply new limits without a restart
(see `docs/live-config-rehash.md`). A restart is only needed when the same
edit also touches restart-required fields.

## Dashboard Visibility

The public `/v1/models` response includes configured effective limits under the
namespaced `eggpool.limits` object when the catalog knows them:

```json
{
  "id": "MiniMax-M3/opencode-go",
  "object": "model",
  "eggpool": {
    "limits": {
      "context": 220000,
      "output": 16384
    }
  }
}
```

The dashboard Models table links each model ID to its model-info detail page.
That detail page renders the effective and externally observed context/output
limits when model-info enrichment has populated them. The table itself remains
focused on routing availability, usage, cost, and pricing exactness so the
default Models view stays scannable. Pricing exactness follows the same
request-cost labels used elsewhere in EggPool: `provider_reported`,
`exact`, `derived`, `partial`, `estimated`, and `unknown`.

## Local User Budgets

Per-user request, token, microdollar, and concurrent request budgets are
separate from these model context/output limits. Operators manage rules with
`eggpool users budget set` (`--enforcement hard|estimated`, default
`estimated`); daily and monthly periods use UTC calendar windows. A hold is
reserved atomically before provider dispatch, and retries extend the same
logical request hold. User usage distinguishes provider-reported cost, local
estimates, and conservative reservation fallback.

Each rule is explicitly `hard` or `estimated`, visible in the dashboard,
`GET /api/user-budget`, and the stored audit history. Pre-existing rules are
`estimated` and stay that way until an administrator explicitly promotes them;
nothing is silently upgraded to a guarantee.

- `estimated` (default): token holds use a bounded request-size estimate and
  microdollar holds use EggPool's estimated pricing projection, including
  fallback rates when no model price is known. These are operational
  approximations, not an exact charge or invoice for flat-rate providers, and
  not a hard ceiling.
- `hard`: token rules admit only requests carrying an explicit caller output
  cap compatible with the request surface, and reserve a conservative
  worst-case bound (admission input bound plus that cap, covering the concrete
  attempt plus selector overhead; retries extend per attempt after
  re-checking). USD rules additionally require an operator-configured
  conservative tariff (`[providers.<id>.budget]
  max_microdollars_per_million_tokens`); unpriced providers fail closed, an
  explicit zero tariff means verified free, and estimator fallbacks are never
  a hard price. Requests that cannot be bounded — unbounded output, unknown
  tariff, arithmetic overflow — are denied with a stable 429
  (`User budget hard limit is not enforceable for this request`) before any
  upstream submission. Observed provider usage above a hard hold is settled
  visibly, never clamped.

See the [API reference](api-reference.md#authentication) for the
authenticated `/api/user-budget` view.

## Instance Budget Ceilings

One optional instance-wide ceiling is intersected with every per-user rule:
an admitted call must satisfy all applicable instance-global, instance-model,
user-global, and user-model limits. Manage ceilings with
`eggpool users global-budget set` (same windows, modes, and `--model-id`
scoping; a blank model covers all models) or the admin dashboard's instance
section. Absent ceilings mean no additional cap, and single-admin behavior is
otherwise unchanged.

The shared ceiling is enforced atomically: concurrent requests from different
users cannot both take the final global slot, and no retry, alternate model,
identity switch, or legacy-key request bypasses it — unattributed requests
count against it too. Each logical request counts once across failover and
retries; hard/estimated semantics and the operator-tariff trust boundary match
the per-user contract above. Global counters never depend on a user row, so
disabling or deleting a user preserves history and remaining headroom. Members
see only their own rules and usage; instance ceilings and aggregate usage are
admin-visible only.

Default member profiles do not create or copy budget rules. Configure user and
instance ceilings separately; changing the default model profile leaves all
existing budget rules and usage unchanged.
