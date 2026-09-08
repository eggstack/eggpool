# R010 — Active-Generation Authority Audit and Runtime/Reload Diagnostics

Status: closed; see [closure record](../../closure/runtime-lifecycle/010-status.md)

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: invariant/capability

## Objective

Finish the M8 authority conversion. Audit every Rust server/request/diagnostic path for stale startup generation state, make active generation leases authoritative wherever a live field/service is consulted, enforce live request-body limits before excessive buffering, and expose bounded secret-free runtime/reload/task/retirement diagnostics.

R010 is intentionally an authority and observability pass, not a dashboard redesign.

## Remove stale generation authority from `AppState`

Current pre-M8 `AppState` stores startup `Config`, `ProviderClientPool`, and `Arc<InferenceState>`. After R003/R009 the production state should converge toward:

```text
AppState / ServerState
  process runtime / RuntimeManager
  process Database/repositories
  constructor-owned restart-required server/security values
  static assets / route topology
```

There must be no production path that clones a generation-owned `InferenceState`, provider pool, router, catalog, or live config into long-lived Axum state outside `RuntimeManager`.

A compatibility/test constructor may exist only if it cannot be used by production serve and is clearly named/scoped.

## Authority classification

Audit every handler/middleware/background/diagnostic path and classify each dependency as:

- process-owned;
- constructor-owned restart-required;
- active-generation-owned and therefore lease-required.

Commit the audit as test-visible documentation/table so future code review can catch reintroduction of stale authority.

At minimum review:

- authentication middleware;
- request body limiting;
- health/readiness;
- Chat/Responses/Messages inference;
- model listing/model-info surfaces if present;
- dashboard overview/API rendering;
- provider/account/model counts and any runtime status endpoints;
- background callback factories;
- metrics/diagnostic snapshots;
- semantic router/affinity paths.

## Authentication and route topology

`server.api_key`, listener host/port, dashboard route enable/public topology, and other R005 restart-required constructor settings may remain in process/static state. They must not be accidentally changed by live rehash.

Tests must prove changing such a file field returns `RestartRequired` and the running middleware/router remains unchanged.

## Live body-size authority

`server.max_request_body_bytes` is live in the Python policy. Current `RequestBodyLimitLayer` is created from startup config and therefore cannot be the sole authority after R007.

Implement a dynamic bounded body-admission path that:

1. acquires the active generation before buffering an inference request body;
2. reads that generation's max body size;
3. bounds body collection using Axum/http-body primitives at that size (or a documented small framing overhead), returning 413 without constructing M7 request state when exceeded;
4. keeps the same generation lease for the subsequent finite/stream execution so limit and routing cannot come from different generations.

Do not first buffer an unbounded body and then call M7's existing length check. A conservative process-wide hard safety ceiling may remain outside this path if needed, but it cannot prevent the live per-generation limit from taking effect.

If non-inference request bodies exist, classify/enforce them according to the Python contract separately rather than applying inference generation state blindly.

## Readiness and active config

Current readiness checks startup configured accounts/credentials plus durable model rows. After live provider/account changes, readiness must use the active generation's account/credential/catalog authority where Python does.

Required properties:

- a newly accepted provider/account config affects the next readiness probe without restart;
- a probe already holding generation A may complete consistently on A during rehash;
- readiness never combines A config with B registry/catalog;
- DB writability remains process-owned and can be checked without generation mutation;
- publication gate behavior for readiness matches R001 (wait/old snapshot/explicit reloading result as frozen; do not guess).

## Dashboard/read/control authority

Fields marked restart-required (theme/route topology/refresh where applicable) stay startup-owned. Fields marked live and used by render/selection must come from one acquired generation.

Do not copy all generation state back into `AppState` as a “compatibility mirror.” The manager is the authority.

## Runtime diagnostics

Expose a typed bounded snapshot suitable for future M9 control/status commands and current tests/dashboard diagnostics.

Include only safe fields such as:

### Active generation

- generation id;
- digest prefix;
- age/accepted timestamp class;
- active lease count;
- provider/account/model counts from safe metadata;
- finalization supervisor active job count/capacity.

### Publication/reload

- publication epoch;
- gate open/closed and waiter count;
- reload in-progress state/phase;
- last reload result category, changed section names, restart-required path names, duration;
- retirement pending flag/count.

### Retiring generations

- id/digest prefix;
- lifecycle state;
- lease count;
- finalization job count;
- close/retirement elapsed class;
- forced/failed-close flag and sanitized error category.

### Tasks/recovery/shutdown

- R006 task snapshots;
- last startup crash-reconciliation counts;
- process lifecycle/shutdown state;
- aggregate task/reload/retirement counters.

Use bounded last-result records and aggregate counters, not unbounded histories.

## Secret and cardinality safety

Diagnostics must not contain:

- API keys, tokens, proxy URI credentials;
- raw request/provider bodies;
- route-session identity/hash unless the existing public contract explicitly needs a hash (default: omit);
- full config serialization;
- arbitrary upstream error body/text;
- unbounded provider/model/error label cardinality.

Seed unique sentinel secrets in tests and recursively search diagnostic serialization/Debug/Display for them.

## Runtime metrics authority

If Rust currently exposes runtime metrics derived from generation services, make snapshot construction acquire one active generation and serialize a coherent snapshot. Process-owned counters may be combined with generation metadata only while the generation lease remains valid.

Do not cache direct router/catalog/service pointers across rehash for metrics convenience.

## Tests

Required tests:

### Authority audit

- test-visible table covers every production Axum route/middleware dependency that can read config/generation state;
- static grep/source audit asserts no production `AppState` field exposes `InferenceState`/`ProviderClientPool`/generation `Config` directly;
- no background closure captures generation service outside per-tick lease.

### Body limit

- generation A accepts body under A limit;
- rehash to smaller B limit causes next request to return 413 before M7/provider dispatch;
- request that acquired A before rehash still uses A limit and A routing consistently;
- rehash to larger limit takes effect on next request;
- oversized body does not allocate a buffer substantially beyond configured bound (test via chunked body/poll count where practical).

### Readiness/live state

- live account/provider add/remove/disable affects next readiness result;
- readiness race with rehash is generation-coherent;
- restart-required auth/dashboard topology change is rejected and current behavior unchanged.

### Diagnostics

- active/retiring/reload/task/shutdown snapshots transition correctly through publish/rollback/retirement/failure/shutdown;
- repeated reloads/tasks do not grow diagnostic storage;
- sentinel secrets/raw bodies are absent from JSON/Debug;
- digest is prefix/bounded, not full secret-bearing config;
- failed old-generation close is visible without poisoning active request path.

## Scope boundaries

R010 must not:

- add new dashboard pages or redesign SSR;
- implement M9 control/status CLI transport;
- make restart-required fields live merely to simplify authority code;
- add a general metrics backend/telemetry framework;
- change M7 coordinator behavior;
- add DB schema.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R010 authority/diagnostic tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <Python stale-state/reload/readiness/runtime-metrics tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R010 closes only when:

- production Axum state has no direct generation-owned service authority outside the manager;
- every live request-visible field is read from one acquired generation;
- live max request body size is enforced before excessive buffering and stays generation-consistent with routing;
- readiness/dashboard/control behavior follows R005 authority classification;
- diagnostics are coherent, bounded, and secret-free;
- no M9 transport/UI scope is introduced;
- full M7 and reload/shutdown suites remain green.

## Closure

Write `migration-rs/closure/runtime-lifecycle/010-status.md` and promote R011.
