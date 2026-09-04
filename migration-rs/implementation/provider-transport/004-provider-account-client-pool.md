# Provider Transport T004 — Provider/Account Client Pool and Lifecycle Boundary

Status: closed; see [closure record](../../closure/provider-transport/004-status.md)

Repository baseline for planning: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

Source roadmap: `migration-rs/subsystems/provider-transport-roadmap.md#t004--provideraccount-client-pool-and-lifecycle-boundary`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

Primary class: capability/invariant

## 1. Objective

Port the provider/account HTTP client topology around the direct T002 and proxied T003 transports. The Rust candidate should be able to build the same provider transport graph as Python from one `Config`: one reusable default client per provider and dedicated clients only for accounts with a resolved proxy.

This milestone makes transport ownership available to later catalog/routing/coordinator work but does not dispatch inference requests or make routing decisions.

## 2. Dependencies

Hard: T001-T003 closed.

M5+ remains downstream.

## 3. Python oracle

Re-inspect current `ProviderClientPool`, config proxy resolution, runtime diagnostics consumers, and close/re-registration tests. Use T001's classification to distinguish observable pool behavior from Python implementation details.

The current Python behavior to preserve unless T001 says otherwise includes:

- default client keyed by provider ID;
- account client keyed by `(provider_id, account_name)`;
- account-specific client takes precedence when present;
- direct account falls back to provider client;
- missing provider produces a typed local error;
- snapshot exposes build counts/provider counts/account-client inventory;
- client creation is configuration/generation-time work, not per request.

## 4. Rust module design

Add a focused `ProviderClientPool` under the Rust provider module. Prefer an immutable-after-build structure owned by the current migration runtime/process state. Do not reproduce Python mutability solely because Python has `register()` and displaced-client bookkeeping.

A reasonable structure is:

- provider map: provider ID -> `ProviderHttpClient`;
- account map: `(provider ID, account name)` -> `ProviderHttpClient` only for proxied accounts;
- stable metadata/snapshot computed without exposing credentials or proxy URIs;
- typed construction error containing provider/account identity where safe;
- cheap read access without a global async mutex.

If later runtime generations will replace an entire pool atomically, prefer that model over supporting arbitrary in-place re-registration now.

## 5. Construction from configuration

Implement one build path from the existing Rust `Config`.

For every provider:

1. validate/convert provider transport settings;
2. construct one direct T002 provider client;
3. for each enabled or configured account as defined by T001, resolve proxy URL;
4. when the resolved proxy is non-null, construct a dedicated T003 proxied client using that same provider settings;
5. when null, create no account client and rely on provider fallback.

Construction must be all-or-nothing for a generation candidate. A proxy parse/config error must not quietly omit the account client while leaving the account routable as direct. Return a typed build failure so later runtime generation publication can fail before exposure.

Do not contact provider endpoints merely to build the pool.

## 6. Lookup semantics

Implement a narrow API equivalent to:

- `get_client(provider_id, account_name?)`;
- optional legacy/default-provider helper only if current Rust/later interfaces genuinely need it;
- provider IDs inventory;
- snapshot/diagnostic view.

When `account_name` is supplied and a dedicated proxied client exists, return it. Otherwise return the provider client. Missing provider must fail locally before any network operation.

Do not infer routing eligibility from whether a client exists. A disabled/quarantined/backed-off account is M5 policy; T004 only represents configured transport topology.

## 7. Account isolation invariant

A dedicated proxied account client owns a distinct Hyper connection pool. Two accounts may not share physical provider connections through one pool merely because their provider or proxy endpoint is the same. Direct accounts may share the provider-level direct pool as in Python.

Tests must prove topology identity without depending on internal pointer formatting: observe separate connection counters/fixture sessions or explicit client IDs that contain no secrets.

## 8. Snapshot compatibility

Preserve the operator-relevant Python snapshot shape unless T001 classified a field as incidental:

```json
{
  "build_count": 0,
  "providers": {},
  "account_client_count": 0,
  "account_clients": []
}
```

