# Plan 202: `status` command and provider health summary

> **Status:** READY FOR IMPLEMENTATION
>
> **Parent:** Plan 198
>
> **Baseline context:** existing `runtime-status`, `readyz`, `HealthManager`, account registry, catalog pings, and runtime diagnostics
>
> **Priority:** P0/P1 operator usability
>
> **Scope:** add a top-level `eggpool status` command that reports concise proxy health plus exactly one human-readable line per configured upstream provider, using authoritative cached/live Eggpool state and no outbound provider probes.

## Executive summary

Eggpool currently has several useful but fragmented operational views:

- `eggpool runtime-status` exposes deep process/runtime diagnostics;
- `eggpool accounts status` reports configuration-level account states such as configured/disabled/missing credential;
- `/v1/healthz` is liveness only;
- `/v1/readyz` defines minimum routing readiness;
- `HealthManager` owns live account backoff/circuit/quarantine state;
- catalog refresh persists upstream `/models` probe latency/status/model counts in `provider_pings`;
- runtime diagnostics own generation, task, reload, retirement, and shutdown state.

The requested `eggpool status` should be the compact operator answer to:

```text
Is the proxy up?
Can it route inference now?
Which upstream providers are connected/configured?
Which are healthy, degraded, disabled, unavailable, or not yet observed?
What is the most useful current evidence without actively pinging providers?
```

It should **not** replace `runtime-status`, `accounts status`, the dashboard, or `readyz`. It is a stable summary surface built from those underlying authorities.

A representative human output target is:

```text
EggPool 0.x.y  ready  http://127.0.0.1:11300  uptime=2h18m  models=41  accounts=4/5
opencode-go   ready       accounts=2/2  models=18  probe=84ms  age=43s
minimax       degraded    accounts=1/2  models=11  backoff=1  last=rate_limited
openrouter    ready       accounts=1/1  models=12  probe=121ms age=2m
local-test    disabled    accounts=0/1  models=0
Runtime: generation=17 reload=idle tasks=ok db=ok
```

Exact spacing may evolve, but default output must remain bounded and one physical provider row per provider.

---

# Goals

1. Add top-level `eggpool status`.
2. Show overall proxy state and general health information.
3. Show each configured upstream provider exactly once in the default provider section.
4. Aggregate provider state across its configured accounts without hiding partial failure.
5. Use current runtime/health/catalog evidence rather than performing new upstream HTTP requests.
6. Support stable machine-readable `eggpool status --json`.
7. Preserve `runtime-status` as the detailed diagnostic command.
8. Remain useful when the proxy process is down: print local configured providers as `unknown` where possible, report proxy unavailable, and exit non-zero.
9. Never print credentials, raw provider bodies, prompts, or unbounded/raw upstream error strings.

---

# Non-goals

Do not:

- rename or remove `runtime-status`;
- make `accounts status` perform live checks;
- probe every provider on each invocation;
- consume provider quota/rate limits merely to display status;
- mutate circuit-breaker state;
- claim that initial `HealthManager` registration proves an upstream is reachable;
- expose raw `provider_pings.error` strings by default;
- add a full terminal dashboard/TUI;
- make `/v1/healthz` expensive or database-dependent;
- change routing behavior as a side effect of observing it.

A future explicit command such as `eggpool probe` or `eggpool status --probe` can be planned separately if users need active reachability testing.

---

# Current authority map

## Configuration/account identity

`rust/src/accounts/registry.rs::AccountIdentity` already owns:

- account ID/name;
- provider ID;
- enabled state;
- usable credential fact;
- routing priority/weight;
- supported protocols/request surfaces.

Use active-generation identity where possible. Do not reconstruct provider/account identity independently from raw TOML when the server is running.

## Live routing health

`rust/src/health/health_manager.rs::AccountHealthSnapshot` owns:

- `is_healthy` / `health_state`;
- last check/success/failure;
- last failure category;
- consecutive failures/cooldowns;
- disabled/cooldown deadline;
- model quarantine/terminal models;
- circuit-breaker stats.

This is the primary authority for current routability/backoff state.

Important nuance: a newly registered account starts in a nominal healthy state before a successful upstream observation. The status layer must not convert that initialization default into a claim that the upstream was recently verified.

## Cached upstream observation

`provider_pings` and `db::PingRepository` persist catalog `/models` probe evidence:

- provider ID;
- account name;
- probe timestamp;
- latency;
- status code;
- error presence;
- model count.

Use the latest bounded evidence per account/provider. The raw `error` column is not safe default CLI output; classify it to a bounded category or omit it.

## Runtime/process health

