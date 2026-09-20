# Plan 227 — Coordinator/Provider Ownership and Allocation Cleanup

Date: 2026-09-20  
Status: complete  
Planning baseline: 3e90d36c4094af1c93756ace1c882f55b5f8f5d5  
Parent roadmap: plans/225-native-runtime-performance-optimization-roadmap.md  
Prerequisites: Plan 226 complete; prefer Plan 191 complete first if provider construction changed  
Priority: P1 hot-path ownership/allocation cleanup  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Remove avoidable cloning, temporary allocation, and synchronization from the
coordinator/provider path after the request admission/body ownership boundary is
made efficient by Plan 226.

This plan must preserve public behavior and existing inspection/construction
surfaces. The goal is not to make every type borrowed. The goal is to keep
large or frequently repeated ownership changes out of the synchronous
pre-dispatch hot path, while materializing a fully owned
PreparedUpstreamAttempt before any provider send is awaited.

## Current evidence

### 1. Finite coordinator clones attempt inputs

rust/src/coordinator/finite.rs currently constructs AttemptInput by cloning:

- FinalizationIdentity;
- ProviderConfig;
- account credential into an owned String;
- incoming HeaderMap;
- request_id;
- correlation_id;
- raw Bytes handle;
- ConfiguredWireProfile;
- candidate fingerprint.

It then calls:

    prepare_admitted(attempt_input, request.admitted.clone())

The preparation method is synchronous.

### 2. Streaming coordinator repeats the same ownership pattern

rust/src/coordinator/streaming/coordinator.rs builds the same owned AttemptInput
shape and clones request.admitted before synchronous preparation.

The request must become fully owned before submit_once awaits network I/O, but
it does not need to duplicate all of those structures just to prepare headers,
path, and wire bytes.

### 3. AttemptBuilder only carries a small subset forward

rust/src/coordinator/attempt.rs::prepare_with_admission builds a WireRuntimeContext,
prepares the wire request, composes headers/path, then returns
PreparedUpstreamAttempt.

submit_once ultimately consumes:

- provider/account identity;
- upstream model id;
- selected profile/fingerprint;
- method/path/headers;
- body Bytes;
- stream flag.

The larger PreparedRequest inspection structure returned by WireRuntime is not
used by AttemptBuilder except for prepared.body.bytes.

### 4. ProviderClientPool hot lookup allocates and locks

rust/src/providers/client_pool.rs documents the topology as immutable, but its
inner representation is:

    clients: Mutex<BTreeMap<String, ProviderHttpClient>>
    account_clients: Mutex<BTreeMap<(String, String), ProviderHttpClient>>

Account lookup constructs:

    (provider_id.to_owned(), account_name.to_owned())

for every account-specific request.

The mutexes are also taken for every lookup although topology changes only
during construction and close.

### 5. Static routing facts are rebuilt on each request

rust/src/coordinator/endpoints.rs::static_routing_facts currently clones
known_providers and allocates protocol/transcode Strings and an empty
capability-policy map for each request.

The known provider set and transcode surface set belong to an immutable runtime
generation.

### 6. Incoming header filtering lowercases an already-normalized name

rust/src/server/inference.rs::filtered_incoming_headers calls
to_ascii_lowercase for every incoming header before matching static lower-case
names.

http::HeaderName already provides a normalized case-insensitive header-name
representation. This allocation is unnecessary.

## Required end state

After this plan:

1. finite and streaming coordinators use a shared internal preparation path
   that borrows request/provider/config data through synchronous preparation;
2. no deep AdmittedRequest clone is required solely to prepare one attempt;
3. no full incoming HeaderMap clone is required solely for synchronous
   preparation;
4. credentials are borrowed during preparation and only encoded into the final
   Authorization/header values that must be owned;
5. WireRuntime has an internal dispatch-oriented path so AttemptBuilder does
   not construct/clone inspection-only PreparedRequest fields it immediately
   discards;
6. ProviderClientPool account lookup performs borrowed &str map lookup with no
   temporary String tuple allocation;
