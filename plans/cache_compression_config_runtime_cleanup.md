# Cache Compression Config and Runtime UI Cleanup Plan

Date: 2026-07-03

Related work:

- `plans/cache_preserving_deterministic_compression_roadmap.md`
- `plans/cache_compression_phase_09_10_corrective_pass.md`
- `plans/cache_compression_phase_12_operator_docs_profiles.md`
- `plans/cache_compression_phase_12_polish_pass.md`

## Summary

The cache-preserving deterministic compression work landed functionally, but the operator-facing surface is now too phase-shaped. The generated config example exposes nearly every internal knob, splits related controls across multiple phase sections, and currently contains invalid Phase 10 tuning keys that do not match the Pydantic schema. The runtime/dashboard page has the same problem: it presents cache/compression/synthetic/tuning internals as separate phase panels rather than a coherent operational view.

This corrective pass should clean up three surfaces:

1. **Config example** — prune to a small, valid, operator-facing config block and move advanced knobs to docs.
2. **Config/schema documentation** — clarify which knobs are public/stable, advanced, deprecated/reserved, or internal/testing-only.
3. **Runtime/dashboard page** — collapse phase panels into a simpler Request Shaping view and add one or two high-signal overview-card metrics.

This is primarily an operator-experience cleanup. Runtime behavior should remain stable unless a schema/example bug needs a compatibility alias or validation fix.

## Current exposed config surface

The current config surface is split across:

```toml
[compression]
[compression.transforms]
[[compression.policies]]
[compression.tuning]
[compression.tuning.targets]
[compression.tuning.bounds]
[cache.synthetic_cache_controls]
```

Current `[compression]` fields:

```toml
enabled = false
mode = "observe"                  # observe | safe
placement = "suffix_only"         # suffix_only | after_cache_boundary | anywhere
respect_cache_boundaries = true
compress_static_prefix = false
allow_static_prefix_override = false
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25.0
header_override = false
header_cache_policy = true
```

Current `[compression.transforms]` fields:

```toml
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true
```

Current `[[compression.policies]]` match fields:

```toml
name = "..."
match_clients = [...]
match_provider_ids = [...]
match_provider_kinds = [...]
match_models = [...]
match_requested_models = [...]
match_protocols = ["openai", "anthropic"]
match_transcoded = true
```

Current `[[compression.policies]]` override fields:

```toml
enabled = true
mode = "safe"
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 1024
min_savings_tokens = 512
max_compression_latency_ms = 25.0
transforms = { ... }
synthetic_cache_controls = true
synthetic_cache_dry_run = true
synthetic_cache_min_stable_tokens = 1024
synthetic_cache_max_breakpoints = 4
```

Current `[cache.synthetic_cache_controls]` fields:

```toml
enabled = false
dry_run = true
provider_kinds = ["anthropic"]
ttl = "ephemeral"
min_stable_tokens = 1024
max_breakpoints = 4
require_policy = true
placements = ["system", "tools"]
```

Current `[compression.tuning]` fields:

```toml
enabled = false
mode = "recommend"        # off | recommend | apply
window_requests = 500
min_window_requests = 50
update_interval_s = 300
max_adjustment_pct = 25.0
cooldown_s = 900
persist_recommendations = true
```

Current `[compression.tuning.targets]` fields:

```toml
max_latency_budget_warning_rate = 0.01
max_failed_fallback_rate = 0.001
min_positive_savings_rate = 0.8
min_median_savings_tokens = 512
max_p95_latency_ms = 25.0
```

Current `[compression.tuning.bounds]` fields:

```toml
min_candidate_tokens_min = 256
min_candidate_tokens_max = 16384
min_savings_tokens_min = 128
min_savings_tokens_max = 8192
max_compression_latency_ms_min = 5.0
max_compression_latency_ms_max = 100.0
```

## Known bug: invalid tuning keys in config example

The generated `src/eggpool/_share/config.example.toml` currently documents invalid tuning keys:

```toml
[compression.tuning]
mode = "recommend"
window_seconds = 3600
min_window_requests = 200
max_adjustment_pct = 25
cooldown_seconds = 1800
apply_ttl_seconds = 900

[compression.tuning.targets]
max_failed_fallback_rate = 0.05
max_latency_warning_rate = 0.10
min_positive_savings_rate = 0.60
target_compression_latency_ms = 4000.0
```

These do not match the actual schema. The actual schema uses:

- `enabled`
- `window_requests`
- `update_interval_s`
- `cooldown_s`
- `persist_recommendations`
- `max_latency_budget_warning_rate`
- `max_p95_latency_ms`