`rust/src/runtime_lifecycle/diagnostics.rs::RuntimeDiagnosticsSnapshot` already owns:

- active generation ID/digest/age;
- provider/account/model counts;
- active leases/finalization jobs;
- publication/reload state;
- retiring generations;
- task diagnostics;
- startup recovery;
- shutdown state;
- counters/metrics.

Use this rather than creating duplicate process counters.

## Readiness

`rust/src/server/health.rs::readyz` currently checks:

- runtime generation available;
- database readable/writable enough for enabled accounts/model rows;
- configured accounts exist;
- enabled accounts exist;
- credentials are loaded;
- active catalog has models and persisted models exist.

The status snapshot should reuse the same underlying readiness service/facts rather than copy this decision tree into a second handler that can drift.

## Existing `runtime-status`

`/api/stats/runtime` is valuable deep diagnostics but currently contains several explicit placeholder/null/zero fields for memory, active routing, outbound-client, and provider-pool sections. The new status summary must not interpret those placeholders as real measurements.

---

# Workstream 1 — Define typed status snapshot structures

Create a reusable presentation-light status service, preferably under `rust/src/operations/status.rs` or the closest current operations boundary.

Do not place aggregation logic directly in `runtime.rs` CLI printing or `server/health.rs` handlers.

Suggested semantic shapes:

```rust
#[derive(Serialize)]
struct ProxyStatusSnapshot {
    schema_version: u32,
    observed_at: String,
    proxy: ProxyHealthSummary,
    providers: Vec<ProviderHealthSummary>,
    runtime: RuntimeHealthSummary,
}

#[derive(Serialize)]
struct ProviderHealthSummary {
    provider_id: String,
    status: ProviderStatus,
    enabled_accounts: usize,
    total_accounts: usize,
    routable_accounts: usize,
    backoff_accounts: usize,
    unavailable_accounts: usize,
    model_count: Option<usize>,
    last_probe_age_seconds: Option<u64>,
    last_probe_latency_ms: Option<u64>,
    last_probe_status_code: Option<u16>,
    last_observation: ProviderObservation,
    reason_code: Option<String>,
}
```

The exact type names can differ. Keep fields bounded, stable, and secret-free.

Suggested enums:

```text
ProxyStatus: ready | degraded | unready
ProviderStatus: ready | degraded | unavailable | disabled | unknown
ProviderObservation: verified | failed | stale | never
```

Do not serialize arbitrary `health_state` strings as the sole machine contract. Map internal details to a small stable public enum plus bounded `reason_code` values.

---

# Workstream 2 — Provider aggregation semantics

Default output groups by configured provider ID, not by account. One provider must produce one provider line.

Include every provider in the active validated configuration, even if all of its accounts are disabled. This is more diagnosable than silently hiding a configured upstream. `disabled` makes the state explicit.

When the server is unavailable and only local config can be read, list local configured providers as `unknown`/`disabled` based on static enabled state only. Do not pretend local config knows live health.

## Provider state precedence

Use a deterministic state machine.

### `disabled`

All configured accounts for the provider are disabled, or the provider has no enabled account by configuration.

This is an intentional configuration state, not a failure.

### `unavailable`

At least one account is enabled, but no enabled account is currently routable due to live health/circuit/credential/runtime eligibility.

Examples:

- all enabled accounts in auth-failed terminal state;
- all enabled accounts under active account-wide cooldown/backoff/open circuit;
- no enabled account has usable credentials in the active generation;
- all possible accounts excluded by a current provider-wide health condition.

A stale/failed catalog probe alone should not force `unavailable` if current routing health contains newer successful request evidence and a routable account remains.

### `degraded`

At least one account remains routable, but one or more enabled accounts are unhealthy/cooling down/open-circuit/terminally disabled, or the latest trustworthy provider observation indicates a failure while a viable route remains.

Partial model quarantine should also be representable as degraded when it materially removes some advertised model capability. Do not mark an entire provider unavailable solely because one model is quarantined.

### `ready`

At least one enabled account is routable, no enabled account is in a known degraded condition that should surface at provider scope, and there is successful upstream evidence from either:

- a successful account request recorded by `HealthManager.last_success`; or
- a successful current/recent catalog probe.

Define a bounded observation-freshness window from existing catalog refresh cadence rather than hard-coding an arbitrary tiny timeout. If the observation is old but routing health has newer success, use the newer evidence.

### `unknown`

Provider is enabled/routable by static/live gating, but Eggpool has no successful or failed upstream observation yet, or all observation evidence is too stale to make a reachability claim.

This avoids calling a just-registered account `ready` merely because `HealthManager` initializes it optimistically for routing.

