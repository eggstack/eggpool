# Cache Compression Phase 12 Polish Pass Plan

Date: 2026-07-03

Related plans:

- `plans/cache_compression_phase_10_11_review_pass.md`
- `plans/cache_compression_phase_11_replay_fixtures_regression_tests.md`
- `plans/cache_compression_phase_12_operator_docs_profiles.md`

## Summary

The cache-preserving deterministic compression roadmap is now broadly complete through Phase 12. The runtime path is in good shape: synthetic cache controls run post-route, streaming parity is covered, structural diff validation is stricter, Phase 10 tuning is recommendation-only, and operator-facing docs now exist.

This polish pass addresses the remaining quality issues discovered in the post-Phase-12 review:

1. `run_full_replay()` in the Phase 11 helper has misleading semantics for transcode fixtures: synthetic-cache replay still runs against the client-shape request and client-shape segmentation rather than the provider-bound/transcoded payload.
2. Most of the highest-value replay tests are gated behind `cache_compression_replay_full`; the default test suite may not cover a small smoke set of the cross-layer invariants unless CI explicitly selects the full marker.
3. The docs should clarify the difference between client-shape replay and provider-bound replay so future fixture authors do not accidentally test the wrong shape.

This is a polish/test-quality pass. It should not change production request behavior unless a test exposes a real defect.

## Non-goals

- Do not add new compression transforms.
- Do not enable compression, synthetic cache controls, or tuning by default.
- Do not implement Phase 10 apply-mode lifecycle.
- Do not add cache-aware routing.
- Do not broaden synthetic cache controls beyond Anthropic.
- Do not add live provider replay tests.
- Do not persist or commit real request bodies.

## Problem 1: `run_full_replay()` synthetic-cache semantics are client-shape

### Current issue

`tests/helpers/cache_compression_replay.py::run_full_replay()` currently does this order:

1. expand fixture;
2. segment the original client request using `client_protocol`;
3. run safe compression against the original client request;
4. if `synthetic_cache` is supplied, run `run_synthetic_cache_synthesis()` against the original request and original segmentation;
5. only afterwards run `run_transcode()` to collect transcode warnings.

That means a transcode fixture such as OpenAI-client to Anthropic-provider does **not** exercise the production Phase 9 shape. In production, synthetic cache controls run post-route against the provider-bound Anthropic payload and provider-bound Anthropic segmentation.

### Required behavior

For transcode fixtures, the helper should either:

- run synthetic-cache replay on the provider-bound payload by default, or
- expose two clearly named replay modes and make tests choose explicitly.

Recommended solution:

```python
@dataclass(frozen=True, slots=True)
class ReplayBundle:
    ...
    synthetic_cache_shape: str  # disabled | client_bound | provider_bound
    provider_bound_segmentation_status: str | None = None
    provider_bound_synthetic_cache_status: str | None = None
```

Update `run_full_replay()`:

1. Always run client-shape segmentation and safe compression as today.
2. If `client_protocol != target_protocol`, run `run_transcode()` before synthetic-cache synthesis and capture `provider_payload`.
3. If `synthetic_cache` is supplied:
   - if transcode produced a provider payload, run `segment_request(provider_payload, protocol=target_protocol)`;
   - run `run_synthetic_cache_synthesis()` against the provider payload and provider-bound segmentation;
   - set `synthetic_cache_shape="provider_bound"`.
4. If no transcode is needed, keep current behavior and set `synthetic_cache_shape="client_bound"`.
5. If transcode fails or returns no provider payload, set synthetic status to `provider_bound_unavailable` or equivalent test-local sentinel and do not attempt mutation.

Do not replace the durable/client segmentation fields in `ReplayBundle`; add provider-bound fields separately.

### Alternative acceptable solution

If changing `run_full_replay()` is too invasive, add a separate helper:

```python
def run_provider_bound_synthetic_replay(...): ...
```

Then update transcode replay tests to use the new helper explicitly. If this route is chosen, update the docstring of `run_full_replay()` to say it is client-shape unless callers invoke provider-bound helpers.

### Acceptance criteria

- Transcode fixtures can assert synthetic-cache status based on provider-bound payloads.
- OpenAI-client to Anthropic-provider fixture uses Anthropic segmentation for synthetic-cache candidate selection.
- Client-side segmentation remains available and unchanged.
- Replay bundle does not expose raw request or provider payload content.
- Existing non-transcode fixture behavior remains stable.

## Problem 2: default replay smoke coverage is too thin

### Current issue

Many high-value replay tests are under `@pytest.mark.cache_compression_replay_full`. That is acceptable for exhaustive matrix coverage, but a small smoke subset should run in normal CI/local `pytest` by default.

### Required behavior

Promote or duplicate a minimal smoke suite outside the full marker.

Recommended default smoke tests:

1. `test_smoke_openai_safe_suffix_preserves_prefix`
   - fixture: `openai/repeated_tool_output`
   - assert safe compression applies and stable-prefix content hash is unchanged.

2. `test_smoke_anthropic_nested_tool_result_compresses`
   - fixture: `anthropic/tool_result_nested_text_large`
   - assert production segmentation resolves the nested tool-result text path and safe compression applies.

3. `test_smoke_openai_to_anthropic_provider_bound_synthetic_dry_run`
   - fixture: `transcode/openai_client_to_anthropic_provider`
   - assert provider-bound synthetic-cache dry-run sees Anthropic candidate paths.

4. `test_smoke_native_cache_control_preserved_apply_mode`
   - fixture: `anthropic/system_blocks_native_cache`
   - assert native cache controls survive synthetic apply mode and are not duplicated.

