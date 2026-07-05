# EggPool Performance Closure Pass Plan

## Purpose

This plan closes the correctness-preserving performance optimization line after the corrective pass landed. The repo now has the core hot-path changes in place: prepared transcode reuse, resolved-policy segmentation gating, single-pass routing plans, trace write controls, request/dashboard/performance test markers, and focused validation documentation.

This closure pass should avoid expanding scope. Its purpose is to verify the landed behavior, clean up any remaining ambiguity, and make the optimization line maintainable for future contributors.

## Current state to preserve

The implementation now appears to have these desired properties:

- Compression policy resolution happens before segmentation gating.
- Segmentation gating reads the effective resolved compression policy rather than the raw global config.
- Skipped segmentation is represented with `segmentation_not_collected` instead of being conflated with `empty_request`.
- Transcode preflight encodes translated body once via `encode_json_body()` and uses a separate padded body for context-limit checking.
- `PreparedTranscode` reuse is active for safe non-thinking transcode cases and recomputes for thinking-bearing requests.
- The coordinator uses `Router.build_routing_plan()` as the authoritative selection path without falling back to legacy `select_account()` selection.
- `routing.trace.mode` now advertises only implemented modes: `all`, `sampled`, and `off`.
- `request_path`, `dashboard`, and `performance` pytest markers are registered.
- `AGENTS.md` documents focused verification commands.

The closure pass should prove these properties with targeted tests and documentation, not rely on code comments alone.

## Non-goals

- Do not add new routing modes.
- Do not reintroduce `routing.trace.mode = "errors"` unless fully implemented in a separate plan.
- Do not make performance thresholds hard CI blockers unless they are stable across local and CI hardware.
- Do not broaden compression, cache, or synthetic-cache behavior.
- Do not change provider eligibility semantics.
- Do not alter cost/pricing/accounting behavior.
- Do not mix unrelated dashboard redesign work into this closure pass.

## Closure item 1: Verify the full local command set

### Goal

Make sure the documented verification commands actually pass and are sufficient for handoff.

### Tasks

1. Run the full documented pre-commit sequence:

   ```bash
   uv sync --extra dev
   uv run ruff format --check src/ tests/ scripts/
   uv run ruff check src/ tests/ scripts/
   uv run pyright src/ scripts/
   uv run pytest
   ```

2. Run the focused subsets documented in `AGENTS.md`:

   ```bash
   uv run pytest -m request_path -v
   uv run pytest -m dashboard -v
   uv run pytest -m performance -v
   ```

3. Confirm `pytest -m performance` selects the new perf tests. If the perf tests still use only an older marker such as `perf_baseline`, either:
   - add `@pytest.mark.performance` to those tests/classes/files, or
   - document the actual marker name and register it in `pyproject.toml`.
4. Confirm no tests emit `PytestUnknownMarkWarning`.
5. Confirm all new tests added for prepared transcode and segmentation guard are included in the `request_path` subset.
6. Capture any failures in this plan’s implementation PR description, including whether they are existing unrelated failures or introduced by this line.

### Acceptance criteria

- All documented commands either pass or have clearly documented existing failures with issue references.
- Focused markers select the intended files.
- No unknown pytest markers remain.

## Closure item 2: Tighten `PreparedTranscode` diagnostics mutability

### Goal

Keep prepared dispatch data effectively immutable while retaining coordinator diagnostics.

### Problem

`PreparedTranscode` became mutable so the coordinator can set `reused` and `recompute_reason`. That is practical, but it weakens the separation between immutable prepared dispatch data and mutable observability state.

### Preferred implementation

1. Introduce a small mutable diagnostics dataclass, for example:

   ```python
   @dataclass(slots=True)
   class PreparedTranscodeDiagnostics:
       available: bool = True
       reused: bool = False
       recompute_reason: str | None = None
   ```

2. Make `PreparedTranscode` frozen again if feasible.
3. Store diagnostics on `PreparedTranscode` as a field, or store it separately on `ProxyRequestContext`, for example `prepared_transcode_diagnostics`.
4. Ensure the coordinator mutates only diagnostics, never `translated_payload`, `translated_body`, `warnings`, `tool_token_padding`, `loss_policy_used`, or `features_fingerprint`.
5. Keep the stable recompute reasons set:
   - `no_prepared_result`
   - `protocol_or_features_mismatch`
   - `thinking_controls_present`
   - `transcoder_missing`
6. Update tests to assert dispatch fields are not mutated by reuse/recompute diagnostics.

### Acceptable minimal implementation

If a separate diagnostics object is too invasive for this closure pass, add explicit tests proving the coordinator never mutates `translated_payload`, `translated_body`, or `warnings`, and add a comment explaining why only `reused` / `recompute_reason` are mutable.

