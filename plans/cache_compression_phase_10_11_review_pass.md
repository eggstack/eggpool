# Cache Compression Review Pass: Phase 9/10 Closure Verification

Date: 2026-07-03

Related plans:

- `plans/cache_compression_phase_09_synthetic_cache_controls.md`
- `plans/cache_compression_phase_10_closed_loop_threshold_tuning.md`
- `plans/cache_compression_phase_09_10_corrective_pass.md`
- `plans/cache_compression_phase_11_replay_fixtures_regression_tests.md`
- `plans/cache_compression_phase_12_operator_docs_profiles.md`

## Summary

The Phase 9/10 corrective pass moved synthetic cache controls post-route, normalized internal paths to tuples, made TTL strict, isolated synthetic-cache overlay fields from compression config overlay, added structural-diff fail-closed checks, and clarified that Phase 10 tuning is recommendation-only.

This review pass is a focused verification step before continuing with replay fixtures and operator documentation. It should prove that the implementation works across the high-risk live request shapes, especially streaming requests and OpenAI-client-to-Anthropic-provider transcoding.

The expected outcome is either:

1. confirm the current implementation is ready for Phase 11 replay fixture codification, or
2. produce a short corrective patch for any issues found.

## Review goals

- Verify synthetic cache controls run post-route for both streaming and non-streaming requests.
- Verify provider-bound segmentation is used for synthetic-cache candidate selection.
- Verify OpenAI-client to Anthropic-provider flow can dry-run and apply synthetic cache controls correctly.
- Verify native Anthropic `cache_control` is preserved and never duplicated.
- Verify structural-diff safety check fails closed on unexpected mutation.
- Verify Phase 10 tuning is recommendation-only in all production paths.
- Verify docs/config examples match actual runtime behavior.
- Verify routing guardrails still hold after the post-route hook.

## Non-goals

- Do not add new compression transforms.
- Do not enable synthetic cache controls by default.
- Do not implement Phase 10 runtime apply lifecycle.
- Do not add cache-aware routing.
- Do not broaden synthetic cache providers beyond Anthropic.
- Do not change context-limit check ordering.

## Review area 1: streaming-path parity

### Risk

The current post-route synthetic-cache hook was verified in the non-streaming path, where `_apply_selected_provider_transcode_adjustments()` is followed by `_apply_synthetic_cache_controls()` before `client.build_request(...)`. Many coding-agent requests stream, so the streaming path must run the same hook before dispatch.

### Tasks

1. Inspect `_execute_streaming()` in `src/eggpool/request/coordinator.py`.
2. Confirm it calls `_apply_selected_provider_transcode_adjustments()` and `_apply_synthetic_cache_controls()` before building the upstream request.
3. If the hook is missing, add it with the same capability-rejection cleanup behavior used by non-streaming.
4. Ensure streaming finalization persists `synthetic_cache_result` on success, cancellation, upstream error, and exhausted retry paths.
5. Add or update tests that exercise synthetic cache dry-run/apply on streaming requests.

### Acceptance criteria

- Streaming and non-streaming request paths both run synthetic-cache synthesis post-route.
- Streaming synthetic-cache result is persisted in finalization metadata.
- Capability rejection cleanup remains correct.
- No route reselection occurs after synthetic-cache synthesis.

## Review area 2: OpenAI client to Anthropic provider

### Risk

The intended high-value Phase 9 path is an OpenAI-compatible client request routed to an Anthropic-compatible provider through the transcoder. Synthetic cache controls must operate on the Anthropic provider-bound body, not the OpenAI client body.

### Tasks

1. Create a focused integration test using an OpenAI request that routes to an Anthropic-capable provider.
2. Configure a matching policy with:

```toml
[[compression.policies]]
name = "openai-to-anthropic-synthetic-dry-run"
match_provider_kinds = ["anthropic"]
synthetic_cache_controls = true
synthetic_cache_dry_run = true
synthetic_cache_min_stable_tokens = 0
```

3. Verify `context.synthetic_cache_segmentation` uses `protocol="anthropic"`.
4. Verify `synthetic_cache_result.status == "dry_run"`.
5. Verify summary JSON records target protocol/provider correctly if such fields exist; add them if they are absent.
6. Repeat with `synthetic_cache_dry_run = false` and assert the provider-bound Anthropic payload gains `cache_control` only at eligible stable-prefix containers.

### Acceptance criteria

- OpenAI client body is never directly annotated.
- Anthropic provider-bound body is the only mutation target.
- Candidate paths are Anthropic-shape paths.
- Native/client segmentation remains persisted separately.
- Selected account/provider does not change after synthesis.

## Review area 3: native cache-control preservation

### Risk

Native cache controls must be preserved and never duplicated. The corrective pass normalized path representation, but this needs explicit coverage across system blocks, tools, and message content blocks.

### Tasks

Add tests for:

- top-level Anthropic `system[]` block with existing `cache_control`;
- Anthropic `tools[]` definition with existing `cache_control`;
- Anthropic `messages[].content[]` block with existing `cache_control`, even if message-block placement is not currently selected;
- mixed native + synthetic candidates where only unannotated containers receive synthetic controls.