7. normal provider-client lookup does not require a per-request topology mutex;
8. close still atomically prevents new submissions and releases the pool-owned
   client handles while already-cloned request handles may finish;
9. static routing inputs are generation-owned/precomputed;
10. filtered incoming headers do not allocate a lowercase String per header;
11. existing public helper/types remain source-compatible unless a narrower
    crate-private item is demonstrably not public contract.

## Workstream 1 — Add a borrowed synchronous attempt-preparation core

### Authority

- rust/src/coordinator/attempt.rs
- rust/src/coordinator/finite.rs
- rust/src/coordinator/streaming/coordinator.rs

### Preserve existing public types

Do not delete or incompatibly reshape:

- AttemptInput;
- PreparedUpstreamAttempt;
- AttemptBuilder::prepare;
- AttemptBuilder::prepare_admitted.

Tests and sibling modules already use these explicit ownership boundaries.

Instead, introduce a crate-private borrowed representation or equivalent helper
used by the finite/streaming hot paths.

A conceptual shape is:

    AttemptPreparation<'a> {
        identity: &'a FinalizationIdentity,
        provider: &'a ProviderConfig,
        account_api_key: Option<&'a str>,
        incoming_headers: &'a HeaderMap,
        request_id: Option<&'a str>,
        correlation_id: Option<&'a str>,
        raw_body: &'a Bytes,
        client_surface: ClientSurface,
        profile: &'a ConfiguredWireProfile,
        stream: bool,
        candidate_fingerprint: &'a str,
        admitted: &'a AdmittedRequest,
    }

The exact type/name is implementation-owned.

### Ownership boundary

The borrowed object must never cross an await.

Preparation remains synchronous and must return a fully owned
PreparedUpstreamAttempt before submit_once is called.

Do not create long-lived self-referential/lifetime-heavy request state.

### Shared implementation

Existing owned prepare/prepare_admitted methods and the new borrowed path should
delegate to the same validation/header/path/wire core.

Do not fork authentication, static-header overlay, path expansion, or
compaction capability logic.

### Credentials

Finite and streaming coordinators currently call map(str::to_owned).

Use Option<&str> during synchronous header construction.

The final HeaderValue owns whatever bytes are required; no standalone copied
credential String should survive merely to feed add_auth_header.

Credentials must remain absent from diagnostics and persistence.

## Workstream 2 — Add an internal dispatch-oriented WireRuntime preparation path

### Authority

- rust/src/wire/runtime.rs
- rust/src/coordinator/attempt.rs

PreparedRequest is valuable for focused wire tests and inspection because it
contains:

- identity;
- canonical request;
- semantic metadata;
- adaptation summary/notices;
- byte facts;
- stream intent;
- admission;
- encoded body.

AttemptBuilder currently only carries the encoded body into
PreparedUpstreamAttempt.

Do not remove fields from PreparedRequest.

### Preferred internal shape

Factor wire request preparation into a common core that can produce:

- the owned encoded body required for dispatch;
- the same validation/adaptation errors;
- any lightweight facts actually needed by AttemptBuilder.

Then:

- public prepare_request/prepare_admitted_request can continue constructing the
  full PreparedRequest;
- the coordinator hot path can call a crate-private dispatch method that avoids
  cloning the canonical/admission tree solely to populate fields it discards.

A conceptual result could be:

    PreparedWireDispatch {
        body: Bytes,
        adaptation: ... only if the caller actually needs it
    }

Do not introduce duplicate codec logic.

### Native path

When Plan 226's owned-Bytes path selects native/no-rewrite forwarding, the
dispatch path should:

- borrow canonical/admission for validation;
- reuse the Bytes handle;
- avoid cloning the canonical request entirely.

### Rewrite/transcode path

Codec paths may require an owned CanonicalRequest when model rewriting occurs.

Prefer:

- borrowing the canonical request when no semantic mutation is required;
- cloning only for the actual model rewrite or codec API that requires
  ownership.

Do not contort public codec APIs solely to eliminate small scalar/String clones
unless measurements show they matter.

## Workstream 3 — Make ProviderClientPool topology immutable on the hot path

### Authority

