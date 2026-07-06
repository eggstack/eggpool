# Cache Page Simplification and Request-Shaping Copy Polish Plan

## Context

The dashboard `/cache` page centralizes cache reporting, request segmentation, native cache preservation, compression observability, safe compression runtime, policy overrides, synthetic cache controls, advisory tuning, routing guardrails, and transcoding diagnostics. That direction is correct, but the current presentation still reads like an implementation dump rather than an operator workflow.

The page exposes accurate instrumentation, but it places many peer panels in one long flow and uses internal terminology that can confuse three separate concepts operators need to distinguish: provider-reported cache counters from upstream usage payloads; EggPool-added provider cache annotations through synthetic cache controls; and cache-preserving compression of only eligible volatile suffix content.

The config and docs already state the intended safety model: default request shaping is reporting-only, compression observe mode does not mutate requests, safe compression mutates only volatile suffixes and fails closed on stable-prefix changes, synthetic cache controls are disabled/dry-run first, and cache/compression/tuning metrics never enter routing. The UI should make those facts obvious without requiring the operator to read the full operator guide.

## Goals

Make `/cache` easier to understand while preserving existing diagnostics and safety guarantees.

Target end state:

1. A short plain-language status summary answers whether requests are being changed, whether provider cache counters are visible, whether compression helps, whether safety guardrails are clean, and whether any of this can influence routing.
2. Normal operator panels show only actionable details for provider cache counters, compression, and EggPool cache annotations.
3. Advanced diagnostics remain available but are visually demoted/collapsed unless warnings or non-default activity make them relevant.
4. `config.example.toml`, README snippets, and cache/compression docs use the same terminology as the UI.
5. No request-path behavior, routing behavior, schema, stats collection, or provider protocol behavior changes in this pass.

## Non-goals

Do not remove stats endpoints or durable counters. Do not change compression eligibility, synthetic cache insertion behavior, request segmentation logic, native cache preservation logic, or routing scorer inputs. Do not enable compression or synthetic cache controls by default. Do not introduce a frontend framework. Do not add raw prompt/request/response content to any dashboard or API payload.

## Current files likely involved

Primary UI implementation:

- `src/eggpool/dashboard/render.py`
- `src/eggpool/dashboard/routes.py`
- dashboard CSS/static JS files if disclosure state or persistent expansion is needed

Stats/API contracts to preserve:

- `/api/stats/request-shaping`
- `/api/stats/cache-observability`
- `/api/stats/canonical-request-segmentation`
- `/api/stats/cache-stability`
- `/api/stats/compression-observability`
- `/api/stats/compression-runtime`
- `/api/stats/compression-policies`
- `/api/stats/synthetic-cache-observability`
- `/api/stats/compression-tuning`
- `/api/stats/runtime`

Config/docs:

- `config.example.toml`
- `README.md`
- `docs/cache-compression.md`
- `docs/cache-compression-profiles.md`
- `docs/cache-compression-troubleshooting.md`
- `architecture/README.md` if request-shaping overview copy is duplicated there

## Proposed information architecture

### Tier 1: Operator status summary

Replace the current top summary card copy with a plain-language status section. Keep the existing `request_shaping_summary` payload shape unless a small additive field materially improves clarity.

Recommended cards:

1. `Request changes`: metric `Off`, `Observe only`, `Safe compression`, or `Mixed`. Subtext should make mutation state explicit: `No EggPool request mutation enabled`, `Analyzed N requests; no payload mutation`, or `Compressed N requests; saved M tokens`. Warn if failed fallbacks or stable-prefix mismatches are nonzero.
2. `Provider cache counters`: metric provider counter coverage percentage, or `—` when no known rows exist. Subtext: `Provider-reported rows only; missing counters are excluded`. This replaces the ambiguous `Cache controls` summary card.
3. `Compression effect`: metric actual saved tokens when safe mode applied, otherwise estimated observe savings. Subtext should distinguish `actual savings` from `estimated observe-only opportunity`.
4. `Safety guardrail`: metric `Clean` when fallbacks/mismatches/warnings are zero, otherwise a concise warning count. Subtext: `stable prefix preserved; fail-closed fallback on mismatch`.
5. `Routing isolation`: metric `Isolated` or `Unexpected`. Subtext: `cache/compression/tuning are not scorer inputs`. Warn if any guardrail flag unexpectedly reports true.
6. Optional `EggPool cache annotations`: metric `Off`, `Dry run`, or `Apply`; subtext `N candidates; M applied`. This must not be conflated with provider cache counter coverage.