### Acceptance criteria

- Dispatch payload/body fields are protected from accidental mutation.
- Reuse/recompute observability remains available.
- Tests cover both reuse and recompute branches.

## Closure item 3: Strengthen prepared transcode integration tests

### Goal

Move beyond unit-level simulations and verify the request coordinator behavior in integration-style tests.

### Tasks

1. Add a fake or spy transcoder fixture that counts `encode_request()` calls.
2. Verify common non-thinking transcode path:
   - preflight runs once,
   - coordinator reuses prepared body,
   - dispatch uses the prepared body,
   - warnings are appended exactly once,
   - upstream body has no padding bytes.
3. Verify thinking-bearing path:
   - preflight may run,
   - coordinator recomputes rather than reusing stale budget mapping,
   - provider-specific thinking budget behavior remains unchanged,
   - recompute reason is `thinking_controls_present`.
4. Verify feature mismatch path:
   - prepared result is not reused when `TranscoderFeatures` fingerprint differs,
   - recompute reason is `protocol_or_features_mismatch`.
5. Verify `loss_policy = "reject"` rejects before request/reservation/attempt rows are created.

### Acceptance criteria

- Tests prove the coordinator path, not only the `PreparedTranscode` dataclass behavior.
- Warning duplication and padding leakage are impossible under test.

## Closure item 4: Verify segmentation finalization and dashboard semantics

### Goal

Ensure `segmentation_not_collected` is persisted and rendered honestly everywhere it matters.

### Tasks

1. Add or review finalizer tests proving:
   - `segmentation_not_collected=True` stores `segmentation_status = 'not_collected'`.
   - `segmentation_result=None` after a failed segmentation run does not get mislabeled as intentionally skipped unless `segmentation_not_collected=True`.
   - a real empty segmentation still stores `empty_request` or the current equivalent empty status.
2. Add or review dashboard tests proving:
   - cache page displays not-collected segmentation separately from zero activity,
   - runtime page does not show misleading compression/cache zeros when segmentation was intentionally skipped,
   - trace-off/sampled modes do not appear as missing request data.
3. Add at least one request-path test where global compression is disabled but a scoped policy enables observe/safe and segmentation runs.
4. Add at least one request-path test where global compression is enabled but scoped policy disables compression and segmentation can be skipped when no other consumer needs it.

### Acceptance criteria

- Dashboard and API surfaces distinguish disabled, not-collected, empty, sampled/off, and error states.
- Compression never runs without required segmentation.

## Closure item 5: Confirm routing-plan equivalence and no legacy fallback remains

### Goal

Prove `Router.build_routing_plan()` is the single authoritative selection path and preserves routing semantics.

### Tasks

1. Search for coordinator hot-path calls to legacy selection helpers.
2. Confirm `_select_and_persist_attempt()` no longer calls `select_account()` as a fallback.
3. Keep `select_account()`, `get_eligible_account_names()`, and `select_accounts_for_failover()` only as public/helper APIs or compatibility wrappers if still needed by tests/dashboard; they must not re-enter the coordinator hot path.
4. Add or verify tests for:
   - equal same-provider accounts rotate under `round_robin`,
   - `random` is restricted to the near-tie fairness band,
   - `off` is score ordered,
   - provider-suffixed requests cannot cross provider boundaries,
   - thinking-required requests reject correctly when no eligible provider supports the capability,
   - retry exclusion uses attempted accounts without rebuilding a divergent plan.
5. Verify missing-account recovery scheduling remains rate-limited and fires no more than once per attempt.

### Acceptance criteria

- There is one authoritative request-path selection call.
- Existing public router methods remain behaviorally consistent with `build_routing_plan()` or are clearly documented as compatibility helpers.
- The previous all-traffic-to-one-account regression class remains pinned.

## Closure item 6: Confirm trace write-mode behavior and docs

### Goal

Ensure routing trace config and docs match runtime behavior exactly.

### Tasks

1. Confirm `RoutingTraceConfig.mode` accepts only `all`, `sampled`, and `off`.
2. Confirm `config.example.toml`, shared config examples, README, architecture docs, and AGENTS do not advertise `errors` mode.
3. Verify tests for:
   - `all` writes traces for every attempt,
   - `off` writes no trace rows but request/reservation/attempt/finalization rows remain intact,
   - `sampled` is deterministic under seeded/hash-based request IDs,
   - `include_score_components=false` writes trace rows without large score component JSON,
   - dashboard handles no trace rows as intentional when mode is `off`.
4. Confirm any comments that still say `sampled = successful traces + all errors` are either implemented or reworded. If sampled currently applies at selection time and cannot force all errors, docs should say that precisely.

