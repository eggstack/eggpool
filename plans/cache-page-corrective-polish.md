# Cache Page Corrective Polish Plan

## Context

The cache-page simplification work landed in `main` and substantially improved the dashboard shape. The `/cache` page now uses operator-facing vocabulary, merges the old compression-opportunities and safe-compression panels into one `Compression` panel, and moves lower-priority diagnostics under an advanced `<details>` disclosure.

A review of the current implementation shows that the large structural work is in place, but a few details need a corrective polish pass before this surface is fully coherent:

- the top summary reads some request-shaping fields by names that do not match the summary payload;
- provider cache counter coverage is still partially conflated with EggPool cache annotation candidates;
- quiet/default installs still show ambiguous main metrics such as `—` and raw `reporting_only` where the operator-level state should be `Clean` or `Isolated`;
- advanced diagnostics auto-open only for compression warnings or routing guardrail failures, not for several other warning/action states;
- a few visible labels remain partially old/internal (`Reported`, `Not reported`, `Unknown shape`, `Cached tokens (Reported)`);
- tests pin the existence of broad sections, but do not yet pin the key semantic corrections.

This plan is a corrective polish pass. It should preserve all request-path behavior, API field names, config keys, stats collection, routing scorer invariants, and protocol behavior.

## Goals

1. Make the top `/cache` summary semantically correct and useful on quiet/default installs.
2. Separate provider-reported cache counters from EggPool cache annotations everywhere in summary copy and card subtext.
3. Render quiet-state safety and routing as clear operator states (`Clean`, `Isolated`) instead of raw implementation values or `—`.
4. Broaden advanced diagnostics auto-open triggers to cover all actionable warning/non-default states.
5. Finish visible label cleanup in provider cache counter tables/cards.
6. Add targeted tests that prevent the regressions found in review.

## Non-goals

Do not change stats endpoint schemas.

Do not rename config keys such as `[cache.synthetic_cache_controls]` or API payload fields such as `cache_hit_ratio_known_only`.

Do not change compression behavior, synthetic cache annotation behavior, segmentation behavior, transcoding behavior, routing behavior, or database schema.

Do not remove advanced diagnostics. Collapse/demote them, but keep the data available.

Do not add raw prompt/request/response content to any dashboard or API output.

## Files likely involved

Primary:

- `src/eggpool/dashboard/render.py`
- `tests/unit/test_dashboard_cache_page.py`

Possible docs/copy follow-up if labels change:

- `README.md`
- `docs/cache-compression.md`
- `docs/cache-compression-troubleshooting.md`
- `config.example.toml`

## Detailed work items

### 1. Fix summary payload key mismatches

Current issue: `_render_request_shaping_summary_panel` reads `shaping_cache.get("reported_rows", 0)` and `shaping_cache.get("candidate_count", 0)`. The summary builder uses names like `cache_counter_reported_rows`, `cache_counter_known_rows`, and synthetic candidates live under the `synthetic_cache` section, not the `cache` section.

Required change:

- Read provider cache rows from `shaping_cache["cache_counter_reported_rows"]`.
- Read known provider cache rows from `shaping_cache["cache_counter_known_rows"]`.
- Do not read synthetic candidate counts from `shaping_cache`.
- If an EggPool cache annotation summary card is retained or added, read candidates from `shaping_synthetic["candidate_count"]`.

Suggested summary subtexts:

- Provider cache counters: `N provider-reported rows · M classified rows`.
- EggPool cache annotations: `N candidates · M dry run · K applied`.

Acceptance criteria:

- A summary payload with `cache_counter_reported_rows=12` renders `12` in the provider cache counter card subtext.
- A summary payload with `synthetic_cache.candidate_count=7` does not show `7 candidates` under provider cache counters.
- Synthetic candidates appear only under an EggPool cache annotation card/panel.

### 2. Add or restore a distinct EggPool cache annotations summary card

The implementation renamed the lower panel, but the top summary no longer clearly shows synthetic-cache/EggPool annotation mode. The earlier summary had synthetic state mixed into the old `Cache controls` card. The corrective version should make it a separate operator concept.

Required change:

Add a top summary card titled `EggPool cache annotations`, or equivalent short label, with:

- metric: `Off`, `Dry run`, `Apply`, or `Mixed` based on configured/observed synthetic mode;
- subtext: `N candidates · M dry run · K applied`; and
- warning state when synthetic warnings are nonzero.