### Tier 2: Normal operator panels

Keep these visible by default.

#### Provider cache counters

Rename current `Cache reporting` panel to `Provider cache counters`.

Terminology changes:

- `Cache hit ratio` -> `Reported cache read share`.
- `Reported` -> `Rows with cache counters` if space allows.
- `Not reported` -> `Rows without cache counters` if space allows.
- `Unknown shape` -> `Unrecognized payload shape`.
- `Cached input tokens (REPORTED)` -> `Provider-reported cached input tokens`.
- Account/model table column `Cached tokens (REPORTED)` -> `Provider-reported cached tokens`.

Add this explanation near the panel header:

`These counters come from upstream usage payloads. A provider that omits cache fields is counted as "without cache counters"; that does not prove no provider-side cache was used.`

Tooltip targets:

- Reported cache read share: `Calculated only from rows where the provider returned cache counters. Rows without cache fields are excluded from the denominator.`
- Rows without cache counters: `The upstream response was parseable but did not include recognized cache fields.`
- Unrecognized payload shape: `EggPool could not safely classify cache counters from this response shape.`

#### Compression

Merge the current `Compression opportunities` and `Safe compression` panels into one primary `Compression` panel.

Visible card set:

- `Mode` — off / observe / safe / mixed.
- `Analyzed` — observe-mode/analyzer request count.
- `Compressed` — safe-mode applied request count.
- `Tokens saved` — actual safe-mode savings if available; otherwise estimated opportunity with clear subtext.
- `Fallbacks` — fail-closed fallback count with warning styling when nonzero.
- Optional `p95 latency` if space allows and latency is useful for performance tuning.

Below cards, show one compact table containing total finalized requests, candidate segments, eligible segments, estimated savings in observe mode, actual savings in safe mode, analyzer latency p95, stable prefix preserved, and stable prefix mismatch.

Move transform breakdown and warning rollup under an advanced disclosure inside this panel labeled `Compression internals`.

Preserve all existing underlying values from `compression_observability` and `compression_runtime`; this should be a render-layer consolidation, not a stats contract change.

#### EggPool cache annotations

Rename current `Synthetic cache controls` panel to `EggPool cache annotations` or `Provider cache annotations`.

Lead with mutation state:

- `Mode`: off / dry run / apply.
- `Candidates`: eligible stable-prefix segments.
- `Dry-run annotations`: planned but not applied.
- `Applied annotations`: payload mutation count.
- `Warnings`: warning events.

Use this explanation:

`EggPool can add provider-specific cache annotations for supported upstreams. Dry run records where annotations would be added without mutating requests. Keep dry run enabled until candidates and warnings look clean.`

Avoid using `synthetic` as the main visible term. Keep `synthetic_cache_controls` in code/config/API names for backward compatibility, but use operator-facing copy that says `EggPool cache annotations`.

### Tier 3: Advanced diagnostics

Render the following sections collapsed by default unless they contain warnings, mismatches, nonzero parse failures, applied mutations, recommendations, or unexpected guardrail values:

- Request segmentation
- Native cache preservation
- Policy overrides
- Advisory tuning internals
- Routing guardrails
- Transcoding cache-related diagnostics if retained on this page

Prefer native `<details><summary>Advanced diagnostics</summary>...</details>` for no-JS progressive disclosure. If preserving expansion state across refreshes is desirable, add a tiny dashboard JS helper that stores expanded section IDs in `localStorage`. The default-expanded state must remain server-side deterministic: open automatically when there is a warning that requires operator attention.

