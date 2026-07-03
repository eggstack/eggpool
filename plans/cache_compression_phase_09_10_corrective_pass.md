# Phase 9-10 Corrective Pass Plan: Synthetic Cache Correctness and Tuning Lifecycle Closure

Date: 2026-07-03

Related plans:

- `plans/cache_compression_phase_06_policy_controls.md`
- `plans/cache_compression_phase_07_dashboard_runtime_views.md`
- `plans/cache_compression_phase_08_routing_guardrails.md`
- `plans/cache_compression_phase_09_synthetic_cache_controls.md`
- `plans/cache_compression_phase_10_closed_loop_threshold_tuning.md`

Current implementation baseline:

- `src/eggpool/transcoder/compression/policy.py`
- `src/eggpool/transcoder/compression/policy_resolver.py`
- `src/eggpool/transcoder/cache_synthesis.py`
- `src/eggpool/transcoder/cache_synthesis_policy.py`
- `src/eggpool/transcoder/compression/tuning.py`
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/request/coordinator.py`
- migrations `0044`, `0045`, `0046`

## Summary

The phases 6-10 implementation broadly landed, but the current repo shape leaves several production-risk gaps around Phase 9 synthetic cache controls and Phase 10 closed-loop threshold tuning.

This corrective pass should make synthetic cache controls correct before apply mode is trusted, and should make Phase 10 honestly recommendation-first unless the recommendation-to-runtime-override loop is fully wired and tested.

The corrective goal is not to add new compression transforms, routing behavior, or semantic cache optimization. The goal is to tighten invariants and close the implementation gaps introduced by the broad phase 6-10 pass.

## Current assessment

### Good shape

- Phase 6 policy models, resolver, persistence fields, and finalizer wiring exist.
- Phase 7 dashboard/stats surfaces exist and are intentionally content-private.
- Phase 8 routing guardrails are directionally strong: scorer input remains load/fairness/health oriented, not cache/compression oriented.
- Phase 10 tuning core is narrow and content-private at the pure-engine level.
- DB schema version is advanced to 46 and migrations are additive.

### Corrective focus

1. Synthetic cache controls are run pre-route, before the actual selected provider/account/upstream protocol is known.
2. Provider-specific compression and synthetic-cache policy matches are documented as reserved for post-route but are not actually resolved post-route.
3. Synthetic cache controls probably do not work for the key intended path: OpenAI-client request routed/transcoded to an Anthropic-compatible upstream.
4. Native `cache_control` preservation uses inconsistent path syntaxes and may fail to detect native annotations.
5. Synthetic apply currently hardcodes `ephemeral` TTL instead of honoring the effective config.
6. Synthetic-cache override fields may poison normal compression policy overlay because they ride on `CompressionPolicyOverride` but are not skipped by `_overlay_config()`.
7. Phase 10 has a registry and pure recommendation engine, but the full recommendation persistence / runtime override lifecycle needs a closure pass or explicit recommendation-only positioning.

## Hard invariants

This corrective pass must preserve these invariants:

- Compression/cache/tuning metrics remain reporting-only for routing.
- Same-provider account fairness must not depend on cache hit ratio, synthetic cache eligibility, compression savings, tuning recommendation, or policy name.
- Synthetic cache controls are disabled by default and dry-run by default.
- Apply mode must never annotate volatile suffix content or compressed content.
- Native provider cache controls must be preserved and never overwritten or duplicated.
- Provider-visible request mutation must happen only after the target provider/protocol is known.
- Tuning must not enable compression, change mode, alter placement, alter transforms, alter synthetic-cache knobs, or enable static-prefix compression.
- No raw prompts, request bodies, tool output, auth headers, or system messages may be persisted or rendered in dashboard/API outputs.

## Phase A: Split pre-route and post-route request shaping

### Problem

`handle_proxy_request()` currently resolves compression policy, runs compression, and runs synthetic cache synthesis before the coordinator selects the actual account/provider. At that point the code only knows the client endpoint protocol and optional requested provider prefix. It does not know the selected account, selected provider kind, final upstream protocol, or provider-specific transcode shape.

This is tolerable for client/protocol/model compression policies, but it is wrong for provider-specific synthetic cache controls. Synthetic cache controls need to know whether the request will be sent to an Anthropic-compatible upstream.

### Required design

Introduce a clear two-stage model:

1. **Pre-route stage**
   - Parse payload.
   - Validate model field.
   - Run context-limit preflight as currently implemented.
   - Run canonical segmentation on the client-visible payload.
   - Resolve pre-route compression policy using only client/protocol/requested model/transcoded-unknown context.
   - Run observe-mode compression analysis and safe suffix compression if enabled by pre-route policy.
   - Do not apply synthetic cache controls.

2. **Post-route/pre-dispatch stage**
   - Select account/provider exactly once using existing routing logic.
   - Resolve upstream protocol and target provider kind.
   - Perform provider-bound transcoding/model rewrite as needed.
   - Re-resolve policy with provider/account/model/upstream protocol context, or overlay provider-specific policy onto the pre-route result.
   - Run synthetic cache-control selector/mutator against the provider-bound payload, not the original client payload.
   - Dispatch the same selected account; do not reroute because of synthetic-cache results.

### Implementation options

Prefer moving synthetic-cache application into `RequestCoordinator` immediately after account selection and after `_apply_selected_provider_transcode_adjustments()` / request transcoding has produced the final provider-bound body.

If current coordinator architecture makes that difficult, add a narrow post-route hook:

```python
@dataclass(frozen=True, slots=True)
class ProviderBoundRequestShape:
    payload: dict[str, Any]
    body: bytes
    client_protocol: str
    upstream_protocol: str
    provider_id: str
    provider_kind: str | None
    account_name: str
    model_id: str