## Reason codes

Use safe bounded categories such as:

```text
no_enabled_accounts
no_routable_accounts
authentication_failed
rate_limited
provider_backoff
circuit_open
partial_account_failure
model_quarantine
probe_failed
probe_stale
unobserved
```

Prefer existing `BackoffReason`/health classifier enums. Do not print raw provider exception/error strings.

---

# Workstream 3 — Overall proxy health semantics

Overall status is not simply the worst provider status.

### `ready`

- process/status endpoint reachable;
- current readiness conditions pass;
- active generation available;
- at least one route/model is usable;
- no proxy-internal critical condition is known;
- providers may include intentionally disabled entries, but no active provider degradation that warrants operator attention.

### `degraded`

Readiness still passes and inference remains serviceable, but one or more of these is true:

- an enabled provider is degraded/unavailable while another route remains;
- background task diagnostics report a defined failure/degraded condition;
- a retirement/reload condition is abnormal but current generation remains usable;
- persisted probe/catalog evidence shows partial upstream trouble;
- other explicit reusable runtime diagnostic flag says service remains available with reduced capacity.

Do not infer task failure from arbitrary text. If current task diagnostics do not expose a typed success/failure category, either add one at the runtime diagnostic authority or report task counts/state without using them to change overall status.

### `unready`

Process reachable but current readiness fails: no usable catalog/accounts/credentials/runtime/database readiness.

The snapshot should contain a bounded reason code matching the shared readiness decision.

### CLI-only `unavailable`

If the CLI cannot reach the local/declared Eggpool server, print `proxy unavailable` plus static configured provider rows where possible and use the existing control/unavailable exit-code class. `unavailable` need not become a server-returned proxy enum because no server snapshot exists.

---

# Workstream 4 — Reuse readiness through a shared service

Refactor `readyz` only as much as necessary to prevent duplicated readiness policy.

Preferred design:

```text
operations/runtime health service
    -> readiness_snapshot(...)
        -> readyz HTTP adapter
        -> status snapshot
```

The shared readiness result should contain:

```rust
struct ReadinessSnapshot {
    ready: bool,
    reason_code: Option<ReadinessReason>,
}
```

The `readyz` wire contract can remain its existing small JSON shape/status codes. The status service consumes the richer typed result internally.

Do not make `healthz` call database/runtime readiness checks; liveness stays cheap.

---

# Workstream 5 — Add an authenticated status API surface

The CLI needs one consistent server-side snapshot so it does not race multiple HTTP calls and independently join health/catalog/runtime generations.

Add a small non-OpenAI operational endpoint, suggested:

```text
GET /api/status
```

or the nearest repository convention if `/api/stats/status` is more consistent after implementation-time review.

Requirements:

- authenticated with the normal Eggpool server/operator key;
- never exposed merely because the dashboard is configured public;
- one runtime generation lease/snapshot per request where practical;
- bounded DB reads for latest provider pings/refresh state;
- no outbound provider requests;
- response body below the existing status body ceiling;
- deterministic provider sort by provider ID;
- `schema_version: 1` for machine consumers;
- no credentials/raw ping error/request content.

Keep `/api/stats/runtime` unchanged for deep diagnostics unless a genuinely authoritative missing field is also useful there. Do not stuff the compact status snapshot into that existing placeholder-heavy schema.

---

# Workstream 6 — Latest probe aggregation

`PingRepository` currently supports bounded provider ping reads. Add a focused query/helper if necessary to fetch the latest ping per configured account/provider efficiently.

Avoid loading hundreds/thousands of historical rows and reducing them in CLI code.

Preferred SQL semantics use the latest row per account or provider with a bounded configured-account set. Since provider/account cardinality is small in Eggpool deployments, clarity is more important than clever SQL.

For provider output aggregate:

- latest successful/failing observation timestamp;
- representative/recent latency when the latest authoritative probe succeeded;
- latest status code if safe/useful;
- model count from successful catalog evidence;
- count of accounts with recent failed probes.

Do not expose `provider_pings.error` raw text. If a failed probe has no structured HTTP code, report generic `probe_failed`.

If model counts differ across accounts, use the active catalog/provider model count where that authority exists rather than summing duplicate model lists.

---

# Workstream 7 — Human CLI contract

Add to `rust/src/cli.rs`:

```rust
Status(StatusArgs)
```

with at minimum:

```text
--json
```

Do not initially add a large flag matrix. `--verbose`, `--accounts`, or `--probe` can be added later if a real use case appears.

### Default output

Keep the header concise, then exactly one provider row per configured provider, then one compact runtime summary line.