Request segmentation should remain available, but its default presentation should be smaller. It is a safety primitive, not a normal operator action panel. Summarize segmented requests, not collected, parse failures, protected prefix requests, and volatile suffix candidate requests. Keep token/byte totals and per-model rows in the advanced body.

Routing guardrails should be collapsed when all flags are healthy. Show a one-line green statement in the summary: `Routing isolation: cache/compression/tuning are not scorer inputs`. Expand only for the full constant table.

Advisory tuning should not render a large full panel when disabled and there are no recommendations/overrides. Show only the summary card. Render the full panel when recommendation count or override count is nonzero, or when tuning mode is not off.

Policy overrides should hide the `<global>`-only table in the default view. Render a small statement: `No policy overrides active` when only the global sentinel exists. Show full policy table when operator-defined policies exist or warning/fallback counts are nonzero.

Native cache preservation should be collapsed unless transcoded request count is nonzero or the notes indicate a warning/degraded state.

## Config copy cleanup

`config.example.toml` is already improved, but it still includes a long advanced request-shaping section. Tighten it further so default operators see only normal knobs.

Recommended shape:

```toml
# ----------------------------------------------------------------------
# Request shaping: cache-preserving compression
# ----------------------------------------------------------------------
# Default: disabled / observe-only. Setting enabled=true lets EggPool
# analyze compression opportunities. Requests are not changed unless
# mode="safe". Safe mode only compresses eligible volatile-suffix text
# and falls back to the original payload if stable-prefix safety checks fail.
# [compression]
# enabled = false
# mode = "observe"                 # "observe" = report only; "safe" = apply volatile-suffix compression
# min_candidate_tokens = 2048       # minimum volatile-suffix size to consider
# min_savings_tokens = 1024         # minimum expected savings before applying safe compression
# max_compression_latency_ms = 25.0 # analysis/apply latency budget
#
# [compression.transforms]
# fold_repeated_lines = true
# compact_logs = true
# compact_search_results = true
# elide_base64_blobs = true
# minify_machine_json = true
# compact_stack_traces = true
#
# ----------------------------------------------------------------------
# Request shaping: EggPool cache annotations
# ----------------------------------------------------------------------
# Default: off and dry-run first. These annotations are provider-specific
# and should be validated in dry-run before apply mode.
# [cache.synthetic_cache_controls]
# enabled = false
# dry_run = true
# min_stable_tokens = 1024
```

Move detailed examples for `[[compression.policies]]` and `[compression.tuning]` out of `config.example.toml` unless maintainers strongly want one commented policy example. If one policy example remains, keep exactly one short example and point to `docs/cache-compression-profiles.md` for complete rollout profiles.

Use consistent labels in README/docs/config/dashboard:

- Provider cache counters
- Request segmentation
- Native cache preservation
- Compression
- EggPool cache annotations
- Policy overrides
- Tuning suggestions
- Routing isolation

Avoid mixing `cache controls` as a generic phrase when referring to provider-reported counters.

## Tooltip and explanation pass

Add or update tooltips where the dashboard already supports metric-card `tooltip` arguments.

Suggested tooltip text:

- `Request changes`: `Whether EggPool changed request payloads in this period. Observe mode analyzes only; safe mode may compress eligible volatile-suffix text.`
- `Provider cache counters`: `Coverage of upstream usage rows that included recognized cache fields. Missing provider fields are not counted as cache misses.`
- `Compression effect`: `Token reduction from safe compression when applied; otherwise estimated observe-only opportunity.`
- `Safety guardrail`: `Stable-prefix content must remain unchanged. If a safety check fails, EggPool sends the original request.`
- `Routing isolation`: `Cache, compression, synthetic-cache, and tuning metrics are not inputs to account selection.`
- `Rows with cache counters`: `The provider returned recognized cache usage fields.`
- `Rows without cache counters`: `The response was parseable but had no recognized cache fields.`
- `Reported cache read share`: `Calculated only from rows with recognized provider cache counters.`
- `Mode: observe`: `Analyzer ran but request payloads were not changed.`
- `Mode: safe`: `Eligible volatile-suffix content may be compressed; stable prefixes are protected.`
- `Fallbacks`: `Count of requests where EggPool abandoned mutation and sent the original payload.`
- `EggPool cache annotations`: `Provider-specific cache markers EggPool can add after routing. Dry run records candidates without changing requests.`

