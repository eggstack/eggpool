# D003 — Catalog Refresh, Normalization, and Persistence

Status: closed; see [closure record](../../closure/routing-domain/003-status.md)

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d003--catalog-refresh-normalization-and-persistence`

Primary class: capability/invariant

## 1. Objective

Port the routing-essential catalog refresh pipeline onto the closed M4 transport and D002 catalog state. Rust must be able to refresh static/live model support for each configured account, isolate per-account failures, normalize routing-relevant metadata, persist semantic changes into the existing schema, and reproduce Python's authoritative-vs-uncertain withdrawal behavior.

D003 does not add inference dispatch and does not create a second generic HTTP stack for optional external catalogs.

## 2. Models-endpoint request contract

Implement the provider model-list request contract using `ProviderClientPool::get_client(provider_id, account_name)` and neutral `ProviderHttpClient::send`.

Preserve:

- provider-specific `models_endpoint` override;
- `DISABLED` endpoint semantics;
- GET and POST methods;
- provider-relative path and validated query parameters;
- configured finite JSON body for POST;
- `Accept: application/json`;
- provider auth/static headers required by the catalog endpoint;
- account-specific proxied transport selected by M4;
- finite response size/JSON parsing bounds;
- latency/status/error/model-count observation used by pings/diagnostics.

Create a small reusable provider contract/header builder where appropriate so M7 can later reuse the same validated auth/header semantics. D003 may exercise it only for model discovery; do not build inference request bodies or retry policy.

No ambient proxy lookup, Reqwest, or second TLS stack.

## 3. Response validation and normalization

Port deterministic model-list validation:

- top-level JSON must be an object;
- `data` must be a list;
- invalid/non-object rows are skipped according to Python's iterator rules;
- a non-empty list with zero valid model rows is an invalid catalog response rather than an authoritative empty catalog;
- invalid JSON/shape becomes a failed/non-authoritative outcome;
- provider errors remain local to the account and preserve prior support.

Port routing-relevant normalizers/resolvers for:

- model ID/display name;
- provider protocol and protocol source;
- capability extraction and source precedence;
- effective context/input/output limits;
- static config overrides;
- source metadata fields required for model-info/pricing identity and later diagnostics.

Do not mechanically port optional descriptive metadata that has no current contract use. Keep the typed Rust surface small and preserve unknown advisory metadata only where database/public parity requires it.

## 4. Static models

Seed configured static models before live fetch exactly as Python does. A provider whose live models endpoint is disabled must still expose static support.

Static protocol and explicit capability/limit values have the same precedence against weaker live metadata as D002's cache contract. Live authoritative rows may augment metadata but cannot erase explicit static facts without the Python policy allowing it.

## 5. Refresh outcomes

Represent per-account outcomes equivalent to:

- `success_authoritative`;
- `success_empty`;
- `success_partial`;
- `failed`;
- `skipped`.

Only a successful response that satisfies the Python authoritative criteria can authorize support withdrawal, and then only when `catalog_withdrawal_policy` permits it.

Failed, exception, malformed, partial, skipped, and non-authoritative empty paths must preserve prior account/model support. One account's failure may not shift/misassociate another account's outcome when concurrent results are collected.

## 6. Refresh isolation and concurrency

Fetch enabled accounts concurrently with bounded task ownership. A panic/error/timeout in one fixture request must become that account's failed outcome rather than canceling unrelated accounts or corrupting the shared cache.

Use per-refresh serialization equivalent to Python's `_refresh_lock` so two full/single-account refresh mutations cannot interleave. Do not hold catalog mutation locks across M4 I/O; gather per-account immutable results first, then apply/persist in a controlled update phase where practical.

All spawned refresh tasks must be cancelled/joined boundedly when the parent refresh is cancelled.

## 7. Single-account recovery hook

Implement the D003 equivalent of `refresh_one_account(account_name)` against D002/M4. This exists so D006 can request bounded recovery when a model is known globally but an otherwise eligible sibling account is missing catalog support.

The method must:

- require known/enabled account/provider/usable credentials;
- use the correct provider/account client;
- seed static models;
- execute one live catalog request;
- apply the same outcome/withdrawal policy as full refresh;
- persist semantic state;
- return a stable outcome rather than throwing ordinary upstream failures through the router.

D006 owns rate limiting of recovery attempts. M8 owns periodic scheduling; D003 only provides the operation.

## 8. Persistence

Extend typed Rust repositories for existing schema tables used by catalog state, including catalog refresh state, models/provider model metadata/account support and pings/price snapshots or model-info rows only where D003 owns deterministic updates.

Persistence rules:

- preserve first-seen timestamps when semantic identity did not change;
- update last-seen only for actually refreshed rows;
- persist successful refresh timestamp once per successful account, not globally;
- do not rewrite another account's freshness as a side effect;
- compare canonical JSON semantically so key ordering alone does not create writes;
- preserve provider-specific resolution status/protocol-source semantics;
- prune durable rows only when the same authoritative rules permit cache pruning;
- use bounded transactions and existing `Database` serialization.

No migration 55 unless review proves schema 54 incapable of parity.

## 9. Quarantine/model reappearance handoff

D003 should expose authoritative model reappearance/withdrawal events with enough exact identity for D005 quarantine integration:

`provider_id`, account durable ID/name, canonical model ID, upstream model ID when known, and upstream protocol.

Do not implement quarantine state in D003. The callback/event boundary must be deterministic and testable so D005 can clear bounded quarantine on authoritative reappearance and distinguish terminal catalog withdrawal from transient runtime suspicion.

## 10. Optional pricing/model-info enrichment boundary

Port pure deterministic logic that materially affects catalog identity or routing capabilities when supplied with metadata:

- alias/match normalization needed to interpret persisted model-info/pricing evidence;
- source precedence/trust gates;
- capability merge behavior;
- bounded TTL/cache semantics where they are part of the current in-process result contract.

Do **not** create a second production outbound client or periodic external polling loop in M5. Use injected fixture responses/database snapshots for D003 qualification. M8 can later attach the generic outbound manager/background schedule.

OpenCode/models.dev or external pricing metadata must remain advisory: failure to enrich cannot delete provider-authoritative model support.

## 11. Security and failure handling

- API keys exist only long enough to construct provider auth headers and are never included in snapshots/errors.
- Catalog response bodies are bounded before full buffering/parsing.
- Invalid upstream JSON cannot crash the process or poison unrelated account state.
- A provider catalog HTTP failure does not mutate health/backoff by itself unless a later owner explicitly applies that policy; D003 reports stable observations only.
- Error diagnostics identify account/provider/status/category without returning secret headers/body content.

## 12. Differential tests

Use deterministic local HTTP/HTTPS/M4 proxy fixtures for:

- GET/POST models endpoint;
- auth/static header shape without secret capture;
- query/body composition;
- DISABLED endpoint + static seeds;
- valid authoritative list;
- invalid JSON;
- wrong top-level/data shape;
- mixed valid/invalid rows;
- empty list under each withdrawal policy;
- failed account among successful siblings;
- concurrent refresh cancellation cleanup;
- one-account refresh;
- protocol/capability/limit resolution;
- provider-specific shared model IDs;
- semantic JSON persistence/no-op refresh;
- freshness only for successful account;
- authoritative withdrawal and reappearance event.

Tests must prove failed/malformed refresh cannot de-pool prior working support.

## 13. Acceptance criteria

D003 closes only if:

- routing-essential catalog traffic uses the M4 provider/account transport;
- static/live/discovery-disabled behavior matches Python fixtures;
- malformed/partial/failed account responses are isolated and non-destructive;
- authoritative withdrawal requires the frozen policy gates;
- freshness/persistence rows match schema-54 semantics;
- one-account recovery is bounded and deterministic;
- provider secrets do not appear in observations/errors;
- optional metadata enrichment can be tested without a new production HTTP stack;
- no inference provider request path exists yet.

## 14. Stop conditions

Do not close if an empty/malformed response removes prior support, one failing account aborts the whole refresh, refresh writes freshness for accounts that did not succeed, optional external metadata becomes required for routing availability, or D003 adds retry/finalization/background-loop ownership that belongs to M7/M8.
