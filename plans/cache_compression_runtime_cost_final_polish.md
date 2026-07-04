# Cache Compression Runtime and Cost Final Polish Plan

Date: 2026-07-03
Repository: `eggstack/eggpool`
Status: handoff plan
Priority: polish / verification

Related work:

- `plans/cache_compression_runtime_cost_cleanup_followup.md`
- `plans/2026-07-03-reservation-fallback-floor-removal-followup.md`
- `plans/cache_compression_config_runtime_cleanup.md`

## Summary

The request-shaping/cost cleanup line is now mostly closed. The critical reservation-floor bug appears fixed, finalizer-level regression tests exist, generated config examples are schema-valid and operator-shaped, the overview has a compact Request Shaping card, and the Runtime page now hides detailed request-shaping diagnostics behind an `Advanced request-shaping details` disclosure.

This final polish pass should remove the remaining phase-era internal text, tighten tests around the final UI/config contract, and verify full validation/CI visibility. It should be a low-risk pass: comments, tests, small docs, and validation only unless a test exposes a real defect.

## Non-goals

- Do not change canonical cost precedence again unless a test exposes a bug.
- Do not remove existing stats endpoints.
- Do not remove advanced request-shaping diagnostics.
- Do not remove backward-compatible config fields.
- Do not implement tuning apply lifecycle.
- Do not add cache-aware routing.
- Do not change default request mutation behavior.

## Current good state

- `RequestFinalizer` no longer floors lower plausible estimated local cost back to reservation.
- MiniMax regression fixture is pinned in `tests/unit/test_request_finalizer.py`.
- `config.example.toml` and `src/eggpool/_share/config.example.toml` validate and no longer expose bad tuning keys.
- Config examples now use product/operator language around request shaping instead of Phase 5/12 implementation prose.
- Overview page renders a compact `Request shaping` metric card.
- Runtime page renders the summary panel by default and collapses detailed request-shaping panels under `<details class="advanced-request-shaping">`.

## Remaining polish items

### Item 1: remove phase-era comments from request-shaping UI internals

The remaining phase language is mostly internal comments in `src/eggpool/dashboard/render.py`, for example comments around routing guardrails and historical compression sections. These are not currently operator-facing, but they keep the renderer hard to reason about and invite future phase-shaped UI regressions.

Replace comment labels such as:

- `Phase 7 compression observability card`
- `Phase 7 compression runtime card`
- `Phase 8 routing-guardrails panel`
- `Phase 9 synthetic cache controls card`
- `Phase 10 closed-loop threshold tuning card`

with product/component language:

- `Compression opportunity panel`
- `Safe compression runtime panel`
- `Routing guardrails panel`
- `Synthetic cache controls panel`
- `Advisory tuning panel`

Do not rewrite plan documents; plan files can keep phase names.

Acceptance:

- `src/eggpool/dashboard/render.py` request-shaping section contains no `Phase N` comments.
- Rendered HTML remains unchanged except comments do not render anyway.
- No production behavior changes.

### Item 2: pin generated example phase-free request-shaping text

The config examples now look operator-shaped. Add an explicit test to prevent regression.

Target: `tests/unit/test_config.py`.

Add a focused test similar to:

```python
def test_config_examples_request_shaping_section_is_operator_facing() -> None:
    for path in ("config.example.toml", "src/eggpool/_share/config.example.toml"):
        text = Path(path).read_text()
        section = text.split("Request shaping surfaces", 1)[-1]
        for bad in ("Phase 5", "Phase 6", "Phase 7", "Phase 9", "Phase 10", "Phase 12"):
            assert bad not in section
```

Scope the assertion to generated config examples only. Do not scan the whole repo because plan files and historical docs legitimately reference phases.

Acceptance:

- Both generated config examples validate.
- Known-bad tuning keys remain absent.
- Request-shaping sections in generated examples stay phase-free.

### Item 3: pin Runtime disclosure structure

There are already dashboard tests around runtime request-shaping surfaces. Add or tighten a test to specifically enforce the final layout contract.

Target: `tests/unit/test_dashboard_phase7.py` or a renamed future test module if desired.

Assert:

- rendered Runtime HTML contains `<details class="advanced-request-shaping">`;
- rendered Runtime HTML contains `<summary>Advanced request-shaping details</summary>`;
- `Request shaping` appears before `Advanced request-shaping details`;
- detailed labels such as `Compression opportunities`, `Safe compression`, `Synthetic cache controls`, `Advisory tuning`, and `Routing guardrails` are inside the details block;
- operator-facing rendered HTML does not contain `Phase 5`, `Phase 7`, `Phase 9`, or `Phase 10`.

Use an HTML parser or robust string slicing; avoid brittle full HTML snapshots.

Acceptance:

- Runtime page defaults to summary-first layout.
- Advanced request-shaping details remain available.
- Phase-era labels do not leak into rendered Runtime HTML.

### Item 4: reconcile tuning wording

Check all operator-facing tuning text for consistency. The current product contract should be one of the following, and docs/UI/tests must agree.

Recommended final contract:

- Tuning is advisory/recommendation-first.
- `mode = "recommend"` surfaces recommendations only.
- `mode = "apply"` is reserved/dormant unless the implementation really registers runtime overrides in production.
- Tuning never inspects raw prompt content and never affects routing.

If production apply mode is still not wired, remove UI wording that implies live apply overlays are active. In particular, avoid language like “mode = apply overlays bounded runtime overrides” unless the background lifecycle is implemented and tested.

Acceptance:

- `docs/cache-compression.md`, `docs/cache-compression-profiles.md`, generated examples, and Runtime UI use consistent tuning language.
- Tests do not assert contradictory apply-mode behavior.
- If apply mode is dormant, Runtime says `Recommend-only` / `reserved`, not active overlay.

### Item 5: verify cost repair/stat warning after finalizer fix

The finalizer bug is fixed, but verify historical repair visibility remains correct.

Inspect and, if necessary, strengthen tests around:

- `src/eggpool/cost_repair.py`
- `tests/unit/test_cost_repair.py`
- `tests/unit/test_stats.py`
- dashboard reservation fallback warning tests

Acceptance:

- Historical row where canonical cost equals reservation and lower local estimated cost exists is flagged/repaired.
- Clean newly finalized MiniMax fixture row is not flagged as reservation fallback.
- Provider-reported rows are never flagged.
- Warning card still renders for unrepaired historical suspicious rows.
- Warning disappears after repair or clean data.

### Item 6: verify CI/check visibility

The GitHub combined status API has repeatedly returned no statuses. That may be normal for this repo, but it makes handoff reviews less reliable.

Tasks:

1. Inspect `.github/workflows/` if present.
2. If workflows exist, verify they run on pushes to `main` or PRs.
3. If no workflows exist, add a small plan note or issue for CI setup rather than mixing workflow creation into this polish pass.
4. At minimum, document local validation commands in the final PR/commit message or handoff.

Recommended local validation:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

Focused validation:

```bash
uv run pytest tests/unit/test_request_finalizer.py -q
uv run pytest tests/unit/test_cost_repair.py -q
uv run pytest tests/unit/test_stats.py -q
uv run pytest tests/unit/test_config.py -q
uv run pytest tests/unit/test_dashboard.py -q
uv run pytest tests/unit/test_dashboard_phase7.py -q
```

Acceptance:

- Handoff can state whether CI exists and whether checks ran.
- If CI is absent, this is explicitly documented as a repo-level follow-up, not silently ignored.

## Suggested implementation order

1. Remove phase-era comments from `src/eggpool/dashboard/render.py` request-shaping section.
2. Add generated-config phase-free request-shaping test.
3. Add Runtime advanced-disclosure structure test.
4. Reconcile tuning wording across Runtime UI/docs/config examples.
5. Tighten cost repair/stat warning tests if any gap remains.
6. Inspect CI/workflow status and document the outcome.
7. Run focused tests.
8. Run full validation.

## Final acceptance criteria

- Finalizer cost precedence remains unchanged and regression-tested.
- Generated config examples validate and remain phase-free in request-shaping sections.
- Runtime request-shaping UI is summary-first with advanced details collapsed.
- Rendered Runtime HTML contains no phase-era labels.
- Tuning language is consistent with actual implementation.
- Historical reservation-fallback repair/warning behavior remains intact.
- CI/check visibility is documented or a follow-up is created.
- Full ruff, pyright, and pytest pass locally or in CI.

## Rollback guidance

This pass should be mostly comments/tests/docs. If a UI test becomes brittle, keep the disclosure structure but loosen the assertion to semantic substrings. Do not roll back the finalizer floor removal or the config example validation fixes.