For docs/config option explanations:

- `compression.enabled`: `Enables analysis. Does not mutate requests unless mode is safe.`
- `compression.mode`: `observe records opportunities only; safe applies deterministic compression only to volatile suffix content.`
- `compression.min_candidate_tokens`: `Minimum volatile-suffix size before EggPool considers compression.`
- `compression.min_savings_tokens`: `Minimum expected token reduction before safe compression is applied.`
- `compression.max_compression_latency_ms`: `Latency budget for compression analysis/application; requests fall back unchanged if the budget is exceeded.`
- `compression.transforms.*`: `Deterministic cleanup transforms. Disable individual transforms only if one is noisy for your workload.`
- `cache.synthetic_cache_controls.enabled`: `Allows EggPool to add provider-specific cache annotations where supported. Keep dry_run=true first.`
- `cache.synthetic_cache_controls.dry_run`: `Records where annotations would be added, but does not change requests.`
- `cache.synthetic_cache_controls.min_stable_tokens`: `Minimum stable-prefix size before EggPool considers cache annotations.`

## Implementation phases

### Phase 1: Add UI terminology helpers

Create small helper functions in `render.py` for display labels and warnings rather than scattering string logic across panels.

Suggested helpers:

- `_display_request_change_mode(...)`
- `_display_synthetic_cache_mode(...)` or `_display_cache_annotation_mode(...)`
- `_routing_isolation_healthy(guardrails: dict[str, Any]) -> bool`
- `_compression_has_warnings(...) -> bool`
- `_advanced_section_open_by_default(section_name, metrics) -> bool`
- `_render_details_panel(summary: str, body: str, *, open: bool = False, id_: str | None = None) -> str`

Acceptance criteria:

- Labels are generated from existing payloads without changing stats contracts.
- Existing tests still pass.
- No user-provided text is rendered without escaping.

### Phase 2: Replace top summary cards

Refactor `_render_request_shaping_summary_panel` to produce the new plain-language cards.

Specific changes:

- Replace `Compression` with `Request changes` or add `Request changes` and demote the old compression summary to `Compression effect`.
- Replace `Cache controls` with `Provider cache counters`.
- Replace `Safety` with `Safety guardrail`, using `Clean` as the quiet-state metric.
- Replace `Advisory tuning` with `Tuning suggestions`.
- Replace `Routing` with `Routing isolation`.
- Do not mix synthetic cache candidates into provider cache counter coverage.

Acceptance criteria:

- A default disabled install communicates `no request mutation` without reading lower panels.
- Provider cache counter coverage and EggPool cache annotations are visually distinct.
- Routing isolation is shown as healthy when all guardrail flags are false.

### Phase 3: Merge compression panels

Create a consolidated `_render_compression_panel(...)` that consumes both `compression_observability` and `compression_runtime`.

Deprecate direct calls to `_render_compression_opportunities_panel` and `_render_compression_runtime_panel` from `render_cache`, or keep them private only for tests until removed.

Acceptance criteria:

- `/cache` renders one primary compression section instead of two adjacent compression sections.
- No metrics disappear; advanced transform/warning details remain available under disclosure.
- Fallbacks/stable-prefix mismatches are warning-styled and cause the advanced compression details to open by default.
- Observe-only and safe-mode windows both render sensible copy.

### Phase 4: Rename and simplify cache panels

Update `_render_cache_reporting_panel` and `_render_synthetic_cache_controls_panel` copy.

Acceptance criteria:

- `Cache reporting` becomes `Provider cache counters`.
- `Synthetic cache controls` becomes `EggPool cache annotations` in visible UI.
- `Cache hit ratio` becomes `Reported cache read share`.
- Visible copy explains that missing provider cache counters are not proof of no provider-side cache.
- API and config names remain unchanged for compatibility.