```

Then run synthetic cache synthesis on `ProviderBoundRequestShape.payload` and update `context.upstream_body` before `client.build_request(...)`.

Do not run synthetic mutation in `handle_proxy_request()` unless no routing/transcoding is involved and the upstream protocol is already known to be Anthropic.

### Tests

- OpenAI client request routed to Anthropic provider triggers post-route synthetic dry-run when enabled and policy matched.
- OpenAI client request routed to OpenAI provider is provider-unsupported.
- Anthropic client request routed to Anthropic provider works.
- Provider-specific `match_provider_ids` and `match_provider_kinds` policies match only post-route.
- The selected account before synthetic-cache application is the account dispatched to upstream.
- Synthetic-cache result cannot trigger account reselection.

## Phase B: Fix provider/upstream protocol resolution for synthetic cache controls

### Problem

Current synthetic-cache call passes `target_protocol = endpoint.protocol`, which is the client endpoint protocol, not the upstream/provider protocol. The selector rejects anything where `target_protocol != "anthropic"`, so OpenAI-client to Anthropic-provider flows will not synthesize controls.

### Required changes

- Ensure synthetic cache receives `target_protocol = selected.protocol` or `context.upstream_protocol`, whichever represents the final provider wire protocol.
- Ensure `target_provider_kind` comes from the selected provider, not only the requested provider prefix.
- Add helper with explicit name, for example:

```python
def resolve_selected_provider_kind(catalog, selected: SelectedAttempt) -> str | None:
    ...
