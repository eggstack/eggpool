# Phase 5 — Operator controls and docs

## Goal

Make the transcoder **discoverable, configurable, and observable** for
operators. After this phase:

- The default `[transcoder]` config block is documented and uncommented
  in `config.example.toml`.
- `eggpool status` (or equivalent) reports the active transcoder policy
  and counts transcoded requests per day.
- The dashboard gains a small "Transcoding" panel under the runtime page
  showing real-time counters and loss-warning summaries.
- README and `docs/` explain when and why to enable transcoding.
- CHANGELOG notes the feature as opt-in (default off).
- A structured log line is emitted for every transcoded request.

## Scope

In scope:

- `config.example.toml` documentation block with examples for both
  common setups.
- `eggpool stats` adds a `--transcoding` view (or the existing `stats`
  JSON includes transcoding counters when `[api].stats_detail` is
  `transcoding`).
- Dashboard `/runtime` page adds a "Transcoding" card.
- Structured logging: one INFO line per transcoded request with
  `request_id`, `client_protocol`, `upstream_protocol`,
  `loss_warnings`, `native_match`, and `account_name`.
- Daily aggregation table in the database (optional migration) so the
  dashboard query is fast.
- README updates under a new section "Protocol transcoding".
- `docs/transcoding.md` long-form operator guide.
- `CHANGELOG.md` entry.
- Boot-time log line: when `[transcoder] enabled = true`, emit an INFO
  line announcing "Protocol transcoding ENABLED" so operators see it in
  their service startup.

Out of scope:

- New provider templates that exercise the transcoder. Operators can
  already opt-in via the existing templates.
- Tooling around `eggpool connect` for transcoded providers. Templates
  stay native; the operator flips the global flag.

## Files to modify

```
config.example.toml                              # documented [transcoder] block
src/eggpool/cli_full.py                          # new `eggpool stats transcoding` subcommand
src/eggpool/app.py                               # register dashboard routes for transcoding
src/eggpool/dashboard/render.py                  # _render_transcoding_card
src/eggpool/dashboard/routes.py                  # GET /api/stats/transcoding
src/eggpool/stats/                               # StatsService.get_transcoding_stats
src/eggpool/request/coordinator.py               # structured log on every transcoded request
src/eggpool/app.py                               # boot-time INFO line
README.md                                        # new "Protocol transcoding" section
docs/transcoding.md                              # new long-form guide
CHANGELOG.md                                     # new entry
```

## Files to create

```
src/eggpool/db/migrations/
└── 00XX_transcoding_daily.sql                   # optional daily aggregation

tests/unit/test_cli_stats_transcoding.py
tests/integration/test_transcoding_dashboard.py
```

## Detailed design

### 1. `config.example.toml`

Add a top-level commented section near the end of the providers block:

```toml
# -----------------------------------------------------------------------------
# Protocol transcoding
# -----------------------------------------------------------------------------
# When enabled, requests from clients using one protocol (OpenAI or
# Anthropic) can be forwarded to upstream accounts whose provider
# declares only the other protocol. EggPool translates the request body,
# the streaming SSE events, and the response body bidirectionally.
#
# Use this when you want to mix protocol-native and protocol-transcoded
# accounts (e.g. opencode-go native plus MiniMax International
# Anthropic-only, both reachable from OpenCode clients).
#
# Default: enabled = false. The behaviour before this flag existed is
# preserved exactly.
[transcoder]
enabled = false
loss_policy = "warn"
prefer_native = true
```

### 2. Daily aggregation table

Only needed if the dashboard query gets expensive. Migration:

```sql
-- 00XX_transcoding_daily.sql
CREATE TABLE IF NOT EXISTS transcoding_daily (
    day TEXT NOT NULL,                          -- YYYY-MM-DD UTC
    client_protocol TEXT NOT NULL,
    upstream_protocol TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    loss_warning_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, client_protocol, upstream_protocol)
) WITHOUT ROWID;
```

The migration is added defensively only if `eggpool stats transcoding`
takes more than 100ms on a synthetic test. Otherwise the dashboard query
runs against the existing `requests` and `request_attempts` tables.

Skip the migration if benchmarks show the live aggregate is fast enough
on a 7-day window. The plan defaults to "include the migration" because
real deployments have millions of request rows.

### 3. Stats aggregation

`StatsService.get_transcoding_stats(period)` returns:

```python
@dataclass(slots=True)
class TranscodingStats:
    period: str                    # "1d" | "7d" | "30d"
    request_count: int             # total requests that were transcoded
    native_count: int              # requests where client==upstream
    per_direction: dict[tuple[str, str], int]   # ("openai","anthropic"): 42, ...
    top_loss_warnings: list[tuple[str, int]]    # ("dropped_field:presence_penalty", 17), ...
```

Per-request counters live on `request_attempts` rows already
(migration 0026–0029 in the architecture doc). The aggregation reads
from there.

### 4. Dashboard card

`_render_transcoding_card()` in `src/eggpool/dashboard/render.py` produces
HTML matching the existing card style. Inserted on the `/runtime` page
beneath the existing dispatch-overhead card. The card shows:

- Total transcoded requests (24h, 7d, 30d tabs)
- Top direction (`openai → anthropic` count, `anthropic → openai` count)
- Top 3 loss warnings (with `data-tooltip` describing each kind)

