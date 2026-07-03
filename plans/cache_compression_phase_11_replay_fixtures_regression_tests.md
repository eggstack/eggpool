# Phase 11 Plan: Replay Fixtures and Regression Test Harness

Date: 2026-07-03

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Depends on:

- Phase 1 cache/token observability
- Phase 2 canonical request segmentation
- Phase 3 transcoder cache stability
- Phase 4 observe-mode compression accounting
- Phase 5 safe suffix compression
- Phase 6 policy controls
- Phase 7 dashboard/runtime views
- Phase 8 routing guardrails
- Phase 9 synthetic cache controls
- Phase 10 threshold tuning recommendation engine
- `plans/cache_compression_phase_10_11_review_pass.md`

## Summary

Phase 11 turns the cache/compression work into a durable regression suite. The project now has many moving parts: canonical segmentation, path resolution, transcoder cache preservation, safe suffix compression, policy resolution, synthetic cache controls, dashboard stats, routing guardrails, and recommendation-only tuning. Unit tests exist, but the next risk is drift across realistic request shapes.

Phase 11 adds a replay fixture harness with sanitized OpenAI and Anthropic request/response shapes. The harness should prove that the system preserves cacheable prefixes, compresses only eligible volatile suffixes, applies synthetic cache controls only where allowed, never routes differently based on cache/compression metadata, and keeps dashboard/stats summaries content-private.

The fixtures must be content-safe: no real user prompts, credentials, private tool output, provider request IDs, or proprietary system messages.

## Non-goals

- Do not persist raw production prompts into the repo.
- Do not replay against live providers by default.
- Do not add semantic or learned compression.
- Do not add routing optimization.
- Do not require network access for fixture tests.
- Do not snapshot entire dashboard HTML unless the project already prefers that style.

## Fixture categories

Create a structured fixture tree, for example:

```text
tests/fixtures/cache_compression/
  openai/
    simple_stable_prefix.json
    repeated_tool_output.json
    large_search_results.json
    base64_blob.json
    stack_trace.json
    mixed_native_cache_like_fields.json
  anthropic/
    system_blocks_native_cache.json
    tool_schema_native_cache.json
    tool_result_string_large.json
    tool_result_nested_text_large.json
    thinking_block_protected.json
    synthetic_cache_candidates.json
  transcode/
    openai_client_to_anthropic_provider.json
    anthropic_client_to_openai_provider.json
  routing/
    same_provider_two_accounts_equal_load.json
    adversarial_cache_metrics.json
  stats/
    request_rows_phase_1_to_10.json
```

Each fixture should contain only synthetic strings such as:

- `"SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS"`
- `"TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS"`
- `"VOLATILE_LOG_LINE repeated 500 times"`
- `"STACK_TRACE_SENTINEL"`

Use repeat counts or generated payload builders rather than committing huge blobs where possible.

## Fixture schema

Define a small schema so fixtures are self-describing.

Example:

```json
{
  "name": "openai_repeated_tool_output",
  "client_protocol": "openai",
  "target_protocol": "openai",
  "description": "Stable system/tool prefix plus repeated volatile tool output.",
  "request": {"model": "gpt-test", "messages": []},
  "expectations": {
    "segmentation_status": "segmented",
    "stable_prefix_contains": ["SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS"],
    "volatile_suffix_contains": ["VOLATILE_LOG_LINE"],
    "compression_safe_applies": true,
    "stable_prefix_content_hash_unchanged_after_compression": true,
    "synthetic_cache_status": "disabled"
  }
}
```

For transcode fixtures, include expected provider-bound protocol and minimal expected path facts rather than full transformed payload snapshots if exact transcoder formatting is still evolving.

## Harness design

Add a small fixture runner under `tests/helpers/cache_compression_replay.py` or similar.

Responsibilities:

- Load fixture JSON.
- Build request payloads, expanding compact repeat specifications if used.
- Run `segment_request()`.
- Run `stable_prefix_content_hash()`.
- Run `analyze_compression()`.
- Run `apply_safe_compression()`.
- Optionally run `BodyTranscoder` for client-to-provider protocol changes.
- Run post-route-style synthetic cache synthesis against provider-bound payloads.
- Compute structural diffs.
- Assert fixture expectations.

Keep the harness deterministic and content-private. Do not log request content on failure; log fixture name, path, expected status, observed status, and structural paths only.

## Replay dimensions

Each fixture should be replayable across modes:

1. Compression disabled.
2. Compression observe mode.
3. Compression safe mode.
4. Synthetic cache disabled.
5. Synthetic cache dry-run.
6. Synthetic cache apply mode.
7. Tuning disabled/recommendation-only.

The harness should not run every combination for every fixture by default if it becomes slow. Use marks such as:

```python
@pytest.mark.cache_compression_replay
@pytest.mark.parametrize("fixture_name", [...])
```

## Required fixture assertions

### Segmentation

- Every `compressible_candidate=True` segment resolves to a string leaf.
- Every protected stable-prefix segment is marked non-compressible by safe policy.
- OpenAI message/string/list paths resolve.
- Anthropic system/message/tool_result paths resolve.
- Stable-prefix shape hash is deterministic.
- Exact stable-prefix content hash changes only when stable content changes.

### Safe compression