```

- Avoid best-effort catalog lookup by requested provider ID when a selected provider ID is available.
- Make synthetic-cache result record both `client_protocol` and `target_protocol` in summary JSON.

### Tests

- OpenAI endpoint + Anthropic-selected provider: `target_protocol == "anthropic"` in synthetic summary.
- Anthropic endpoint + Anthropic-selected provider: `target_protocol == "anthropic"`.
- OpenAI endpoint + OpenAI-selected provider: provider unsupported.
- Explicit provider prefix routes to that provider and synthetic policy uses that provider kind.

## Phase C: Normalize native cache-control path handling

### Problem

`_existing_native_cache_controls()` records paths using bracket syntax such as `system[0].cache_control`, while candidates use dot/tuple-derived paths such as `system.0.text` or `tools.0`. The mutator checks candidate paths against native paths, so it may fail to detect existing native controls and overwrite or duplicate them.

### Required changes

Adopt one internal path representation for all synthetic-cache selector/mutator logic.

Recommended approach:

```python
JsonPath = tuple[str | int, ...]
```

- Store candidates with tuple paths, not string paths.
- Convert to display strings only in `as_dict()` / summary JSON.
- `_existing_native_cache_controls()` should return container paths, not `.cache_control` leaf paths.
- Candidate target should point at the container block that would receive `cache_control`, not the text leaf.
- If the segment path points to a text leaf, normalize to its owning container before candidate creation.

Example normalized paths:

- Anthropic system block text leaf: `("system", 0, "text")` -> container `("system", 0)`.
- Anthropic tool definition: `("tools", 0)` -> container `("tools", 0)`.
- Anthropic message content text block: `("messages", 3, "content", 0, "text")` -> container `("messages", 3, "content", 0)` if message-block placement is later allowed.

### Required behavior

- If a container already has `cache_control`, skip it and record `synthetic_cache_control_existing_native_preserved`.
- Never overwrite a non-empty native cache-control object.
- Never attach `cache_control` to a string leaf.
- Never attach `cache_control` to a dict that is not an Anthropic-supported block/tool container.

### Tests

- Existing native system `cache_control` is preserved and not overwritten.
- Existing native tool `cache_control` is preserved and not overwritten.
- Existing native message-block `cache_control` is preserved if message-block placement is enabled in a future extension.
- Candidate path and native path use the same normalized tuple representation.
- Summary JSON renders stable, redacted display paths without raw text.

## Phase D: Honor effective TTL and provider limits

### Problem

`apply_synthetic_cache_controls()` currently hardcodes `ttl="ephemeral"` and emits `cache_control_type="ephemeral"`. The effective `SyntheticCacheControlsConfig.ttl` is not honored in apply mode.

### Required changes

- Add `ttl` to `SyntheticCachePlan` or `SyntheticCacheCandidate` after validation.
- Pass the effective TTL from `_merge_synthetic_cache_config()` into the plan.
- `apply_synthetic_cache_controls()` must use `plan.ttl`.
- Cache-boundary annotations must record the same TTL that was applied.
- If only `ephemeral` is truly supported today, reject or downgrade unsupported TTLs during config validation rather than accepting `5m`/`1h` and silently ignoring them.

### Recommended policy

For this corrective pass, prefer **strict support**:

- `ttl = "ephemeral"` is accepted.
- `ttl = "5m"` and `ttl = "1h"` are rejected or marked reserved-but-disabled unless actual provider support and wire shape are implemented.

If keeping reserved values is required for config compatibility, then the selector must emit a clear warning and no mutation for unsupported TTLs.

### Tests

- Apply mode emits the configured TTL when supported.
- Unsupported TTL fails config validation or produces provider-unsupported/no-mutation warning.
- Cache-boundary annotation TTL equals the actual emitted TTL.
- Summary JSON includes TTL.

## Phase E: Keep synthetic-cache override fields out of compression config overlay

### Problem

Synthetic-cache override fields are defined on `CompressionPolicyOverride` so they can reuse policy matching, but `_overlay_config()` currently only skips `_OVERRIDE_ONLY_FIELDS`. It should also skip `_SYNTHETIC_CACHE_OVERLAY_FIELDS` before validating against `CompressionConfig`. Otherwise policies that contain only synthetic-cache fields can trigger validation errors when overlaid onto compression config.

### Required changes

- Update `_overlay_config()` to skip both:
  - `_OVERRIDE_ONLY_FIELDS`
  - `_SYNTHETIC_CACHE_OVERLAY_FIELDS`
- Keep extracting synthetic overrides separately into `synthetic_cache_overrides`.
- Add a regression test where a policy row contains only synthetic-cache fields and does not produce compression policy warnings.
- Add a regression test where a policy row contains both compression fields and synthetic-cache fields; compression fields overlay correctly and synthetic fields overlay into cache synthesis config.

### Tests

- `resolve_compression_policy()` with `synthetic_cache_controls=true` only: no validation warning, compression config unchanged, synthetic overrides present.
- Mixed safe compression + synthetic dry-run policy: both effective compression config and synthetic effective config are correct.
- Bad synthetic value fails at Pydantic config load, not during per-request resolution.

## Phase F: Re-segment or map segmentation after transcoding

### Problem

Synthetic cache candidate selection currently uses segmentation from the client-visible payload. For OpenAI-to-Anthropic transcoding, the provider-bound payload shape differs from the client payload. A path valid in OpenAI request shape may not resolve in the Anthropic provider-bound payload.

### Required changes

For synthetic cache controls, use segmentation over the provider-bound payload with the provider-bound protocol.

Recommended approach:

- Keep the existing client segmentation for Phase 2/4/5 metrics.
- After provider-bound payload is produced, run `segment_request(provider_payload, protocol=upstream_protocol)` for synthetic-cache candidate selection only.
- Store this as `synthetic_cache_segmentation` only inside the synthetic-cache result; do not replace the original request segmentation persisted for canonical client-shape observability.
- Make synthetic-cache summary include `segmentation_protocol` and `segmentation_status`.

### Tests

- OpenAI client payload transcodes to Anthropic provider payload; synthetic-cache selector uses Anthropic paths that resolve in the provider payload.
- No mutation is attempted when provider-bound segmentation fails.
- Client segmentation remains persisted as before.
- Synthetic-cache segmentation summary does not expose raw content.

## Phase G: Tighten synthetic cache apply-mode safety

### Required safety checks

Before applying mutation:

- Confirm target protocol is Anthropic.
- Confirm provider kind is in effective provider list.
- Confirm policy requirement is satisfied if `require_policy=true`.
- Confirm every candidate resolves to a supported container dict.
- Confirm no candidate container already has native cache_control.
- Confirm candidate segment kind is stable prefix and protected.
- Confirm candidate segment source is allowed by placement config.

After applying mutation:

- Confirm the mutated payload differs only by added `cache_control` keys at candidate containers.
- Confirm no text content changed.
- Confirm no volatile suffix path was mutated.
- Confirm native cache controls remain byte-for-byte equivalent.
- If validation fails, return original payload with `status="failed_fallback"` or equivalent warning and do not dispatch mutated payload.

### Implementation note

Add a small structural diff helper for dict/list trees that reports paths of changed/added/removed leaves. It must not persist values, only paths and change kinds.

### Tests

- Apply adds only allowed `cache_control` paths.
- Text content unchanged after apply.
- Native cache controls unchanged after apply.
- Volatile suffix unchanged after apply.
- Unsupported shape falls back without mutation.
- Failed fallback is persisted in synthetic summary and dashboard counts.

## Phase H: Clarify Phase 10 lifecycle: recommend-only or fully applied

### Problem

The tuning module, registry, migration, and API surface exist, but the implementation needs a clear lifecycle:

- How are recommendations computed periodically?
- How are `compression_tuning_recommendations` rows written?
- In `mode=apply`, who builds and registers runtime overrides?
- How are `compression_tuning_overrides` audit rows written?
- How are expired overrides cleared?

Current code has the pure engine and registry, but the operational producer path needs explicit closure or the docs should say Phase 10 is currently recommendation-library/API only.

### Option 1: Recommendation-only closure

If keeping this pass small, make Phase 10 explicitly recommendation-only:

- Keep `mode="apply"` config rejected or documented as reserved.
- Do not create or use runtime registry except test fixtures.
- `/api/stats/compression-tuning` returns window metrics and persisted recommendations only when a future background task writes them.
- Update docs to remove claims that apply-mode runtime override is live.
- Add tests that no runtime override is applied in recommend mode.

### Option 2: Full lifecycle closure

If implementing full apply mode now:

- Add a supervised background task, e.g. `compression_tuning_refresh`, gated by `[compression.tuning] enabled=true`.
- On each interval:
  1. Fetch per-policy window metrics.
  2. Fetch current static config for each policy.
  3. Compute recommendations.
  4. Persist recommendation JSON to `compression_tuning_recommendations` if `persist_recommendations=true`.
  5. If `mode="apply"` and recommendation status is `applied`, build runtime override and register it in `RuntimeCompressionPolicyOverrideRegistry`.
  6. Persist audit row to `compression_tuning_overrides`.
  7. Expire old registry entries on lookup or periodic cleanup.

- Add operator-clearable hook or CLI later; not required in this pass if registry expiry works.

### Recommendation

For this corrective pass, choose **Option 1 unless there is already a nearly complete background loop hidden outside the inspected files**. The project is already complex; recommendation-only is safer until the metrics windows stabilize.

### Tests

For recommendation-only:

- `mode=recommend` never changes resolved request policy.
- `mode=apply` is rejected or explicitly no-op with a warning if reserved.
- API returns clear `mode` and `active=false` for runtime overrides.
- Docs do not claim automatic runtime override unless implemented.

For full lifecycle:

- Background task persists recommendations.
- Apply mode registers in-memory override.
- Resolver applies only allowed fields.
- Override expires.
- Audit row written.
- Routing unchanged.

## Phase I: Correct documentation and config examples

### Documentation updates

Update `README.md`, `architecture/README.md`, and config example comments to reflect the corrected semantics:

- Synthetic cache controls run post-route on provider-bound payloads.
- Provider-specific policy matches require post-route context.
- OpenAI-client to Anthropic-provider synthesis is supported only through post-route provider-bound segmentation.
- Native cache controls are preserved via normalized container paths.
- TTL support is explicit; no silent hardcoded TTL.
- Phase 10 is recommendation-only unless full lifecycle is implemented.
- Routing non-interference remains hard invariant.

### Config example updates

Ensure `src/eggpool/_share/config.example.toml` does not imply apply-mode synthetic cache or tuning is production-safe by default. Recommended examples:

```toml
[cache.synthetic_cache_controls]
enabled = false
dry_run = true
ttl = "ephemeral"
require_policy = true