### Acceptance criteria

- Config, docs, comments, and tests describe the same behavior.
- Trace write controls cannot be confused with core accounting controls.

## Closure item 7: Check logging and middleware cleanup claims

### Goal

Validate Phase 5 closure claims in documentation and code.

### Tasks

1. Confirm whether `BaseHTTPMiddleware` replacement actually landed.
2. If it landed, add/verify tests for:
   - oversized request returns the same 413 error body/status,
   - header redaction still applies,
   - streaming responses are not buffered,
   - response headers remain protocol-compatible.
3. If it did not land, update AGENTS/architecture docs to avoid claiming Phase 5 is complete.
4. Confirm routine request logs are DEBUG or configurable if docs claim so. If `logger.info("Proxying ...")` remains on the hot path, either:
   - intentionally leave it and document Phase 5 as incomplete, or
   - move it to DEBUG/sampled INFO in this closure pass.
5. Keep warnings/errors at INFO/WARNING/ERROR as appropriate.

### Acceptance criteria

- Documentation matches actual middleware/logging behavior.
- No streaming behavior regresses.

## Closure item 8: CI/status visibility

### Goal

Make verification visible to handoff reviewers.

### Tasks

1. Inspect `.github/workflows/` if present.
2. If CI exists, confirm it runs at least:
   - ruff format check,
   - ruff lint,
   - pyright,
   - pytest.
3. If CI does not exist, either add a minimal GitHub Actions workflow or explicitly document that verification is local-only.
4. Confirm GitHub exposes statuses/checks for PR commits or `main` after workflows run.
5. Keep perf timing diagnostic; do not enforce wall-clock thresholds in default CI.

### Acceptance criteria

- Maintainers can see whether request-path closure checks passed.
- Lack of CI is explicit if intentionally omitted.

## Closure item 9: Final documentation sweep

### Goal

Remove stale statements from the performance plans/docs now that corrective decisions landed.

### Tasks

1. Update `plans/2026-07-05-performance-corrective-pass.md` if implementation chose de-advertising `errors` mode rather than completing it.
2. Update `plans/performance_optimization_safety_plan.md` with a short closure note, or leave plans immutable and add a new `docs/performance-optimization-closure.md` summary.
3. Ensure docs consistently say:
   - trace modes are `all`, `sampled`, `off`,
   - segmentation gating uses resolved effective compression policy,
   - prepared transcode reuse skips duplicate encode only for safe cases,
   - thinking controls force recompute when provider-specific budget mapping may matter,
   - routing stays load-based and never consumes cache/compression/synthetic-cache metrics.
4. Ensure config examples do not include removed or misleading options.

### Acceptance criteria

- New contributors can understand what landed and what remains intentionally out of scope.
- No plan/doc says a mode or behavior exists when it does not.

## Suggested execution order

1. Run documented verification commands and fix marker/test selection issues first.
2. Audit docs/comments for stale `errors` mode, Phase 5 logging/middleware claims, and performance marker names.
3. Add/strengthen integration-style prepared transcode tests.
4. Add/strengthen segmentation finalizer/dashboard tests.
5. Confirm routing-plan hot path has no legacy fallback and add any missing route-equivalence tests.
6. Decide whether to split `PreparedTranscode` diagnostics from immutable dispatch data.
7. Add or document CI/status visibility.
8. Do final docs sweep.

## Required verification matrix

| Surface | Required closure check |
| --- | --- |
| Preflight transcode | Single compact encoded body; padding separate from dispatch; warnings once |
| Thinking transcode | Thinking-bearing requests recompute; provider budget semantics unchanged |
| Segmentation guard | Effective resolved policy controls gating |
| Finalizer | `not_collected` distinct from `empty_request` and failure |
| Dashboard/cache page | Optional/skipped observability rendered honestly |
| Routing | `build_routing_plan()` is authoritative; fairness/provider/capability parity |
| Trace config | `all`/`sampled`/`off` behavior matches docs and tests |
| Middleware/logging | Docs match actual code; streaming remains unbuffered |
| Verification | documented commands pass or failures are recorded |
| CI/status | visible or explicitly documented as local-only |

## Definition of done

This closure pass is complete when:

- Full and focused verification commands are accurate and runnable.
- The prepared transcode implementation is protected against accidental dispatch-data mutation.
- Integration tests prove prepared reuse/recompute behavior.
- Segmentation skipped/empty/error states are persisted and rendered distinctly.
- The coordinator has one authoritative routing selection path.
- Trace-mode docs and config match runtime behavior.
- Phase 5 claims about logging/middleware are either implemented or corrected.
- CI/status visibility is available or local-only verification is explicitly documented.
- No further obvious request-path correctness risks remain from the performance optimization line.