Recommended information density:

Header:

```text
EggPool <version>  <ready|degraded|unready>  <base-url>  uptime=<...>  models=<n>  accounts=<routable>/<enabled>
```

Provider row:

```text
<provider-id>  <status>  accounts=<routable>/<enabled>  models=<n|?>  probe=<latency|->  age=<duration|never>  [reason=<code>]
```

Runtime footer:

```text
Runtime: generation=<id> reload=<state> tasks=<summary> db=<ok|degraded> retiring=<n>
```

Do not wrap normal provider rows if avoidable. Bound/truncate provider IDs and reason strings using existing diagnostic text limits if necessary.

Do not use ANSI color as the only status signal. If color is later added, preserve plain words for piping/logs and honor non-TTY behavior.

### Provider naming

Use configured provider ID as the stable identifier. Do not expose credential/account labels unless JSON or a later explicit detail mode requires them.

---

# Workstream 8 — JSON CLI contract

`eggpool status --json` prints exactly the typed server snapshot when the server is reachable, or a documented offline wrapper with the same schema/version and `proxy.available=false` if implementing offline fallback in one schema is clean.

Requirements:

- one JSON document;
- deterministic provider ordering;
- explicit schema version;
- numeric durations/latencies in named units;
- no human-formatted-only duration strings as the sole machine field;
- no secret fields;
- no raw error bodies.

Prefer fields such as:

```json
{
  "schema_version": 1,
  "proxy": {
    "status": "degraded",
    "ready": true,
    "version": "...",
    "uptime_seconds": 8123.4
  },
  "providers": [
    {
      "provider_id": "minimax",
      "status": "degraded",
      "enabled_accounts": 2,
      "routable_accounts": 1,
      "reason_code": "rate_limited"
    }
  ]
}
```

Do not make human formatting the JSON source of truth. Human output should render from the typed snapshot.

---

# Workstream 9 — CLI transport and offline behavior

Reuse the bounded HTTP/status-client pattern already present in `runtime.rs::fetch_runtime_status()`:

- resolve wildcard listen host to loopback for local access;
- use configured server port;
- authenticate using the server-key mechanism already used by operational status calls;
- enforce `STATUS_TIMEOUT` or a shared equivalent;
- cap response body bytes;
- reject malformed/non-JSON bodies cleanly.

Prefer extracting a reusable small authenticated local-control HTTP fetch helper rather than duplicating raw TCP HTTP parsing if the current implementation makes that worthwhile. Do not introduce `reqwest` solely for one CLI call.

### Server unavailable

If config loads but the server cannot be reached:

```text
EggPool <version>  unavailable  http://127.0.0.1:11300
<provider-a>  unknown  accounts=?/<configured-enabled>
<provider-b>  disabled accounts=0/<configured-total>
...
```

Static config can determine provider presence/enabled counts, but not routability/reachability. Label unknown honestly.

Use the existing `EXIT_CONTROL_UNAVAILABLE` class (currently code 3) or the repository's current equivalent for an unreachable server.

### Reachable but unready

Print the snapshot, then exit non-zero so shell/system automation can detect failure.

Recommended exit semantics:

```text
0 = process reachable and readiness passes (ready or degraded)
1 = process reachable but unready
3 = process/control endpoint unavailable
```

Do not return non-zero merely because one provider is degraded if another valid route keeps the proxy ready. The human/JSON status still surfaces the degradation.

If current bootstrap error plumbing makes `1` conflict with generic validation errors, introduce a narrow status exit mapping rather than hiding unready state behind a generic parse error.

---

# Workstream 10 — Relation to existing commands

## `runtime-status`

Keep as deep diagnostics. Its output may continue to include generation internals, tasks, DB/WAL sizes, reload state, and diagnostic counters.

Documentation should say:

```text
eggpool status          concise operator/provider health
eggpool runtime-status  detailed process/runtime diagnostics
```

## `accounts status`

Keep its static/configuration-level semantics or rename fields/document them more clearly if needed. Do not make it a second provider-health implementation.

## dashboard

The dashboard may eventually consume the same typed snapshot, but that is optional. Do not delay the CLI to redesign dashboard pages.

## `croncheck` / `ensure-running`

Do not change watchdog semantics unless current implementation can directly reuse the shared readiness result without altering their existing contract.

---

# Workstream 11 — Secret and privacy constraints

The status surface is likely to be pasted into bug reports, so treat it as display-safe diagnostics.

Never include:

- API keys or env-var values;
- auth headers/cookies;
- request prompts/responses;
- tool arguments/results;
- raw upstream response bodies;
- prompt cache keys;
- full raw provider exception text;
- filesystem paths containing secrets unless already established as safe operator diagnostics.

