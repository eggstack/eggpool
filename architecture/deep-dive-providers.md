# Deep Dive: Provider Architecture

Back to [Overview](overview.md)

## Purpose

Provider configuration separates provider identity, protocol/model contracts,
credentials, account health, and outbound transport. Connection pooling and
the operating-system resolver provide the normal network path; EggPool does
not add a process-local DNS cache.

## Key modules

- `providers/contract.py` — provider URL, protocol, model, authentication, and
  capability contracts.
- `wire/` — closed wire-surface names, packaged codec/profile definitions, and
  immutable resolved dispatch facts.
- `providers/client_pool.py` — provider/account client ownership and bounded
  HTTPX connection pools.
- `providers/outbound.py` — shared outbound client manager and diagnostics.
- `providers/pproxy_transport.py` — per-account proxy transport when configured.
- `providers/auth.py` — credential/header construction and redacted diagnostics.
- `providers/connect.py` — provider setup and connectivity probes.

## Provider client lifecycle

`ProviderClientPool` owns clients for eligible provider accounts. Normal clients
use HTTPX transports and connection reuse. Accounts with configured proxies use
the pproxy transport path. Both paths are generation-owned and close with the
retiring generation.

## Configuration and model IDs

Provider-suffixed model IDs use `model-id/provider-id`. `compose_provider_url()`
is the single URL construction authority. Static model rows are the source of
truth when a provider serves a non-default protocol or capability contract.

`WireSurfaceName` is independent of `ProtocolName`. The built-in registry in
`providers/_wire_profiles.toml` currently names OpenAI Chat Completions,
OpenAI Responses, Anthropic Messages, Gemini Interactions, and Gemini
generateContent. `ProviderConfig.wire_surfaces` maps a provider to one or more
candidate paths, optional streaming paths, priorities, auth shapes, and static
headers. When the field is absent, the legacy `protocols`, `openai_path`,
`responses_path`, and `anthropic_path` fields synthesize equivalent candidates.
The registry selects only Python-registered codec IDs; it never imports code
from TOML. `wire/codecs/defaults.py` owns the Responses and Gemini codecs,
while `compat.py` owns the Chat and Messages codecs. Bundled model hints are
preferences with source/verification metadata, not permanent routing truth.

Provider catalog `reasoning_options` are normalized into independent
toggle/effort/budget dimensions; omitted options remain unknown, while a
complete empty list means supported reasoning with no caller control. Verified
provider-scoped models.dev metadata uses the same parser as live catalog data.
Explicit live facts take precedence over models.dev facts at the individual
control dimension, and operator overrides remain highest authority. A missing
fact stays unknown; EggPool does not infer controls from model-family names.
Routing then matches the request's toggle, exact effort, or numeric budget
requirement against that provider/model row. Reasoning support alone does not
authorize an unadvertised caller control.

Use `resolve_provider_wire_profiles()` to obtain immutable structural profiles
and `build_wire_profile_headers()` to render the selected account credential.
Account secrets remain in the account/config machinery and are not stored in a
profile. Surface-specific auth replaces the former need to send multiple
credential headers on every request; existing `auth.additional` remains a
legacy compatibility field.

`WireProfileResolver` is process-owned and receives the generation's resolved
profiles after account/provider selection. It orders fixed/operator, learned,
verified catalog, bundled, and provider-priority candidates. A completed
ordinary request refreshes the learned preference; deterministic rejection
cooldowns and negotiation single-flight state are in memory only. The
coordinator admits an authorized alternate transition through one
provider/model flight. Its leader owns the provider-wide abnormal-dispatch
permit while submitting the next candidate; followers wait on the bounded wire
decision and then make their own inference request. Ordinary known-good
inference does not use this permit. The resolver does not inspect raw HTTP
failures, start probes, or create alternate retries; the canonical
failure-effects decision must authorize those transitions.