- rust/src/providers/client_pool.rs
- existing provider-pool tests
- runtime-generation close tests

### Build topology locally before publication

from_config should build ordinary local maps first.

Do not lock around each insertion while the pool is still private and
single-owner.

Use a topology object containing:

- provider default clients;
- account-specific clients.

For account clients, prefer a nested map:

    provider_id -> account_name -> ProviderHttpClient

so both lookups can use borrowed &str keys without allocating a tuple of owned
Strings.

### Preserve close semantics

The current close contract is important:

- once closed, get_client returns Closed and no new submission can enter;
- pool-owned client handles are dropped/cleared;
- a request that already cloned a ProviderHttpClient may finish;
- repeated close calls are idempotent;
- close_count is monotonic;
- providers/snapshot after close reflect no active pool-owned clients.

Use the already-present arc-swap dependency if it provides the cleanest
representation, for example an atomically replaceable optional immutable
topology.

A reasonable conceptual representation is:

    topology: ArcSwapOption<ClientTopology>

with:

- construction publishing Some(Arc<ClientTopology>);
- get_client loading one immutable snapshot;
- close atomically swapping None.

The exact ArcSwap API is implementation-owned.

Do not add a new dependency for this.

### Race semantics

A lookup racing with close must retain the existing lease intent:

- if it obtained/cloned a client before close won, that in-flight request may
  finish;
- lookups after the closed state is visible must fail Closed.

Add a deterministic race/close test only if existing runtime tests do not
already cover this boundary.

Do not add sleeps or yield-count assumptions.

## Workstream 4 — Precompute generation-static routing inputs

### Authority

- rust/src/coordinator/endpoints.rs
- InferenceState construction
- request admission/routing tests

The following are static for an immutable generation/surface:

- known_provider_ids;
- requested client protocol for each ClientSurface;
- transcode protocol set;
- empty/default capability policy;
- catalog stale default in this endpoint path.

Store/reuse these in InferenceState or another generation-owned object.

The request should derive RoutingRequestFacts by borrowing those inputs.

Do not move truly request-dynamic fields into shared state.

If StaticRoutingFacts must remain owned for an existing public constructor,
keep that constructor unchanged and add a borrowed/internal path for the
optimized endpoint.

## Workstream 5 — Remove trivial header filtering allocation

### Authority

- rust/src/server/inference.rs
- server/inference unit tests if present

Replace per-header to_ascii_lowercase allocation with direct comparison against
the normalized HeaderName string or static HeaderName values.

Preserve the exact deny list:

- authorization;
- proxy-authorization;
- x-api-key;
- host;
- content-length;
- x-eggpool-route-session;
- connection;
- transfer-encoding;
- upgrade;
- keep-alive.

Do not accidentally forward credentials, routing-session identity, or
hop-by-hop framing.

## Workstream 6 — Keep the optimization bounded

Do not optimize these areas in this plan:

- RoutingRouter selection_lock;
- SQLite connection/gate;
- streaming mpsc bridge;
- Tokio runtime flavor;
- provider transport protocol implementation;
- Eggfetch connection pooling;
- Eggress facade migration.

Plan 228 owns measurement of the first four. Plan 191 owns Eggress.

## Regression coverage

### Attempt preparation

Add/extend tests proving the owned and borrowed preparation paths produce
identical:

- path;
- method;
- provider/account identity;
- selected wire profile;
- request-id/correlation headers;
- static provider/surface headers;
- authorization header;
- filtered forwarded headers;
- body bytes;
- stream flag;
- error classifications.

Never assert or log raw credential values beyond existing secret-safe fixtures.

### Provider client pool

Cover:

- empty/no-provider pool;
- provider default lookup;
- account proxy lookup;
- missing provider;
- account fallback to provider default where current behavior does so;
- snapshot/build_count parity;
- providers list parity;
- close idempotency;
- lookup after close;
- already-cloned client survival across close;
- no-default-feature provider transport where relevant.

### Routing facts

Compare precomputed/static path against existing expected RoutingRequestFacts
for all ClientSurface values and provider-qualified routing.