Because the models use `extra="forbid"`, uncommenting the current example will fail config validation. Fix this first.

## Desired operator-facing config posture

The default example should show a small stable surface, not every internal knob. The guiding principle:

- **Default config example** should be minimal, safe, valid, and readable.
- **Advanced docs** can list every schema field.
- **Runtime dashboard** should show status and actions, not phase internals.

Recommended default example block:

```toml
# ----------------------------------------------------------------------
# Request shaping: cache-preserving compression
# ----------------------------------------------------------------------
# Disabled by default. "observe" records opportunities without changing
# requests. "safe" compresses only volatile suffix content and fails closed
# if a stable prefix would change. Routing never uses these metrics.
[compression]
enabled = false
mode = "observe"                 # observe | safe
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25.0

# Optional: disable specific deterministic transforms if one is noisy.
[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true

# Optional: Anthropic synthetic cache-control dry-run. Leave disabled unless
# you are validating Anthropic provider-bound cache annotations.
[cache.synthetic_cache_controls]
enabled = false
dry_run = true
min_stable_tokens = 1024
```

Keep policy and tuning examples out of the main generated example, or move them to a clearly marked advanced appendix at the bottom with every line commented and schema-valid.

## Config cleanup tasks

### Task 1: Fix invalid tuning example immediately

Update both likely example sources if both exist:

- `config.example.toml`
- `src/eggpool/_share/config.example.toml`

Replace invalid tuning keys with schema-valid keys, or remove the tuning block from the generated example entirely. If keeping it, use:

```toml
# [compression.tuning]
# enabled = false
# mode = "recommend"
# window_requests = 500
# min_window_requests = 50
# update_interval_s = 300
# max_adjustment_pct = 25.0
# cooldown_s = 900
# persist_recommendations = true
#
# [compression.tuning.targets]
# max_latency_budget_warning_rate = 0.01
# max_failed_fallback_rate = 0.001
# min_positive_savings_rate = 0.8
# min_median_savings_tokens = 512
# max_p95_latency_ms = 25.0
#
# [compression.tuning.bounds]
# min_candidate_tokens_min = 256
# min_candidate_tokens_max = 16384
# min_savings_tokens_min = 128
# min_savings_tokens_max = 8192
# max_compression_latency_ms_min = 5.0
# max_compression_latency_ms_max = 100.0
```

Preferred: do not include tuning in the default example at all. Mention it in docs as advanced/recommendation-only.

### Task 2: Prune phase-shaped config prose

Remove or sharply shorten blocks labeled:

- `Phase 5`
- `Phase 6`
- `Phase 9`
- `Phase 10`
- `Phase 12`

Replace with operator language:

- `Request shaping`
- `Safe compression`
- `Synthetic cache controls`
- `Advanced policy overrides`
- `Advisory threshold tuning`

### Task 3: Hide or demote advanced knobs from the main example

Move these out of the main example body:

- `placement`
- `respect_cache_boundaries`
- `compress_static_prefix`
- `allow_static_prefix_override`
- `header_override`
- `header_cache_policy`
- `provider_kinds`
- `ttl`
- `max_breakpoints`
- `require_policy`
- `placements`
- full `[[compression.policies]]` examples
- full `[compression.tuning.*]` examples

These can remain valid schema fields for backwards compatibility, but they should not be presented as normal operator knobs.

### Task 4: Add an advanced docs table

Update `docs/cache-compression.md` or `docs/cache-compression-profiles.md` with a compact table:

| Field | Stability | Normal use? | Notes |
| --- | --- | --- | --- |
| `compression.enabled` | stable | yes | master switch |
| `compression.mode` | stable | yes | observe/safe |
| `compression.min_candidate_tokens` | stable | yes | threshold |
| `compression.min_savings_tokens` | stable | yes | threshold |
| `compression.max_compression_latency_ms` | stable | yes | latency budget |
| `compression.transforms.*` | stable | sometimes | deterministic transform toggles |
| `compression.placement` | advanced | no | keep `suffix_only` |
| `compression.compress_static_prefix` | dangerous/reserved | no | should remain false |
| `compression.allow_static_prefix_override` | dangerous/reserved | no | should remain false |
| `compression.header_override` | advanced | no | request-level override |
| `compression.header_cache_policy` | advanced | no | request-level preserve opt-out |
| `compression.policies.*` | advanced | sometimes | scoped overrides |
| `cache.synthetic_cache_controls.*` | experimental | maybe | Anthropic-only |
| `compression.tuning.*` | experimental/advisory | rarely | recommendation-only |