Keep provider cache counter coverage separate.

Acceptance criteria:

- Provider cache counter card never mentions annotation candidates.
- EggPool annotation card never implies provider-reported cache-hit behavior.
- Dry-run mode is visibly distinct from applied mutation.

### 3. Improve quiet/default summary metrics

Current issue: `Safety guardrail` uses stable-prefix preserved rate as the main metric, which often renders `—` on quiet installs. `Routing isolation` uses raw mode such as `reporting_only` as its metric. Both are technically traceable, but not ideal operator summaries.

Required change:

- Safety card metric should be:
  - `Clean` when fallbacks, stable-prefix mismatches, synthetic warnings, policy warnings, and relevant parse failures are zero;
  - `Warnings` or `Needs review` when any warning trigger is present.
- Safety card subtext should include the details: `0 fallbacks · 0 policy warnings · 0 annotation warnings`, or a concise variant.
- Routing card metric should be:
  - `Isolated` when no routing guardrail violation exists;
  - `Unexpected` when any cache/compression/synthetic/tuning field is reported as a scorer input.
- Routing card subtext can include the raw mode: `mode reporting_only · cache/compression stay out of scorer`.

Acceptance criteria:

- A default/quiet render shows `Safety guardrail` with metric `Clean`.
- A default/quiet render shows `Routing isolation` with metric `Isolated`.
- Raw `reporting_only` may appear in subtext/details, but not as the primary routing summary metric.

### 4. Broaden advanced diagnostics auto-open triggers

Current issue: advanced diagnostics auto-open only for `_compression_has_warnings(...)` or unhealthy routing guardrails. This misses other actionable states.

Required change:

Create one helper that computes a structured advanced diagnostics state, for example:

```python
@dataclass(frozen=True, slots=True)
class CacheAdvancedState:
    open_by_default: bool
    warning: bool
    reason_count: int
    reasons: tuple[str, ...]
```

or a plain dict if keeping the renderer simple.

Trigger auto-open for at least:

- compression failed fallback count > 0;
- compression stable-prefix mismatch > 0;
- compression warning counts > 0;
- policy warning counts > 0;
- synthetic/EggPool annotation warning count > 0;
- synthetic/EggPool annotation applied count > 0, because actual request mutation is non-default and should be visible;
- segmentation parse failures > 0;
- tuning recommendation count > 0;
- tuning override count > 0;
- routing isolation unhealthy;
- native cache preservation notes indicating degraded/warning state, if such states are represented in payloads;
- transcoding loss warnings > 0, if transcoding remains in the advanced cache page.

Suggested summary text:

- quiet: `Show advanced diagnostics`;
- non-warning but active: `Advanced diagnostics (N active)`;
- warning: `Advanced diagnostics (N needs review)`.

Acceptance criteria:

- Synthetic annotation warnings open advanced diagnostics.
- Synthetic applied count opens advanced diagnostics even with zero warnings.
- Segmentation parse failures open advanced diagnostics.
- Tuning recommendations open advanced diagnostics.
- Healthy quiet/default data stays collapsed.

### 5. Finish provider cache counter label cleanup

Current issue: the provider cache counter panel heading is corrected, but several cards/table labels still use older shorthand.

Required visible label changes:

- Card `Reported` -> `Rows with cache counters`.
- Card `Not reported` -> `Rows without cache counters`.
- Card `Unknown shape` -> `Unrecognized payload shape`.
- Table `Cached input tokens (Reported)` -> `Provider-reported cached input tokens`.
- Account/model table `Cached tokens (Reported)` -> `Provider-reported cached tokens`.
- Protocol table columns may remain compact, but prefer `With counters`, `Without counters`, and `Unrecognized` if width allows.

Acceptance criteria:

- The old `Cache hit ratio` label does not return.
- The old visible card labels are either gone or only present in API/raw-code contexts where compatibility requires them.
- The panel copy explicitly says missing provider cache counters are not cache misses and do not prove provider-side cache absence.

### 6. Add targeted render tests

Extend `tests/unit/test_dashboard_cache_page.py` with semantic tests rather than only smoke tests.

Add tests for:

1. Provider cache summary uses canonical payload keys:
   - build a request shaping summary with `cache_counter_reported_rows=12` and `cache_counter_known_rows=20`;
   - assert the summary renders `12 provider-reported rows` or equivalent;
   - assert synthetic candidates do not appear under that card.