### Header filtering

Use mixed-case construction if HeaderMap permits it and prove the same blocked
set remains blocked.

## Performance evidence

Compare the Plan 226 candidate to this plan's candidate under the same local
fixture.

Focus on:

- large native finite Responses;
- large native streaming Responses pre-handoff;
- concurrency 1, 4, and 16;
- account-specific proxied-client lookup.

Record request throughput/p50/p95 and, when feasible, allocation/profile data.

Do not add jemalloc, dhat, Criterion, or another profiling dependency merely to
produce closure evidence.

If a standard profiler is available on the development host, use it
out-of-tree and record only secret-free aggregate findings.

## Focused validation

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1

cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
~~~

Then run the full repository baseline in AGENTS.md.

No new dependency is expected.

If Plan 191 lands in the same implementation window and Cargo changes are
present, run cargo deny, cargo tree -e features, cargo tree --duplicates, and
the locked release build.

## Stop conditions

Stop and investigate if:

1. the borrowed preparation path needs references to survive submit_once await;
2. a clone removal causes shared mutable request state;
3. ProviderClientPool close can no longer promptly drop pool-owned clients;
4. a topology swap can permit new submission after close is authoritative;
5. the optimized wire dispatch path bypasses adaptation-policy validation;
6. the dispatch path and public PreparedRequest path begin producing different
   body/error semantics;
7. provider/account fallback behavior changes;
8. header filtering becomes less restrictive;
9. optimization requires a new general-purpose concurrency/dependency layer.

## Completion criteria

This plan is complete when:

- [ ] finite coordinator no longer deep-clones AdmittedRequest solely for
      synchronous attempt preparation;
- [ ] streaming coordinator has the same improvement;
- [ ] incoming headers/provider/profile/credential ownership is borrowed where
      safe through the synchronous preparation phase;
- [ ] PreparedUpstreamAttempt is fully owned before provider send awaits;
- [ ] public AttemptInput/prepare/PreparedRequest behavior remains available;
- [ ] AttemptBuilder can use a dispatch-oriented wire path that does not build
      inspection-only cloned fields;
- [ ] provider/account client lookup allocates no String tuple key;
- [ ] normal client lookup no longer requires a topology mutex;
- [ ] close semantics and diagnostics remain equivalent;
- [ ] generation-static routing inputs are reused;
- [ ] filtered header names no longer allocate lowercase Strings;
- [ ] focused provider/coordinator/wire tests pass;
- [ ] no-default-feature checks pass;
- [ ] full repository validation passes;
- [ ] before/after evidence is appended to this plan.

## Handoff note

Keep borrowing local.

The clean boundary is:

    long-lived request/generation state
        -> borrowed synchronous preparation
        -> fully owned PreparedUpstreamAttempt
        -> await provider send

Do not allow lifetime-driven design to leak across that final arrow. The
performance win comes from removing unnecessary temporary ownership, not from
making the whole coordinator reference-based.

## Closure record — 2026-09-20

Implementation candidate: `aba2ab33945e21bdc9086a8c80734d4d6ce62209`.

Evidence:

- Finite and streaming coordinators now use borrowed synchronous
  `AttemptPreparation`; credentials, incoming headers, provider/profile data,
  request IDs, and the admitted request are not deep-cloned solely for
  pre-await preparation. The owned `PreparedUpstreamAttempt` remains the
  provider-send boundary.
- `WireRuntime::prepare_admitted_dispatch` is internal and dispatch-oriented;
  native no-rewrite requests retain the ingress `Bytes` handle while public
  `PreparedRequest`/slice helpers remain available.
- `ProviderClientPool` builds local maps before publication, uses a nested
  provider/account topology with borrowed lookups, and atomically swaps the
  topology to `None` on close. Existing close, fallback, snapshot, and cloned
  client-survival semantics passed `provider_transport`.
- Static routing inputs are generation-owned per client surface, and incoming
  header filtering compares normalized names without allocating lowercase
  strings.
- Focused coordinator, provider, wire, default, no-default, and tooling gates
  passed; no dependency or feature changes were made.
