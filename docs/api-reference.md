# API Reference

EggPool exposes OpenAI Chat Completions- and Anthropic Messages-compatible paths, plus internal diagnostic endpoints.

## Chat Completions & Anthropic Messages

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List models visible to the authenticated principal; managed users see only models allowed by their effective policy |
| `GET` | `/api/user-budget` | Authenticated user rules and bounded usage; admins may add `?user_id=<id>` to inspect one user |
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
| `GET` | `/api/stats/transcoding` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Native/transcoded request counts and bounded loss-warning summary |
| `GET` | `/api/stats/cache-observability` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Cache-counter coverage and known-only token ratios |
| `GET` | `/api/stats/canonical-request-segmentation` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Segmentation status and scalar token/byte totals |
| `GET` | `/api/stats/cache-stability` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Persisted transcoded request count; in-memory boundary detail is unavailable |
| `GET` | `/api/stats/request-shaping` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Cache coverage, segmentation counts, and reporting-only routing guardrails |
| `GET` | `/api/timeseries` | Dashboard-gated (`?period=1h\|24h\|7d\|30d`) | Bounded request/usage buckets for the dashboard chart |
| `GET` | `/api/timeseries/grouped` | Dashboard-gated (`period`, `group_by=provider_model\|provider\|model\|account`, bounded `limit`; `metric` retained for compatibility) | Grouped request-count chart series and bucket totals |
| `GET` | `/api/stats/runtime` | Always key | Runtime metrics, routing guardrails, background task summaries, stream diagnostics, and `finalization_supervisor` snapshot |
| `GET` | `/api/stats/update` | Always key | Release update-check status |

Other backoff/model-info/network/event projections remain HTML-only:
account backoffs, model-info detail, per-model/account roll-ups, latency and
bandwidth breakdowns, and event views are rendered server-side into the
dashboard HTML pages below, not served as JSON. `eggpool accounts status`,
`eggpool status`, and `eggpool runtime-status` expose the same facts on the
CLI without HTTP.

## Dashboard

When `[dashboard].enabled = true`, a multi-page dashboard is served at `/`
(overview, accounts, models, latency, events, timeseries, bandwidth, pings,
reliability, routing, traces, runtime, cache). Pages render server-side HTML;
chart APIs are `/api/stats/summary`, `/api/timeseries`, and
`/api/timeseries/grouped`. Runtime/cache statistics are also available from
the five dashboard-gated `/api/stats/{transcoding,cache-observability,canonical-request-segmentation,cache-stability,request-shaping}` routes. Ratios are null when their denominator is not measured; provider cache-hit rate remains unavailable because eligible-input denominators are not persisted. The dashboard is public
and read-only by default (`[dashboard].public = true`): browsers render pages
without an API key. Set `public = false` (or `eggpool dashboard public --off`)
to require the key there too.

## Request Body Limits

Request ingestion is bounded by `[server].max_request_body_bytes` (default 10 MiB). Provider document and image limits remain additional constraints; they never raise the whole-request ceiling. Oversized bodies are rejected before JSON parsing or transcoding. The field is live-reloadable with `eggpool rehash`.

## Authentication

In `legacy_unmigrated`, the shared server key keeps its legacy behavior. After
local identity initialization, inference uses per-user agent tokens and
browser sessions; the legacy key is retired. A personal token grants only its
inference/discovery scopes and does not grant dashboard or account access.
Browser sessions authenticate the web surface, with CSRF proof required for
state-changing requests. In managed mode, `[dashboard].public` does not make
dashboard or `/api/*` data public. Health, readiness, login, and static assets
are unauthenticated. See [deployment](deployment.md) and [firewall](firewall.md)
for operator-managed HTTPS termination, trusted proxy, and loopback guidance.
Managed users receive policy-filtered model discovery, and runtime dispatch
enforces the same rules even when a client submits a hidden model manually.
The integration profile's ETag includes the policy revision so client caches
refresh after an administrator changes model access.

`GET /api/user-budget` is available to personal tokens and browser sessions.
Members can read only their own rules and rollups; an active admin may select a
target with `user_id`. The response identifies UTC calendar windows and reports
integer microdollars as modeled/estimated usage, with separate
`provider_reported_microdollars`, `local_estimated_microdollars`, and
`reservation_fallback_microdollars` totals. Unknown usage consumes the
reserved estimate and increments an explicit unknown counter. Each rule carries
an `enforcement` mode: `hard` rules enforce a provable worst-case bound (token
rules require an explicit caller output cap; USD rules require an
operator-configured provider tariff) and deny otherwise, while `estimated`
rules use approximate holds and are never a guaranteed cap. This API does
not represent invoices or exact charges for flat-rate provider accounts.
Requests denied because a hard rule cannot be bounded return HTTP 429 with a
stable machine-readable message
(`User budget hard limit is not enforceable for this request`) before any
upstream submission.

With `[dashboard].enabled = true`, the dashboard also serves browser-session
pages for `/users`, `/profiles`, `/usage`, `/admin/audit`, `/my-account`, and
`/my-account/models`. Mutations use same-origin, CSRF-protected form posts
under `/admin/users/*`, `/admin/profiles/*`, and `/account/tokens*`.
Administrator pages and writes require an active admin browser session;
members can read only their own usage, model access, and credentials. Agent
tokens never authenticate these pages. A newly issued token is returned once
in a `no-store` response and omitted from the account page thereafter.
Profile edits use a version check so a stale form cannot overwrite a newer
edit. The audit view contains actor, action, target type, target ID, and time;
it does not record passwords, token values, prompts, or request bodies.