Safe fields include:

- provider/account counts;
- provider ID;
- model count;
- bounded status/reason enum;
- HTTP status code;
- latency/age;
- version/generation ID;
- uptime;
- reload/task counts;
- DB readiness boolean.

Tests should use sentinel secrets and assert absence from both human and JSON output.

---

# Workstream 12 — Tests

## Status service unit tests

Cover provider aggregation:

1. all accounts disabled -> provider `disabled`;
2. enabled/unobserved/routable -> `unknown`;
3. recent successful ping -> `ready`;
4. successful request evidence newer than stale failed ping -> do not incorrectly mark unavailable;
5. one healthy + one rate-limited account -> `degraded`;
6. all accounts in backoff/open circuit -> `unavailable`;
7. authentication failure -> unavailable/degraded according to remaining accounts;
8. single model quarantine -> degraded where provider otherwise works;
9. stale probe with no newer evidence -> `unknown` or stale according to defined policy;
10. raw ping error text never leaves the service;
11. deterministic provider ordering.

## Overall proxy tests

Cover:

- ready with all active providers ready;
- ready but one active provider degraded -> overall degraded, exit 0;
- ready with intentionally disabled provider -> not necessarily degraded;
- no usable models/accounts/credentials -> unready;
- runtime unavailable -> unready server response where applicable;
- reload/retirement/task condition only changes status when a typed rule says it should.

## HTTP endpoint tests

Cover:

- authentication required;
- public dashboard mode does not expose private status endpoint;
- body bounded;
- one snapshot per request;
- no outbound provider transport invoked;
- database failure returns bounded degraded/error result without server crash.

## CLI tests

Extend `rust/tests/cli_contract.rs` and/or the current runtime CLI test target:

- `eggpool status` parses;
- `--json` parses;
- provider rows one per line;
- JSON schema stable;
- unreachable server offline fallback;
- exit codes 0/1/3;
- no secrets in output;
- wildcard host -> loopback behavior;
- oversized/malformed status body rejected safely.

---

# Expected source changes

Likely files:

```text
rust/src/cli.rs
    StatusArgs + Command::Status

rust/src/runtime.rs
    dispatch, bounded local status fetch, human/JSON rendering, exit mapping

rust/src/operations/status.rs              # new preferred aggregation boundary
rust/src/operations/mod.rs                 # module export

rust/src/server/health.rs
    reuse shared readiness service; thin status handler if kept here

rust/src/server/mod.rs
    route registration/auth placement

rust/src/db/repositories.rs
    focused latest-ping query helper if needed

rust/src/runtime_lifecycle/diagnostics.rs
    only if a missing typed diagnostic fact must be added at its authority

rust/tests/cli_contract.rs
server/runtime/operations focused tests

README.md
docs/network-diagnostics.md
docs/deployment.md or CLI reference if appropriate
architecture/ documentation only if authority boundaries change
```

Avoid a new production HTTP-client dependency. Reuse existing bounded transport helpers or extract a small local helper.

---

# Verification

Focused checks should include:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_r003 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test catalog_c001 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test catalog_c002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_r001 -- --test-threads=1
```

Use the exact current target names if the repository changed after this plan.

Then run the full serial workspace suite and locked release build.

---

# Acceptance criteria

1. `eggpool status` exists as a top-level command.
2. Default output includes concise proxy health and exactly one provider row per configured provider.
3. `eggpool status --json` emits a versioned stable structured snapshot.
4. Status does not make outbound provider requests or consume provider quota.
5. Provider aggregation uses active account identity + live health + cached probe/catalog evidence, not configuration state alone.
6. Initial nominal HealthManager state without any successful observation does not falsely report an upstream as verified-ready.
7. Partial account/provider failures produce `degraded` while viable routes remain.
8. No routable enabled account produces provider `unavailable`.
9. Intentionally disabled providers are shown as `disabled` and do not by themselves degrade the proxy.
10. Overall proxy can be `degraded` while still shell-successful when readiness passes.
11. Reachable-but-unready exits non-zero; unreachable server uses the control-unavailable exit class.
12. When the process is down, status still lists locally configured providers as `unknown`/`disabled` where possible.
13. `readyz` and status share one readiness authority so their semantics cannot drift silently.
14. Raw `provider_pings.error` content, credentials, prompts, responses, and tool payloads never appear in status output.
15. `runtime-status` remains available for detailed diagnostics and is not repurposed.
16. No new heavy HTTP/TUI/monitoring dependency is added.