## Optional schema cleanup / compatibility aliases

Do not break existing configs in this pass unless necessary. Prefer documentation/example cleanup over schema removal.

However, consider adding alias support for the invalid example keys if there is risk that users copied them already:

- `window_seconds` -> either reject with clear error or map to `update_interval_s` only if semantically correct.
- `cooldown_seconds` -> `cooldown_s`.
- `max_latency_warning_rate` -> `max_latency_budget_warning_rate`.
- `target_compression_latency_ms` -> `max_p95_latency_ms`.

Recommended approach: **do not silently alias semantically wrong keys**. Add a validator error message that identifies the correct key when common invalid keys are present. Pydantic `extra="forbid"` currently errors, but the message may be generic; a pre-validator can make this clearer.

Example:

```python
_COMMON_TUNING_KEY_RENAMES = {
    "window_seconds": "window_requests or update_interval_s, depending on intent",
    "cooldown_seconds": "cooldown_s",
    "apply_ttl_seconds": "not supported; apply mode is dormant",
}
```

## Runtime/dashboard cleanup tasks

### Current problem

Runtime/API routes and likely dashboard panels mirror implementation phases:

```text
/api/stats/cache-observability
/api/stats/canonical-request-segmentation
/api/stats/compression-observability
/api/stats/synthetic-cache-observability
/api/stats/compression-tuning
```

Keep the API endpoints for compatibility, but stop presenting the dashboard as five phase panels.

### Desired dashboard structure

Create one consolidated Runtime section called **Request Shaping**.

Recommended panel layout:

1. **Mode / Safety card**
   - Compression: Off / Observe / Safe
   - Synthetic cache: Off / Dry-run / Apply
   - Tuning: Off / Recommend-only
   - Routing: Reporting-only badge

2. **Compression card**
   - Requests analyzed
   - Requests compressed
   - Estimated savings tokens
   - Actual savings tokens if available
   - p95 analyzer/apply latency
   - Failed fallback count

3. **Cache controls card**
   - Cache counters reported rate
   - Synthetic dry-run candidates
   - Synthetic applied count
   - Native cache preserved warnings or count

4. **Safety / guardrails card**
   - Stable-prefix preserved rate
   - Failed fallback count
   - Policy warning count
   - Routing non-interference badge

5. **Advanced details accordion**
   - Segmentation totals
   - per-policy table
   - reason-code table
   - tuning recommendations
   - raw endpoint-shaped tables, if needed

Do not remove the underlying detailed data. Hide it behind “Advanced details” or separate sub-tabs.

### Runtime labels to remove

Avoid these labels in UI text:

- Phase 1
- Phase 2
- Phase 4
- Phase 5
- Phase 6
- Phase 9
- Phase 10
- canonical request segmentation
- closed-loop threshold tuning

Prefer operator terms:

- Cache reporting
- Request segmentation
- Compression opportunities
- Safe compression
- Synthetic cache controls
- Advisory tuning
- Guardrails

## Overview card metrics

Add one or two high-signal metrics to existing overview cards. Recommended choices:

### Metric 1: Compression effect

Display on overview as:

```text
Request shaping: Off / Observe / Safe
Compressed: N requests / X tokens saved
```

If compression is disabled, show:

```text
Request shaping: Off
```

If observe-only:

```text
Request shaping: Observe
Potential savings: X tokens
```

If safe mode:

```text
Request shaping: Safe
Saved: X tokens
```

Source: aggregate `compression_status`, `compression_mode`, `estimated_savings_tokens`, and actual savings fields if present from `compression-observability` / runtime stats.

### Metric 2: Cache reporting / synthetic cache status

Display on overview as one compact line:

```text
Cache reported: 72% known rows
Synthetic: Off / Dry-run / Applied N
```

Use known-only cache counter status so missing counters are not treated as zero.

If this is too much for one card, add only `Cache reported: X%` to the existing tokens/cache card, and leave synthetic cache on the Request Shaping runtime card.

## API layer cleanup

Keep current endpoints for compatibility. Optionally add one aggregated endpoint to simplify dashboard code:

```text
GET /api/stats/request-shaping
```

Shape:

```json
{
  "period": "24h",
  "mode": {
    "compression": "off|observe|safe|mixed",
    "synthetic_cache": "off|dry_run|apply|mixed",
    "tuning": "off|recommend"
  },
  "compression": {
    "requests_analyzed": 0,
    "requests_compressed": 0,
    "estimated_savings_tokens": 0,
    "actual_savings_tokens": 0,
    "failed_fallback_count": 0,
    "p95_latency_ms": 0
  },
  "cache": {
    "cache_counter_reported_rate": 0.0,
    "cached_input_tokens": 0
  },
  "synthetic_cache": {
    "dry_run_count": 0,
    "applied_count": 0,
    "candidate_count": 0,
    "warning_count": 0
  },
  "guardrails": {
    "routing_uses_cache_metrics": false,
    "routing_uses_compression_metrics": false,
    "routing_uses_synthetic_cache": false,
    "stable_prefix_preserved_rate": 1.0
  }
}
```

This endpoint can compose existing stats service calls. It should not expose raw request content.

If adding a new endpoint is too much, perform the aggregation client-side in the dashboard. Prefer server-side aggregation if dashboard code is already overloaded.

## Files to inspect/update

Likely config files:

- `src/eggpool/_share/config.example.toml`
- `config.example.toml` if present
- `docs/cache-compression.md`
- `docs/cache-compression-profiles.md`
- `docs/cache-compression-troubleshooting.md`
- `AGENTS.md`
- `architecture/README.md`

Likely runtime/API files:

- `src/eggpool/api/stats.py`
- `src/eggpool/stats/service.py` or equivalent stats facade
- `src/eggpool/stats/queries.py`
- dashboard template/static runtime page files; locate by searching for:
  - `cache-observability`
  - `canonical-request-segmentation`
  - `compression-observability`
  - `synthetic-cache-observability`
  - `compression-tuning`
  - `Phase 9`
  - `Phase 10`

## Tests

### Config tests

Add or update tests so the shipped example validates:

```bash
uv run eggpool check-config --config src/eggpool/_share/config.example.toml
```

Or equivalent unit test:

```python
from eggpool.models.config import load_config

def test_packaged_config_example_validates():
    load_config("src/eggpool/_share/config.example.toml")
```

Add a test for no invalid tuning keys in example text:

```python
for bad in ("window_seconds", "cooldown_seconds", "apply_ttl_seconds", "max_latency_warning_rate", "target_compression_latency_ms"):
    assert bad not in example_text
```

### Runtime/dashboard tests

Add tests that the new Request Shaping summary exposes:

- compression mode summary;
- cache reported rate;
- synthetic cache mode summary;
- guardrail booleans;
- no raw prompt/body fields.

If dashboard has HTML tests, assert it renders:

- `Request Shaping`
- `Compression`
- `Cache controls`
- `Guardrails`

And does not render phase headings:

- `Phase 1`
- `Phase 2`
- `Phase 4`
- `Phase 9`
- `Phase 10`

### Regression tests

Keep existing API endpoint tests. The cleanup must not remove or break:

- `/api/stats/cache-observability`
- `/api/stats/canonical-request-segmentation`
- `/api/stats/compression-observability`
- `/api/stats/synthetic-cache-observability`
- `/api/stats/compression-tuning`

## Acceptance criteria

- Packaged config example validates as-is.
- Invalid tuning keys are removed from every example/config doc snippet.
- Main config example exposes only normal operator knobs for request shaping.
- Advanced policy/tuning/static-prefix knobs are moved to docs or commented appendix, not the main example body.
- Runtime page presents one Request Shaping section instead of phase-split panels.
- Runtime UI avoids phase labels in operator-facing text.
- Overview cards include one or two compact request-shaping/cache signals.
- Existing stats endpoints remain backward-compatible.
- Optional aggregated `/api/stats/request-shaping` endpoint, if added, is content-private and tested.
- Docs explain the reduced public config surface and where advanced knobs live.
- Full checks pass: ruff format/check, pyright, pytest.

## Suggested implementation order

1. Fix invalid tuning keys in `src/eggpool/_share/config.example.toml` and any root example.
2. Add test proving packaged example validates and forbidding known-bad keys.
3. Prune generated example to the minimal request-shaping block.
4. Move advanced knobs into docs tables.
5. Locate runtime dashboard code and replace phase panels with Request Shaping summary layout.
6. Add overview-card metrics.
7. Keep API endpoints; optionally add aggregated request-shaping endpoint.
8. Add dashboard/API tests.
9. Run full validation.

## Rollback guidance

This cleanup should be reversible without schema migration. If dashboard aggregation causes issues, revert only the UI aggregation and keep the config-example fix. The config-example bug fix should not be reverted because the current example contains schema-invalid keys.