[compression.tuning]
enabled = false
mode = "recommend"
```

For synthetic apply examples, include a warning comment:

```toml
# Apply mode mutates provider-bound Anthropic requests by adding cache_control
# to stable-prefix containers only. Leave dry_run=true until dashboard counters
# show the expected candidates.
```

## Phase J: Verification matrix

Run the focused tests first:

```bash
uv run pytest tests/unit/test_compression_policy_resolver.py -q
uv run pytest tests/unit/test_cache_synthesis.py -q
uv run pytest tests/unit/test_routing_guardrails.py -q
uv run pytest tests/unit/test_compression_tuning.py -q
uv run pytest tests/integration/test_compression_policy_wiring.py -q
```

Then run the broader relevant suites:

```bash
uv run pytest tests/unit/test_compression_apply_production.py -q
uv run pytest tests/unit/test_compression_path_resolution.py -q
uv run pytest tests/unit/test_stats_synthetic_cache.py -q
uv run pytest tests/unit/test_stats_routes_synthetic_cache.py -q
uv run pytest tests/unit/test_api_phase7.py -q
uv run pytest tests/unit/test_dashboard_phase7.py -q
```

Then full checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

If the repo uses a different command wrapper, preserve the existing project-standard commands in the final implementation commit message.

## Acceptance criteria

- Synthetic cache controls no longer run as a pre-route mutation for provider-dependent flows.
- OpenAI-client to Anthropic-provider synthetic dry-run works with provider-bound Anthropic segmentation.
- Provider-specific policy fields match post-route and are covered by tests.
- Native `cache_control` annotations are never overwritten or duplicated.
- Synthetic-cache path representation is normalized internally.
- Effective TTL is honored or unsupported TTLs are rejected/no-op with explicit warning.
- Synthetic-cache override fields do not trigger compression config overlay warnings.
- Synthetic apply mode validates structural diff and fails closed on unexpected mutation.
- Phase 10 docs and behavior match reality: either recommendation-only, or full background lifecycle with persisted recommendations and runtime overrides.
- Routing guardrail tests still prove cache/compression/synthetic/tuning fields do not influence scoring, health removal, or reselection.
- No raw request content appears in synthetic/tuning summaries, dashboard cards, or API responses.
- Full tests, ruff, and pyright pass.

## Suggested implementation order

1. Fix resolver overlay skip for `_SYNTHETIC_CACHE_OVERLAY_FIELDS`.
2. Normalize synthetic-cache path representation and native-preservation detection.
3. Honor TTL or reject unsupported TTL values.
4. Move synthetic cache selection/application to post-route provider-bound payloads.
5. Add provider-bound segmentation for synthetic-cache selection.
6. Add structural-diff safety check for synthetic apply.
7. Decide Phase 10 recommendation-only versus full lifecycle and align docs/code/tests.
8. Update dashboard/API summaries if status names change.
9. Update README/architecture/config examples.
10. Run focused suites and full checks.

## Rollback guidance

If production issues appear after this corrective pass:

```toml
[cache.synthetic_cache_controls]
enabled = false

[compression.tuning]
enabled = false
mode = "recommend"
```

These settings should return runtime behavior to Phase 8/observe-safe compression behavior while leaving additive DB columns and audit tables in place.