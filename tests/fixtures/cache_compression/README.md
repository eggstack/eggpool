# Cache/Compression Replay Fixtures (Phase 11)

Phase 11 turns the cache/compression pipeline (Phases 1–10) into a durable
regression suite. This directory holds **content-private** request fixtures
and a small JSON schema so any developer can add a regression case without
copy-pasting payload gymnastics across test files.

## Layout

```
tests/fixtures/cache_compression/
├── README.md                       <- this file
├── openai/                         <- OpenAI-shaped request fixtures
├── anthropic/                      <- Anthropic-shaped request fixtures
├── transcode/                      <- cross-protocol transcoding fixtures
├── routing/                        <- routing guardrails fixtures
└── stats/                          <- stats-query fixtures (counts only)
```

Each directory holds one or more JSON files. Every file is a self-describing
fixture loaded by `tests/helpers/cache_compression_replay.py`.

## Fixture schema

A fixture JSON file looks like this:

```json
{
  "name": "openai_repeated_tool_output",
  "category": "openai",
  "client_protocol": "openai",
  "target_protocol": "openai",
  "description": "Stable system/tool prefix plus repeated volatile tool output.",
  "request": {
    "model": "gpt-test",
    "messages": []
  },
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

Top-level fields:

| Field | Required | Notes |
| --- | --- | --- |
| `name` | yes | Stable fixture id, used in test ids and failure logs |
| `category` | yes | `openai` / `anthropic` / `transcode` / `routing` / `stats` |
| `client_protocol` | yes | `openai` or `anthropic` |
| `target_protocol` | yes | `openai` or `anthropic` (transcoded when different) |
| `description` | no | One-line summary |
| `request` | yes | The raw provider-shaped payload (must be a JSON object) |
| `repeats` | no | Optional compact repeat spec, expanded by the harness |
| `expectations` | no | The expected outcome map (any field may be omitted) |

### Compact repeat specifications

Long repeated content bloats the repo. Use `repeats` to expand at load time:

```json
{
  "name": "openai_repeated_log_lines",
  "request": {"model": "gpt-test"},
  "repeats": {
    "messages": {
      "fields": {
        "role": "tool",
        "content": "ERR: connection timeout\n"
      },
      "repeat": 200,
      "append_after_role": "tool"
    }
  }
}
```

The harness deep-merges these into `request` before any segmentation or
compression runs. Generated content is byte-deterministic.

### `expectations` field reference

| Key | Asserts |
| --- | --- |
| `segmentation_status` | `SegmentationStatus.SEGMENTED`, `EMPTY_REQUEST`, or `PARSE_FAILURE` |
| `stable_prefix_contains` | Each substring must appear in some `STABLE_PREFIX` segment leaf |
| `volatile_suffix_contains` | Each substring must appear in some `VOLATILE_SUFFIX` segment leaf |
| `compression_safe_applies` | `apply_safe_compression(...).applied == True` |
| `compression_safe_does_not_apply` | `apply_safe_compression(...).applied == False` |
| `stable_prefix_content_hash_unchanged_after_compression` | `pre_stable_prefix_hash == post_stable_prefix_hash` |
| `stable_prefix_content_hash_known` | `result.stable_prefix_content_hash` is a 64-hex SHA-256 |
| `synthetic_cache_status` | One of `disabled`, `dry_run`, `applied`, `no_candidates`, `policy_required`, `provider_unsupported`, `failed_fallback` |
| `synthetic_cache_candidate_count` | Expected `plan.candidate_count` (int) |
| `cache_stability_status_counts` | Dict of `CacheBoundaryKind -> int` |
| `no_synthetic_cache_at_paths` | List of (path string) that MUST NOT be mutated by synthesis |
| `transcoder_preserves_native_cache_control` | `result.transformed_body` carries the same native `cache_control` keys |
| `transcoder_drops_unsupported_target` | `result.warnings` contains the relevant loss-warning kind |
| `failed_fallback` | True iff the applier fell back to the original payload |
| `dry_run_no_mutation` | Synthetic cache applies zero `cache_control` keys |
| `expected_transforms_present` | Every named transform appears in `transforms_by_reason` |

The test runner ignores any key it does not understand so fixtures can carry
forward-looking markers.

## Sanitization rules

Fixtures must NEVER contain:

- `sk-` style API keys or any bearer tokens (`Bearer ...`)
- Provider request IDs (`req_...`, `msg_...`, etc.)
- Real-looking email addresses (use `noreply@example.com`)
- Long natural-language paragraphs that look copied from real prompts
- Raw stack traces from real projects (use synthetic path prefixes)
- Any binary or non-UTF8 content

A regression test (`tests/unit/test_replay_fixtures_sanitization.py`) walks
every fixture file and fails loudly if any forbidden token shows up.  When
adding a fixture you must use the established sentinel markers:

| Sentinel | Use |
| --- | --- |
| `SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS` | Stable-prefix system content |
| `TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS` | Stable-prefix tool schema |
| `VOLATILE_LOG_LINE` | Volatile suffix log line |
| `STACK_TRACE_SENTINEL` | Synthetic stack trace |
| `SYNTHETIC_BASE64_BLOB` | Synthetic base64 placeholder |
| `LONG_USER_INSTRUCTION` | Stable/semi-stable user content |
| `LATEST_USER_SENTINEL` | Latest user turn (volatile classification) |

The sentinels are content-private — they cannot be confused with
real prompts and they make assertions unambiguous.

## Running the replay suite

The fixture tree is retained for focused privacy checks. The former full replay
matrix and its replay harness are no longer part of the retained test corpus:

```bash
# Fixture privacy checks
uv run pytest tests/unit/test_replay_fixtures_sanitization.py -v
```

## Why fixtures use sentinels instead of real prompts

Real prompts can leak secrets, PII, or proprietary system messages into the
repository. They also drift over time — a 200-line realistic fixture from
2024 will not match real 2026 traffic and may hide regressions. Sentinels
keep tests deterministic, content-private, and fast to review.