### Phase 5: Add advanced diagnostics disclosure

Group lower-priority panels into an advanced diagnostics area.

Recommended default visible order:

1. Summary
2. Provider cache counters
3. Compression
4. EggPool cache annotations
5. Advanced diagnostics disclosure

Advanced diagnostics contents:

- Native cache preservation
- Request segmentation
- Policy overrides
- Tuning suggestions internals
- Routing guardrails
- Transcoding

Acceptance criteria:

- Advanced diagnostics are collapsed on quiet/default installs.
- Advanced diagnostics auto-open when warnings, parse failures, stable-prefix mismatches, applied annotations, recommendations, or unexpected routing guardrails are present.
- No JavaScript is required for basic expand/collapse.
- If JavaScript is added for persisted disclosure state, it is optional and failure-safe.

### Phase 6: Config and docs copy pass

Prune `config.example.toml` request-shaping comments to the shorter operator-focused version.

Update README and docs terminology.

Potential doc edits:

- README Request shaping section: use the same labels as the dashboard.
- `docs/cache-compression.md`: add a short `Dashboard interpretation` section that maps the new cards to operator decisions.
- `docs/cache-compression-profiles.md`: host the longer policy/tuning examples moved out of `config.example.toml`.
- `docs/cache-compression-troubleshooting.md`: update symptom names to new UI labels.

Acceptance criteria:

- Example config exposes stable knobs only.
- Advanced policy/tuning examples are in docs, not the main example config, except for at most one minimal pointer.
- README, docs, config comments, and dashboard use consistent terminology.

### Phase 7: Tests and verification

Add/adjust tests for render output and safety copy.

Suggested tests:

- Default disabled summary includes `Request changes` and communicates no mutation.
- Provider cache counters panel does not use `Cache hit ratio` label anymore.
- Compression panel renders observe-only data without claiming mutation.
- Compression panel renders safe-mode data with actual savings and fallback warnings.
- EggPool cache annotations panel distinguishes dry-run from applied mutation.
- Advanced diagnostics are collapsed by default and open when warning inputs are present.
- Routing isolation copy renders when guardrails are healthy.

Run:

```bash
python -m pytest
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python -m ruff check .
python -m pyright src tests
```

Use the repo's actual test/lint commands if they differ from the above.

Manual verification:

- Start the dashboard with default config and visit `/cache`.
- Confirm the first screen answers whether EggPool is changing requests.
- Confirm cache counters, compression, and EggPool annotations are separate sections.
- Confirm a quiet/default install does not show a wall of advanced tables.
- Confirm changing period keeps the page stable.
- Confirm HTML escaping remains intact for provider/model/policy names.

## Risk notes

The main risk is accidentally changing meaning while changing labels. Keep API field names and config keys stable. Treat this as a presentation/copy refactor.

Do not remove advanced diagnostics outright; operators need them for rollout and bug reports. Collapse/demote them instead.

Do not let UI copy imply that observe mode mutates requests. `enabled=true` plus `mode="observe"` is analysis only.

Do not let UI copy imply missing provider cache counters mean no provider-side caching. They only mean EggPool did not receive recognized cache usage fields.

Do not expose raw request content while trying to improve explanations.

## Completion checklist

- [ ] `/cache` top summary uses plain-language operator cards.
- [ ] Provider-reported cache counters and EggPool cache annotations are visually and terminologically separate.
- [ ] Compression opportunities and safe compression runtime are consolidated into one panel.
- [ ] Advanced diagnostics are collapsed/demoted by default and auto-open on warnings.
- [ ] Tooltips explain the main cards and config concepts.
- [ ] `config.example.toml` request-shaping block is shorter and operator-focused.
- [ ] Advanced policy/tuning examples live in docs/profiles instead of cluttering the example config.
- [ ] README/docs use the same labels as the dashboard.
- [ ] Tests cover quiet/default, observe-only, safe-mode, dry-run, applied, and warning states.
- [ ] Full test/lint/typecheck suite passes.