- Disabled mode does not mutate.
- Observe mode does not mutate.
- Safe mode mutates only volatile suffix string leaves.
- Stable-prefix exact content hash is unchanged after safe compression.
- Fail-closed fallback triggers on intentional stable-prefix mutation fixtures.
- Transform markers round-trip and contain no raw hidden content.

### Synthetic cache controls

- Disabled mode does not run or records disabled according to current design.
- Dry-run mode records candidates but does not mutate provider-bound payload.
- Apply mode adds only `cache_control` keys at candidate containers.
- Native cache controls are preserved and not duplicated.
- Non-Anthropic provider protocols are provider-unsupported.
- OpenAI-client to Anthropic-provider fixture uses provider-bound Anthropic segmentation.
- Structural diff accepts only exact candidate `cache_control` additions.

### Transcoding cache stability

- Native Anthropic cache controls survive Anthropic-to-Anthropic pass-through.
- Unsupported target protocols drop or reject according to configured policy.
- Transcoded stable-prefix content is deterministic across repeated runs.
- Loss warnings are stable and content-private.

### Routing guardrails

- Same-provider account selection is unchanged when replay rows have different cache hit ratios.
- Same-provider account selection is unchanged when compression savings differ.
- Synthetic cache status/candidate count does not enter scorer input.
- Tuning recommendations do not enter scorer input.

### Stats/dashboard content privacy

- Stats summaries contain counts, hashes, statuses, warnings, and paths only.
- No raw sentinel content appears in JSON stats responses except fixture-local assertions that explicitly inspect payloads inside tests.
- Recent-request debugging endpoints do not expose bodies.

## Integration tests

Add at least these tests:

### `test_replay_openai_safe_suffix_compression_preserves_prefix`

- Fixture: OpenAI stable system + tool schema + repeated volatile tool output.
- Assert safe compression applies.
- Assert stable-prefix exact hash unchanged.
- Assert only volatile tool output path changed.

### `test_replay_anthropic_tool_result_nested_text_compresses`

- Fixture: Anthropic nested `tool_result.content[].text` repeated content.
- Assert production segmentation emits concrete path.
- Assert safe compression applies.

### `test_replay_openai_to_anthropic_synthetic_cache_dry_run`

- Fixture: OpenAI client payload that transcodes to Anthropic provider payload.
- Assert synthetic dry-run candidates appear using Anthropic provider-bound segmentation.

### `test_replay_anthropic_native_cache_preserved_apply_mode`

- Fixture: Anthropic system and tools with native `cache_control`.
- Assert apply mode does not duplicate native annotations.

### `test_replay_synthetic_cache_diff_fails_closed`

- Fixture/hook intentionally attempts illegal mutation.
- Assert status `failed_fallback`, original payload preserved, no annotations recorded.

### `test_replay_routing_guardrails_adversarial_metrics`

- Fixture with fake request history rows containing skewed cache/compression/synthetic/tuning metrics.
- Assert scorer/account order unchanged relative to baseline load.

## Sanitization rules

Add a fixture linter or test that rejects unsafe strings:

- `sk-` style API keys.
- `Bearer ` tokens.
- provider request IDs if matching known formats.
- real-looking email addresses unless intentionally synthetic `example.com`.
- long natural-language paragraphs that look copied from real prompts.
- raw stack traces from real projects unless synthetic path prefixes are used.

This does not need to be perfect; it should catch accidental secret/prompt leakage.

## Golden snapshots

Use golden snapshots sparingly.

Prefer asserting structural facts over full JSON equality because provider payload formatting may evolve. Good snapshots:

- segmentation summary JSON with hashes redacted/normalized;
- compression summary JSON;
- synthetic cache summary JSON;
- tuning recommendation JSON.

Bad snapshots:

- full request bodies;
- full transformed provider payloads with large generated content;
- full dashboard HTML.

## Performance budget

The replay suite should stay cheap enough for normal local test runs.

Targets:

- Default replay subset under 5 seconds on a typical dev machine.
- Full replay matrix behind a pytest mark, e.g. `-m cache_compression_replay_full`.
- No network access.
- No large fixture files above a few hundred KB without a strong reason.

## Documentation updates

Add a short test README:

```text
tests/fixtures/cache_compression/README.md
```

Document:

- fixture schema;
- how to add a fixture;
- sanitization rules;
- how to run default and full replay suites;
- why fixtures use sentinel strings instead of real prompts.

## Acceptance criteria

- Fixture tree exists with OpenAI, Anthropic, transcode, routing, and stats fixtures.
- Replay harness can run segmentation, compression, synthetic cache synthesis, and key stats checks offline.
- Default replay tests cover the high-risk Phase 5/9/10 flows.
- Sanitization test prevents obvious prompt/secret leakage.
- Structural assertions prove stable prefix preservation and volatile-only mutation.
- Synthetic cache apply mode is tested against native-control preservation and fail-closed diff behavior.
- Routing guardrails are tested against adversarial cache/compression/synthetic/tuning metrics.
- Full tests, ruff, and pyright pass.

## Rollback notes

Phase 11 should be tests and fixtures only. If a fixture causes maintenance churn, mark it as full-replay only rather than deleting coverage. If a test exposes a real bug, fix the bug rather than weakening the replay expectation unless the expectation conflicts with documented behavior.