5. `test_smoke_routing_guardrails_basic`
   - assert `QuotaFairScorer.score_accounts` signature remains the canonical scorer input set.

6. `test_smoke_fixture_sanitization`
   - run the lightweight forbidden-pattern linter over all fixture strings.

These should be cheap enough for the default suite. Keep the larger fixture matrix behind `cache_compression_replay_full`.

### Acceptance criteria

- Default `pytest` covers at least one replay case for OpenAI compression, Anthropic compression, provider-bound synthetic cache, native-cache preservation, routing guardrails, and fixture sanitization.
- Full replay matrix remains available behind `-m cache_compression_replay_full`.
- The default replay smoke set remains fast and deterministic.

## Problem 3: fixture docs need replay-shape clarity

### Current issue

The fixture README and operator docs describe provider-bound synthetic cache semantics accurately, but the helper API should make replay-shape expectations explicit for future contributors.

### Required docs updates

Update `tests/fixtures/cache_compression/README.md`:

- Define **client-shape replay**: segmentation/compression against the client request as received.
- Define **provider-bound replay**: transcode first, then segment/synthesize against the provider-bound payload.
- Explain when each is used:
  - safe suffix compression uses client-shape replay;
  - synthetic cache controls use provider-bound replay when protocols differ;
  - transcode fixtures should assert both client segmentation and provider-bound synthetic behavior.

Update helper docstrings:

- `run_full_replay()` should describe exactly which shape each pipeline step uses.
- `ReplayBundle` fields should identify whether synthetic-cache metrics came from client or provider-bound shape.

Update `docs/cache-compression.md` only if needed, preferably with one sentence pointing to the fixture README for test semantics. Avoid bloating operator docs with test internals.

### Acceptance criteria

- A new contributor can add a transcode fixture without accidentally running synthetic cache synthesis against the wrong payload shape.
- Helper names/docstrings do not imply provider-bound behavior unless actually performed.
- Operator docs stay focused on runtime semantics.

## Problem 4: strengthen provider-bound synthetic assertions

### Required test assertions

For the OpenAI-client to Anthropic-provider fixture:

- provider-bound payload exists after transcode;
- provider-bound segmentation protocol is `anthropic`;
- synthetic-cache dry-run status is `dry_run` when enabled and policy requirement is disabled or a matching provider policy is supplied;
- candidate paths are Anthropic-shaped, e.g. start with `system` or `tools`, not OpenAI `messages.*.content` paths unless the provider-bound Anthropic shape legitimately contains message content blocks;
- original OpenAI client payload has no synthetic `cache_control` additions;
- provider-bound payload is mutated only in synthetic apply mode, not dry-run.

For synthetic apply mode:

- added paths exactly equal candidate container `cache_control` paths;
- no text fields change;
- native `cache_control` remains unchanged.

### Acceptance criteria

- Tests fail if synthetic-cache replay regresses to client-shape for transcode fixtures.
- Tests fail if dry-run mutates either client or provider-bound payload.
- Tests fail if apply mode annotates non-candidate containers.

## Problem 5: CI/documented command clarity

### Tasks

Inspect project docs or CI config if available. Ensure one of the following is true:

- default CI runs the replay smoke tests, and nightly/full CI runs `-m cache_compression_replay_full`; or
- docs clearly state how to run both smoke and full replay suites locally.

Recommended docs snippet:

```bash
# Default smoke replay coverage, included in normal pytest
uv run pytest tests/unit/test_replay_fixtures_regression.py tests/unit/test_replay_fixtures_sanitization.py

# Full cache/compression replay matrix
uv run pytest -m cache_compression_replay_full tests/unit/test_replay_fixtures_regression.py
```

If the repo has GitHub Actions, add or adjust a workflow only if consistent with existing CI practice. Do not introduce slow full replay into every quick PR job unless runtime remains acceptable.

## Implementation order

1. Update replay helper semantics or add explicit provider-bound helper.
2. Add provider-bound synthetic replay tests for OpenAI-to-Anthropic fixture.
3. Promote minimal smoke tests outside the full marker.
4. Update fixture README and helper docstrings.
5. Confirm routing guardrail smoke remains cheap.
6. Run focused tests.
7. Run full validation.

## Focused test commands

```bash
uv run pytest tests/unit/test_replay_fixtures_regression.py -q
uv run pytest tests/unit/test_replay_fixtures_sanitization.py -q
uv run pytest -m cache_compression_replay_full tests/unit/test_replay_fixtures_regression.py -q
uv run pytest tests/unit/test_cache_synthesis.py -q
uv run pytest tests/unit/test_routing_guardrails.py -q
```

Then full validation:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

## Acceptance criteria

- `run_full_replay()` or an explicit provider-bound helper correctly exercises synthetic cache controls on provider-bound payloads for transcode fixtures.
- Replay bundle records whether synthetic-cache status came from client-bound or provider-bound replay.
- Default tests include a cheap smoke subset for the most important cache/compression invariants.
- Full replay matrix remains available behind `cache_compression_replay_full`.
- Fixture docs and helper docstrings clearly explain replay shape semantics.
- No raw prompt/tool/provider content is exposed by replay bundle fields or docs.
- No production routing or request-shaping behavior changes unless a test uncovers a real bug.
- Full tests, ruff, and pyright pass.

## Rollback guidance

This polish pass should be tests/docs/helper-only. If the provider-bound helper change causes test churn, keep the old helper behavior under a clearly named `run_client_shape_replay()` and introduce `run_provider_bound_replay()` separately. Do not weaken production Phase 9 runtime behavior; it is already post-route and provider-bound.