With configured clients, `providers` counts the provider default plus that provider's dedicated account clients, and `account_clients` contains sorted safe provider/account names. Do not include API keys, proxy URLs, proxy schemes with userinfo, connection destinations containing credentials, or TLS secrets.

This snapshot is transport construction evidence, not a request-rate counter.

## 9. Lifecycle ownership

Document and implement when provider pools are dropped/closed during the migration-stage server lifetime. T004 should integrate the pool into a Rust-owned state boundary only as far as necessary to prove construction and clean shutdown.

Possible integration:

- build pool after F006 listener ownership and config/database initialization;
- store in application/process state even though inference routes remain 501;
- ensure any pool-build failure closes already-open DB/listener resources before process exit;
- ensure graceful server shutdown drops/closes provider pools without hanging.

Do not implement rehash/generation swapping; M8 owns publication/draining. Design the pool so it can later live inside an immutable generation.

## 10. Shutdown and cancellation

Hyper pool drop/close semantics and Eggress stream ownership must be deterministic enough that a process/generation release does not leave sockets or permit ownership alive indefinitely. If explicit async shutdown is required, bound it similarly to the Python 5-second close intent but do not invent a background shutdown supervisor.

A dropped response body or cancelled request remains T002/T003 responsibility. T004 verifies aggregate pool teardown after such cases.

## 11. Tests

Required tests include:

- empty config/provider set as supported by current config contract;
- one provider builds exactly one direct client;
- two providers build one each;
- direct account lookup returns provider client;
- proxied account lookup returns dedicated client;
- mixed direct/proxied accounts on one provider produce expected topology;
- two proxied accounts produce separate pools, including when proxy URI is identical;
- named proxy/inline/env proxy forms produce account clients according to T003 resolution;
- missing provider returns typed error without network activity;
- malformed proxy causes pool construction failure, not direct fallback;
- snapshot exact/semantic parity with Python for controlled configs;
- repeated request use does not increase `build_count`;
- construction does not contact provider upstream;
- graceful shutdown releases direct and proxied loopback connections;
- failed pool construction after server listener/DB acquisition leaves listener reusable and DB cleanly closed;
- snapshot/error output contains no synthetic proxy/API secret markers.

## 12. Differential verification

Run paired Python/Rust topology/snapshot cases using equivalent configs and environment values. For network behavior, reuse T002/T003 fixtures to prove the selected client actually follows direct or proxied path.

Do not compare implementation-specific client object identities. Compare provider/account topology, connection path, build counts, safe inventory, error class, and absence of per-request construction.

## 13. Dependency/resource constraints

T004 should normally add no new third-party dependency. It composes T002/T003 types using standard collections/Arc as needed. Do not introduce DashMap or a concurrent cache unless profiling or real concurrent mutation requires it; the migration target is immutable-after-build.

## 14. Non-goals

- no catalog fetch scheduling;
- no model routing/account scoring;
- no health/backoff/quota state;
- no provider API-key/header injection;
- no inference route dispatch;
- no retry/failover/finalization;
- no rehash generation publication;
- no generic non-provider outbound HTTP manager.

## 15. Acceptance criteria

T004 closes when a Rust `Config` deterministically produces the same provider/direct/proxied-client topology and safe diagnostic snapshot as Python, account-level network isolation is proven, client construction is generation-time rather than request-time, malformed proxies fail the whole candidate build rather than bypassing proxy policy, and shutdown releases transport resources.

## 16. Stop conditions

Stop if implementing topology requires routing/health eligibility decisions; if a proxied account can fall back to a direct provider pool; if sharing a client would collapse account network identity; or if integration begins implementing M8 rehash/generation machinery rather than a future-compatible ownership boundary.

## 17. Closure evidence

Record topology/snapshot parity cases, construction timing/build-count evidence, account isolation observations, failure/cleanup behavior, dependency delta, secret-redaction checks, Rust fmt/clippy/test output, migration differential output, and targeted Python client-pool/proxy tests.