Reuses the existing CSS tooltip system and chart lifecycle helpers
(per the architecture doc's dashboard invariant).

### 5. Boot-time INFO log

`create_app` emits one INFO line:

```python
if config.transcoder.enabled:
    logger.info(
        "Protocol transcoding ENABLED — clients may reach upstream "
        "accounts whose provider.protocols does not match the client "
        "protocol. loss_policy=%s prefer_native=%s",
        config.transcoder.loss_policy,
        config.transcoder.prefer_native,
    )
else:
    logger.debug(
        "Protocol transcoding disabled (default). Set [transcoder] "
        "enabled = true in config.toml to allow cross-protocol routing."
    )
```

The disabled case is debug-level so production logs stay clean; the
enabled case is INFO so operators see it during boot.

### 6. Per-request log

`RequestCoordinator.execute()` emits a structured INFO log per
transcoded request after finalization:

```python
if context.transcode_required:
    logger.info(
        "transcoded_request request_id=%s client=%s upstream=%s "
        "account=%s provider=%s native_match=%s "
        "loss_warnings=%d bytes_in=%d bytes_out=%d",
        context.request_id,
        context.protocol,
        context.upstream_protocol,
        selected.account_name,
        selected.provider_id,
        context.protocol == context.upstream_protocol,
        len(transcode_context.loss_warnings),
        len(context.original_body),
        # bytes_out requires plumbing the response size through, set to 0
        # in v1 since the coordinator doesn't track it consistently.
        0,
    )
```

This is a single line; the goal is grep-ability and dashboard rollup,
not human readability per-request.

### 7. CLI subcommand

`eggpool stats transcoding [--period 1d|7d|30d]` prints a table:

```
Period: 7d
Total requests: 12,418
Native (no transcoding): 11,902
Transcoded: 516

Direction         Count
openai→anthropic   412
anthropic→openai   104

Top loss warnings (this period):
  dropped_field:presence_penalty         231
  dropped_field:logit_bias               198
  dropped_field:top_logprobs              47
```

`--json` outputs the same data as JSON for piping into other tools.

### 8. `README.md` addition

A new section under "Configuration":

```markdown
## Protocol transcoding

When `[transcoder] enabled = true`, EggPool bridges OpenAI Chat
Completions and Anthropic Messages bidirectionally so a single client
ecosystem (e.g. OpenCode, which speaks only OpenAI) can reach
Anthropic-only upstreams (e.g. MiniMax International at
`api.minimax.io/anthropic`) and vice versa.

What gets translated:

- Request bodies (text-only in v1)
- Streaming SSE events
- Non-retryable error envelopes
- Usage and cost fields (preserved exactly as the upstream reported them)

What is dropped with a structured warning log:

- OpenAI fields with no Anthropic equivalent (`logit_bias`,
  `presence_penalty`, `top_logprobs`, etc.)
- Anthropic fields with no OpenAI equivalent (`top_k`, `cache_control`)

What is **not** translated in v1 (lands in phase 6):

- Tool calls / function calling
- Vision / image content
- Extended thinking / reasoning
- Structured outputs (`response_format` / `json_schema`)

See `docs/transcoding.md` for the full translation table and known
limitations.
```

### 9. `docs/transcoding.md` long-form guide

Cover:

- Conceptual overview and why it exists (link to roadmap).
- Translation tables (request, response, errors, usage) for both
  directions.
- Provider-by-provider notes (which providers currently need
  transcoding, which don't).
- Operator checklist for enabling transcoding safely.
- Loss-warning reference: every `kind` value, what it means, what is
  dropped or clamped, and how to inspect logs.
- Performance: a transcoded request adds two JSON serializations and
  one extra dict copy; on the order of 50µs per request. Streaming adds
  the cost of one streaming transcoder state machine per request.
- Known limitations: headers preserved verbatim from upstream; some
  Anthropic-specific response headers may leak.
- Troubleshooting recipe: how to confirm a request was transcoded,
  how to read the structured log, how to disable transcoding for a
  single account (currently all-or-nothing; per-account opt-out is a
  future enhancement).

### 10. `CHANGELOG.md` entry

```markdown
## [Unreleased]

### Added

- **Bidirectional OpenAI ↔ Anthropic protocol transcoding.** When
  `[transcoder] enabled = true`, requests from clients using one
  protocol can be forwarded to upstream accounts that speak only the
  other. Initial scope is text-only requests and responses, plus
  streaming SSE. Tool calls, vision, and extended thinking land in a
  follow-up release. See `docs/transcoding.md` for the full translation
  table.
- New `eggpool stats transcoding [--period 1d|7d|30d]` subcommand.
- New "Transcoding" card on the `/runtime` dashboard page.
- Structured INFO log per transcoded request.

### Changed

- `RequestCoordinator` now carries `upstream_protocol` alongside
  `protocol` on `ProxyRequestContext`. Behaviour is identical when
  `[transcoder] enabled = false`.
```

## Validation

After implementation:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/unit/test_cli_stats_transcoding.py -v
uv run pytest tests/integration/test_transcoding_dashboard.py -v
uv run pytest tests/                                            # full suite
```

Acceptance criteria:

- `[transcoder]` block parses in every existing config example without
  changes (defaults match the documented values).
- Boot-time INFO line fires exactly once when enabled.
- `eggpool stats transcoding` prints a sensible table against an empty
  database ("0 transcoded requests, 0 native requests") and against a
  fixture database with mixed-protocol history.
- Dashboard card renders without breaking existing cards. Auto-refresh
  swaps the card's contents correctly.
- README renders cleanly (no broken links, no Markdown lint errors).
- `CHANGELOG.md` is updated with the entry above.

## Definition of done (phase 5)

- All files in "Files to modify" and "Files to create" are merged with
  passing tests.
- `eggpool stats transcoding` works against a real database.
- Dashboard `/runtime` page includes the new card and auto-refresh works.
- `docs/transcoding.md` exists and links cleanly from the README.
- `CHANGELOG.md` carries the new entry.
- The feature is **opt-in**. Default `enabled = false` preserves
  today's behaviour byte-for-byte for every existing user.
- Roadmap updated; phase 5 marked complete.