### Acceptance criteria

- Existing `cache_control` objects are byte-for-byte preserved.
- No duplicate `cache_control` is added to native-controlled containers.
- `WARN_EXISTING_NATIVE_PRESERVED` is emitted once per request class, not spammed per block unless the design intentionally counts per block.
- Summary JSON exposes paths only, not content.

## Review area 4: structural-diff fail-closed behavior

### Risk

The structural diff currently accepts any added path whose last component is `cache_control`. That prevents arbitrary text edits, removals, and non-cache additions, but it may be too permissive if `cache_control` is added to a non-candidate container.

### Tasks

1. Confirm the diff check validates added `cache_control` paths against the candidate container paths, not merely `p[-1] == "cache_control"`.
2. If it currently only checks suffix, tighten it:

```python
allowed_added_paths = {
    list(container_path) + ["cache_control"]
    for container_path in candidate_container_paths
}
```

3. Add tests that mutate:
   - an arbitrary non-cache field;
   - a text field;
   - a volatile suffix container;
   - a `cache_control` field on a non-candidate container.

### Acceptance criteria

- Only `cache_control` additions at selected candidate containers are accepted.
- Any other added/removed/changed path flips status to `failed_fallback` and preserves original payload.
- No boundary annotations are recorded on fallback.

## Review area 5: provider-kind resolution

### Risk

Synthetic cache support depends on selected provider kind. The helper resolving provider kind must be stable for both catalog-backed and config-backed providers.

### Tasks

1. Inspect `resolve_selected_provider_kind()`.
2. Verify it works when provider metadata is present in catalog.
3. Verify it works when catalog metadata is missing but provider config exists.
4. Verify unknown provider kind produces provider-unsupported, not mutation.
5. Add tests for provider kind resolution fallback order.

### Acceptance criteria

- Anthropic provider kind is resolved consistently after selection.
- Unknown provider kind prevents mutation.
- Provider-specific policy matchers do not fire with stale requested-provider metadata.

## Review area 6: Phase 10 recommendation-only lifecycle

### Risk

Docs now say tuning is recommendation-only. Code must not accidentally apply runtime overrides in production while `mode="apply"` is accepted.

### Tasks

1. Search for production calls to `build_runtime_override()` and `registry.register()`.
2. Confirm no background task populates `compression_tuning_registry`.
3. Confirm `compute_recommendation()` always returns `status="recommended"`, not `"applied"`.
4. Add a test for `mode="apply"` proving it still returns recommendation-only status and does not change resolved policy without a manually injected registry entry.
5. Confirm docs/config examples say apply mode is dormant/reserved.

### Acceptance criteria

- `mode="apply"` does not alter production request policy.
- Runtime registry is only populated by tests/manual future hooks.
- Dashboard/API labels do not imply apply mode is live.

## Review area 7: routing non-interference

### Tasks

Run and, if necessary, expand routing guardrail tests to cover:

- synthetic dry-run candidate count differences;
- synthetic applied count differences;
- synthetic fail-closed fallback;
- tuning recommendation differences;
- post-route provider-specific policy match differences.

### Acceptance criteria

- Same-provider account score/order is unchanged by synthetic/tuning metadata.
- Synthetic fail-closed fallback does not mark provider unhealthy.
- No second route selection happens after synthetic mutation.

## Review area 8: docs/config consistency

### Tasks

Review:

- `README.md`
- `architecture/README.md`
- `AGENTS.md`
- `.opencode/skills/architecture/SKILL.md`
- `config.example.toml`
- `src/eggpool/_share/config.example.toml`

Confirm they all say:

- synthetic cache controls are disabled by default and dry-run by default;
- synthetic cache controls run post-route on provider-bound Anthropic payloads;
- TTL support is `ephemeral` only;
- Phase 10 apply lifecycle is not live;
- routing does not consume compression/cache/synthetic/tuning fields.

## Verification commands

Run focused tests first:

```bash
uv run pytest tests/unit/test_cache_synthesis.py -q
uv run pytest tests/unit/test_compression_tuning.py -q
uv run pytest tests/unit/test_routing_guardrails.py -q
uv run pytest tests/integration/test_compression_policy_wiring.py -q
```

Then run request-path and streaming tests:

```bash
uv run pytest tests/unit/test_request_coordinator.py -q
uv run pytest tests/unit/test_proxy_request.py -q
uv run pytest tests/integration -q
```

Then full validation:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

## Acceptance criteria for the review pass

- Streaming parity is verified or fixed.
- OpenAI-client to Anthropic-provider synthetic cache flow is verified with provider-bound segmentation.
- Native cache-control preservation is verified across supported surfaces.
- Structural diff accepts only exact candidate cache-control additions.
- Provider-kind resolution is covered by tests.
- Phase 10 is recommendation-only in code, docs, and tests.
- Routing non-interference remains proven.
- Full validation passes.

## Rollback guidance

If a production issue is found and cannot be fixed immediately:

```toml
[cache.synthetic_cache_controls]
enabled = false

[compression.tuning]
enabled = false
mode = "recommend"
```

These settings should leave Phase 1-8 behavior intact while preserving additive audit schema.