Failure effects distinguish explicit credential invalidity from an ambiguous
401. Only the former disables the selected account. Deterministic
auth-header, surface, schema, or weak endpoint-local model rejection can
suppress the selected wire candidate and authorize a same-account alternate
before response handoff. Weak model wording is eligible only when the
provider-scoped catalog/config entry knows the model; strong model absence and
authoritative withdrawal retain model-unavailable behavior. The bounded
`model ... is not available` wording is treated as ambiguous availability: it
can be endpoint-local under that same known-model/pre-handoff context, but
otherwise remains conservative model-absence evidence. A typed wire signal
also outranks a generic `Unsupported...` error class; the class name alone
does not invalidate or migrate a wire candidate. 429, quota, transport,
generic validation, and 5xx outcomes do not invalidate a wire candidate.

## Live provider verification

The opt-in live acceptance suite in
`tests/live/test_opencode_go_wire_live.py` calls the public EggPool API against
OpenCode Go using a temporary runtime/database. Its optional outbound hook is
attached at the real `client.send` boundary and emits only sanitized
structural observations; it is not a persisted event table or a second
routing system. Deterministic stale-profile migration, unhinted-model
classification, cross-surface coordinator paths, single-flight, rate-limit,
and failure-isolation behavior remains covered by fake-upstream tests. The
live suite includes a public Messages-to-Responses cross-surface check, a
MiniMax-M3 Chat binary reasoning-toggle request that verifies Anthropic
Messages field adaptation, and deterministically routes its invalid-key account first using
ordinary account weight configuration.
Live provider calls are excluded from smoke tests and CI and must never be
required for ordinary installation or release automation.

## Invariants

- API credentials never appear in diagnostics or logs.
- Provider/model capability data gates native protocol fields.
- Wire surface, model, provider, and protocol-family metadata are separate
  facts; a surface priority or bundled hint is only a starting preference.
- Learned wire preferences are hints, not health state. A deterministic
  candidate rejection temporarily suppresses only that surface; 429, transport,
  5xx, and midstream failures do not invalidate a surface.
- Negotiation dispatches are single-flight per provider/model and separately
  bounded per provider. Normal known-good provider inference is not serialized
  behind this abnormal-dispatch guard. A leader cancellation publishes a
  rejected decision and releases only an acquired permit; follower cancellation
  is isolated from the shared decision. A rate-limited leader wakes followers
  and ends discovery without suppressing the candidate.
- Wire path templates support only the validated `{model}` placeholder and
  quote the canonical model ID before URL composition.
- Gemini `generateContent` streaming uses its explicit
  `:streamGenerateContent?alt=sse` endpoint; the native Interactions stream
  uses typed `event:` names. Each adapter preserves native terminal evidence
  and never invents a terminal event at transport EOF.
- Upstream failures, not local quota estimates, suppress account routing.
- OS resolution and HTTP connection reuse are the default network behavior.
- Proxy routing remains per-account and independent of normal resolution.
- Bundled local-runtime multimodal capability declarations represent verified
  protocol-surface behavior (e.g. base64 image support on the OpenAI-compatible
  endpoint), not guarantees that every loaded model supports the modality.
  Provider-bound decisions use the *selected* provider's row — collapsed models
  served by multiple providers with different capability rows do not borrow
  another provider's claim.
- Provider-bound serialized-size decisions resolve against the selected
  provider's capability row. Speculative universal ceilings (such as a
  universal 5 MiB local-runtime ceiling) are not encoded in bundled templates;
  only verified provider-defined limits are advertised.
- Per-provider capability overrides (e.g. `thinking.status`) live in the
  provider-scoped cache row and are applied by
  `ModelCatalogCache.get_provider_model_entry()` /
  `ModelCatalogCache.get_provider_model_entries()`. The collapsed `models`
  row (`get_model()`) deliberately does **not** apply overrides — it
  represents the raw catalog state. Capability rejection attribution must
  consult provider-scoped entries, not just the collapsed row, to avoid
  false `"unknown"` readings when overrides are present. Quarantine does
  **not** erase a provider's capability — quarantined entries still
  contribute to the aggregated status.