2. EggPool annotation summary is distinct:
   - summary has `synthetic_cache.candidate_count=7`, `dry_run_count=3`, `applied_count=0`;
   - assert `EggPool cache annotations` exists and includes `7 candidates`.

3. Quiet safety/routing states:
   - render with healthy guardrails and zero warnings;
   - assert `Clean` and `Isolated` are present near the respective cards.

4. Advanced diagnostics quiet default:
   - render quiet payloads;
   - assert `<details ... id="advanced-diagnostics"` does not include `open`.

5. Advanced diagnostics warning triggers:
   - segmentation parse failure opens advanced diagnostics;
   - synthetic warning opens advanced diagnostics;
   - tuning recommendation opens advanced diagnostics;
   - synthetic applied count opens advanced diagnostics.

6. Provider cache labels:
   - assert `Rows with cache counters`, `Rows without cache counters`, `Unrecognized payload shape`, and `Provider-reported cached tokens` render where appropriate.

7. Preserve escaping tests:
   - keep existing adversarial provider/account/model/policy tests.

Acceptance criteria:

- Tests fail on the current residual bugs and pass after corrective implementation.
- Tests do not depend on fragile whitespace or exact card ordering unless the ordering is intentional.

### 7. Optional docs/config copy micro-pass

If implementation changes visible labels beyond the already-landed names, update docs accordingly.

Likely edits:

- README request-shaping table and prose.
- `docs/cache-compression.md` dashboard interpretation section.
- Troubleshooting references to reported cache read share.
- `config.example.toml` comments if the summary/panel names changed.

Acceptance criteria:

- Docs continue to use the same operator-facing labels as the UI.
- Config comments still distinguish provider-reported counters from EggPool-added annotations.

## Suggested implementation order

1. Fix the top summary key reads and add a distinct EggPool annotation summary card.
2. Switch safety/routing primary metrics to `Clean`/`Isolated` quiet states.
3. Replace advanced-open logic with a single helper that accounts for all actionable triggers.
4. Finish provider cache counter label cleanup.
5. Add/adjust tests for each corrected behavior.
6. Run targeted tests, then full test/lint/typecheck suite.
7. Update docs only if visible labels changed beyond the already documented set.

## Verification commands

Use the repo's canonical commands if they differ, but minimally run:

```bash
python -m pytest tests/unit/test_dashboard_cache_page.py -q
python -m pytest tests/unit/test_routing_guardrails.py -q
python -m pytest -q
python -m ruff check .
python -m pyright src tests
```

Manual verification:

- Start the dashboard and open `/cache?period=24h` on a default/quiet config.
- Confirm first screen reads as no request mutation, provider cache counter coverage, safety clean, routing isolated.
- Confirm provider cache counters and EggPool cache annotations are separate sections/concepts.
- Confirm advanced diagnostics are collapsed on quiet data.
- Seed or simulate warning payloads and confirm advanced diagnostics open automatically.
- Confirm no raw request/provider content appears in rendered HTML.

## Risk notes

The highest risk is semantic drift in copy. Be precise:

- provider cache counters are upstream-reported usage fields;
- EggPool cache annotations are optional provider-bound request mutations;
- observe compression does not mutate;
- safe compression mutates only eligible volatile suffix content and fails closed;
- routing remains load-based and must not consume cache/compression/tuning metrics.

Do not solve these issues by changing stats payload schemas unless there is no alternative. Prefer render-layer adapters and helper functions.

## Completion checklist

- [ ] Summary uses `cache_counter_reported_rows` / `cache_counter_known_rows` correctly.
- [ ] Synthetic/EggPool annotation candidates are not shown under provider cache counters.
- [ ] Top summary includes a distinct EggPool cache annotations card or equivalent separate state.
- [ ] Safety quiet state renders `Clean` as the main metric.
- [ ] Routing quiet state renders `Isolated` as the main metric.
- [ ] Advanced diagnostics auto-open for compression, policy, synthetic annotation, segmentation, tuning, routing, native-cache, and transcoding warning/action states as applicable.
- [ ] Provider cache counter panel labels are fully operator-facing.
- [ ] Targeted render tests cover the corrected semantics.
- [ ] Escaping tests remain intact.
- [ ] Full relevant test/lint/typecheck suite passes.
