# API Reference

EggPool exposes OpenAI Chat Completions- and Anthropic Messages-compatible paths, plus internal diagnostic endpoints.

## Chat Completions & Anthropic Messages

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions-compatible requests |
| `POST` | `/v1/responses` | Stateless OpenAI Responses-compatible requests; canonical adaptation is allowed to eligible upstream surfaces |
| `POST` | `/v1/responses/compact` | Bounded remote-compaction operation returning replacement history/checkpoint material; native compact-capable upstreams only, otherwise rejected before submission |
| `POST` | `/v1/messages` | Anthropic Messages-compatible requests |

The compact endpoint is a distinct operation, not an ordinary Responses
alias. It shares the stateless Responses contract (`store` may be omitted or
false; `store: true`, continuation references, and background execution are
rejected) and is always finite (`stream: true` is rejected). Only upstreams
whose provider surface opts in with `supports_remote_compaction_v1 = true`
plus a `compact_path_template` are eligible; without a qualified native
target the request fails before upstream submission. There is no translated
compaction fallback. Current Codex custom providers default to local
compaction, so remote compaction is opt-in forward compatibility rather than
a requirement for normal Codex operation. See
[Stateless Responses](stateless-responses.md).

`/v1/models` remains the standard OpenAI-compatible model-list contract. It is
not a Codex-private remote catalog and does not claim Codex reasoning,
context, shell, or tool metadata.

`GET /api/integrations/v1/profile` is the separately versioned EggPool-specific
surface for remote setup. It returns the same conservative provider-neutral
projection as local `configsetup` with a normalized advertised `base_url`,
deterministic public-ID ordering, and a canonical-content `revision`
fingerprint (not timestamps). It is authenticated even when the dashboard is
public, sends `Cache-Control: private, max-age=0, must-revalidate` plus
`ETag: "<revision>"` (`If-None-Match` → `304`), performs no catalog refresh or
upstream request, and uses bounded generic errors (`401`/`403` auth, `503`
unavailable with no internal body).

Configured virtual model routers are included in `/v1/models` as compact,
capability-free entries with `owned_by = "eggpool"` and
`eggpool.virtual = true`. They do not expose selector prompts, route
descriptions, affinity state, prices, or concrete target capabilities. A
virtual request is resolved to a concrete model before normal context,
capability, transcoding, provider, account, and retry handling; only the
resolved concrete model is sent upstream.

`X-EggPool-Route-Session: <opaque-stable-id>` is an optional EggPool-local
header. On sticky virtual routers it provides the strongest cross-request
affinity signal, especially for stateless Responses calls. It is hashed for
the bounded process-local cache and is never persisted, logged, used as a
metric label, or forwarded upstream. See [Model routing](model-routing.md).

## Health & Readiness

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/healthz` | Liveness check |
| `GET` | `/v1/readyz` | Readiness check |
| `GET` | `/api/status` | Authenticated compact proxy/provider health snapshot (`schema_version: 1`; no outbound provider probes) |
| `GET` | `/api/integrations/v1/profile` | Authenticated versioned sanitized integration profile (`schema_version: 1`, deterministic `revision`/ETag, bounded, no credentials) |

## Stats & Diagnostics

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/stats/summary` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Aggregate request stats (counts, tokens, cost, latency) |
| `GET` | `/api/timeseries` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Bounded request/usage buckets for the dashboard chart |
| `GET` | `/api/timeseries/grouped` | Dashboard-gated (`period`, `group_by=model\|account`, `metric=requests\|tokens\|cost`, bounded `limit`) | Grouped chart series and bucket totals |
| `GET` | `/api/stats/runtime` | Always key | Runtime metrics, routing guardrails, background task summaries, stream diagnostics, and `finalization_supervisor` snapshot |
| `GET` | `/api/stats/update` | Always key | Release update-check status |

There are no other JSON backoff/model-info/network/event endpoints:
account backoffs, model-info detail, per-model/account roll-ups, latency and
bandwidth breakdowns, and event views are rendered server-side into the
dashboard HTML pages below, not served as JSON. `eggpool accounts status`,
`eggpool status`, and `eggpool runtime-status` expose the same facts on the
CLI without HTTP.

## Dashboard

When `[dashboard].enabled = true`, a multi-page dashboard is served at `/`
(overview, accounts, models, latency, events, timeseries, bandwidth, pings,
reliability, routing, traces, runtime, cache). Pages render server-side HTML;
the chart APIs are `/api/stats/summary`, `/api/timeseries`, and
`/api/timeseries/grouped`. The dashboard is public
and read-only by default (`[dashboard].public = true`): browsers render pages
without an API key. Set `public = false` (or `eggpool dashboard public --off`)
to require the key there too.

## Request Body Limits

Request ingestion is bounded by `[server].max_request_body_bytes` (default 10 MiB). Provider document and image limits remain additional constraints; they never raise the whole-request ceiling. Oversized bodies are rejected before JSON parsing or transcoding. The field is live-reloadable with `eggpool rehash`.

## Authentication

Inference endpoints (`/v1/*`), the integration profile
(`/api/integrations/*`), and runtime/update/status endpoints
(`/api/stats/runtime`, `/api/stats/update`, `/api/status`) always require the
server API key whenever one is configured. Ordinary dashboard pages and their
non-sensitive JSON data are public while `[dashboard].public = true` (the
default) and require the key when it is `false`. The key is sent as
`Authorization: Bearer <key>` for OpenAI-compatible endpoints or
`x-api-key: <key>` for Anthropic-compatible endpoints. Health, readiness, and
static assets are unauthenticated, and a loopback-only install without a
configured key remains open for local development.
