# R012 — Wire-Negotiation Runtime Authority and Reload-Diagnostics Re-Closure

Status: closed; see [closure record](../../closure/runtime-lifecycle/012-status.md)

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Repository baseline: `2220ad001066200e8240f2d597702458ab11ccc1` (post-R011 closure metadata correction).

Primary class: invariant/corrective

Hard dependencies: accepted R011 historical aggregate closure and the closed C003/C012-C014 wire/retry contracts.

## Objective

Correct the two post-R011 M8 defects found by repository audit, add regression evidence that would have prevented the premature closure, and re-close M8 without reopening the rest of the runtime-lifecycle architecture.

R012 has exactly two production objectives:

1. make the one process-owned `WireResolver` authoritative for the validated `routing.wire_negotiation` configuration at startup and after an accepted live rehash, while retaining compatible learned/rejected state and preserving bounded resolver ownership; and
2. make reload diagnostics owned by the retained reload transaction rather than the caller future, so caller cancellation or a concurrent `Busy` result cannot strand or falsely clear the process `reload_in_progress` state.

R012 is a closure correction, not an M8 redesign. R001-R010 remain accepted. R011 remains append-only historical aggregate evidence, but it no longer authorizes M9 until R012 closes.

## Why R011 missed these defects

R011 exercised a live rehash using `server.max_request_body_bytes`, `models.refresh_interval_s`, and model-router changes. It asserted the R005 classification table, but it did not mutate any `routing.wire_negotiation.*` value and then observe the process-owned resolver. The aggregate therefore proved that the config fields were *classified* live without proving that the live authority actually changed.

R010/R011 reload-diagnostic tests awaited ordinary reload completion. R007 cancellation tests proved that the durable/runtime transaction survives caller cancellation, but they did not assert diagnostics after dropping the caller future. They also did not hold one reload in progress while a second caller returned `Busy`. The current diagnostic begin/finish calls live outside the owned transaction, so those tests could not detect stale or prematurely-cleared `reload_in_progress` state.

R012 must add failing-before/passing-after tests for both omissions. Do not weaken R001/R005 classification or normalize these defects away.

## Authoritative Python and migration sources

Before changing Rust, inspect the current Python oracle and existing migration evidence at minimum:

- `src/eggpool/config_reload_policy.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/generation_factory.py`
- `src/eggpool/reload_transaction.py`
- `src/eggpool/reload_diagnostics.py`
- startup/reload wiring in `src/eggpool/app.py`
- `src/eggpool/cli_rehash_helper.py` only for server-side reload result/diagnostic semantics
- `migration-rs/fixtures/runtime-lifecycle/r001-python-observations.json`
- `migration-rs/authority/runtime-lifecycle.md`
- closed R005, R007, R010, and R011 closure records and tests.

Relevant Rust sources include:

- `rust/src/coordinator/wire_resolver.rs`
- `rust/src/runtime_lifecycle.rs`
- `rust/src/reload.rs`
- `rust/src/config_reload_policy.rs`
- `rust/src/config.rs`
- `rust/src/server.rs`
- `rust/tests/runtime_lifecycle_r005.rs`
- `rust/tests/runtime_lifecycle_r007.rs`
- `rust/tests/runtime_lifecycle_r010.rs`
- `rust/tests/runtime_lifecycle_r011.rs`.

If the existing R001 fixture does not contain enough observable Python behavior to decide live wire-policy transition semantics or concurrent diagnostic semantics, add a **new** bounded `r012-python-observations.json` fixture and targeted migration test. Do not rewrite R001 observations merely to make Rust pass.

## Part A — process-owned wire resolver must consume live configuration

### A1. One shared resolver remains the owner

Keep the C003/M8 architecture: one process-owned resolver survives generation publication so compatible learned/rejected wire state can survive reload.

Do not solve the defect by constructing one independent resolver per generation. That would discard useful learned state, duplicate negotiation flights/gates, and contradict the accepted M8 ownership split.

Do not add a second negotiation scheduler or provider HTTP path.

### A2. Map the validated config exactly

Create one explicit conversion from `Config.routing.wire_negotiation` to the resolver's runtime policy. The runtime policy must cover every field R005 classifies live:

- `enabled`;
- `max_concurrent_per_provider`;
- `min_negotiation_interval_s`;
- `rejection_cooldown_s`;
- `learned_preference_ttl_s`;
- `cache_max_entries`.

Internal hard bounds such as provider-state and metric-label caps may remain implementation-owned, but they must not silently replace a configured field.

Startup must install the configured policy before the first request can negotiate a wire. `ProcessRuntime::new(Database)` may remain for tests/backward compatibility, but production startup/factory wiring must not leave the default resolver policy authoritative when the validated startup config differs.

### A3. Stage live resolver policy with the R007 acceptance transaction

Candidate construction must not mutate the process resolver before acceptance. A failed, restart-required, stale, invalid, cancelled-before-acceptance, or preflight-failed reload must leave the current resolver policy and cache state unchanged.

Add a small staged resolver-policy transition, analogous in responsibility to the staged task-spec diff:

- prepare/validate outside the admission gate;
- capture the previous process policy and any bounded state transformation needed for rollback;
- commit the policy inside the same short R007 acceptance window as durable config-derived state, active pointer, and task-spec acceptance;
- rollback the resolver policy if the transaction remains reversible and a later mandatory acceptance step fails;
- never reopen admission with generation B active while the process resolver still exposes generation A's accepted live policy.

Names are implementation-defined. Do not introduce a generic transaction/workflow framework.

### A4. Preserve compatible learned/rejected state under policy changes

A policy-only rehash must not indiscriminately flush compatible wire observations.

Required behavior:

- structural candidate fingerprints continue to decide whether learned/rejected entries are compatible;
- increasing cache capacity preserves existing entries;
- decreasing cache capacity trims immediately to the new bound using the existing deterministic LRU discipline;
- changing learned TTL/rejection cooldown applies the accepted policy to subsequent eligibility decisions without leaving entries effective beyond the new policy solely because their expiry was materialized under the old policy;
- changing minimum negotiation interval affects subsequent leader eligibility using the existing last-negotiation evidence;
- disabling negotiation stops new dynamic negotiation/learning behavior according to the Python oracle while preserving bounded dormant state when doing so is safe;
- re-enabling negotiation resumes from compatible state only when the Python contract permits it;
- operator-fixed preference remains authoritative and must not be weakened by this correction.

If applying a new TTL/cooldown correctly requires storing observation timestamps instead of only precomputed expiry instants, make that bounded representation correction rather than clearing the cache.

### A5. Concurrency-limit transition

`max_concurrent_per_provider` is live. Existing Tokio semaphores created under the old limit therefore cannot remain silently authoritative forever.

Implement a bounded transition with these semantics unless the Python oracle is stricter:

- negotiations already holding old permits may finish;
- after the old in-flight set drains, all new leader acquisition obeys the newly accepted limit;
- reducing the limit must not require killing an accepted in-flight negotiation;
- increasing the limit becomes observable without process restart;
- obsolete gate/limiter objects are retired and do not accumulate across repeated reloads;
- follower/leader cancellation semantics from C003/C012 remain unchanged.

A simpler shared counter/limit representation is acceptable if it reduces state compared with epoching semaphores. Do not add an actor or generic rate-limit dependency.

### A6. `enabled` must be behavioral, not diagnostic-only

A reload that changes only `routing.wire_negotiation.enabled` and is reported `Applied` must change subsequent request/negotiation behavior. It is not sufficient for the new value to exist only in `RuntimeGeneration.config()`.

Freeze the exact disabled behavior from Python. At minimum prove that disabling does not create new negotiation flights or learned/rejected updates and that normal fixed/native request dispatch still follows the supported coordinator policy.

## Part B — retained reload worker owns diagnostic lifecycle

### B1. Move diagnostic ownership into the owned transaction

`ReloadService::reload()` intentionally spawns an owned task so caller cancellation cannot abandon a staged rehash. The diagnostic lifecycle must follow that same ownership.

Begin and finish the `reload_in_progress` lifecycle inside the owned worker, not in the caller future around `join.await`.

The implementation must guarantee that, once an owned reload operation has ended, process diagnostics do not remain `in_progress=true` merely because the caller future was dropped.

A small synchronous diagnostic guard/token is preferred because diagnostic state uses a synchronous mutex and can therefore clear ownership in `Drop` if the owned future unwinds or is aborted. Do not add an async cleanup framework.

### B2. Concurrent `Busy` calls must not clear another operation's state

One reload may own the process reload transaction at a time. A second caller that returns `Busy` while the first transaction remains active must not set global `reload_in_progress=false` or replace the active operation's phase incorrectly.

Use explicit operation ownership/token semantics or an equivalent narrow mechanism:

- one operation token owns `in_progress` and phase;
- only that token may clear the active marker;
- non-owning `Busy`/rejected calls may update bounded counters/result evidence only if doing so matches Python, but must not falsify active state;
- completion from an older operation must not clear a newer operation if ordering ever overlaps during shutdown/test injection;
- no unbounded reload-history queue is introduced.

### B3. Result and counter semantics

Preserve the accepted R010/R011 secret-free diagnostic shape unless the Python oracle requires a correction.

Explicitly qualify:

- completed applied reload;
- no-op;
- restart-required;
- validation failure;
- stale digest;
- `Busy` while another reload remains active;
- caller cancellation while the owned transaction proceeds to success;
- caller cancellation while the owned transaction reaches a typed failure;
- shutdown/abort path.

The final snapshot must be internally coherent: `in_progress`, phase, counters, active generation/digest, and last terminal result must describe a state that could actually have occurred.

No config bytes, credentials, proxy URLs, request/provider bodies, or unbounded exception text may enter diagnostics.

## Part C — regression and differential qualification

Create `rust/tests/runtime_lifecycle_r012.rs` (or an equivalently focused single corrective suite). The suite must fail against baseline `2220ad0` for the relevant assertions and pass after the correction.

### Required wire-policy tests

1. **Startup non-default authority** — construct production-equivalent startup with non-default wire-negotiation values and prove the process resolver exposes/obeys those values rather than `WireResolverConfig::default()`.
2. **Enabled toggle** — enable -> disable -> enable through accepted live reloads; assert exact generation/epoch transitions and Python-compatible negotiation behavior with no duplicate flights/gates.
3. **Cache capacity** — seed more observations than a reduced candidate capacity, apply reload, and prove immediate deterministic trim and bounded state.
4. **TTL/cooldown** — use deterministic `Instant` values to prove shortened and lengthened learned/rejection policies become authoritative without wholesale compatible-state loss.
5. **Minimum interval** — accepted interval change alters subsequent leader eligibility.
6. **Concurrency limit** — block old in-flight leaders, reduce/increase the live limit, release them, and prove new acquisitions converge to the accepted cap without killing old work or leaking obsolete gates.
7. **Failed reload isolation** — invalid/restart/stale/candidate/task/persistence failure does not change resolver policy or its compatible state.
8. **Repeated policy reload** — many bounded transitions leave resolver entries/provider state/gates/metrics within configured/internal caps.

### Required diagnostic tests

1. **Caller cancellation after worker start** — block the retained worker, drop/abort the public caller, release the worker, then prove diagnostics reach a terminal non-stale state.
2. **Busy cannot clear active** — hold reload A in progress, invoke reload B and receive `Busy`, then assert `in_progress` remains true for A until A finishes.
3. **Cancelled success and cancelled failure** — retained worker terminal result/counters remain coherent for both outcomes.
4. **Shutdown interaction** — shutdown/abort cannot strand `in_progress=true` after reload ownership is gone.
5. **Repeated cancellation/busy storm** — bounded counters/state only; no waiter/task/history leak; the next ordinary reload succeeds without restart.

### Cross-surface regression

Re-run the closed R003/R005/R007/R009/R010/R011 focused suites. At least one R012 test must compose a real Axum request after live wire-policy reload so the new generation and process resolver are observed through the production request path rather than helper methods alone.

No paid/live provider is required; use deterministic local provider/negotiation fixtures.

## Exact vs semantic parity

Exact parity is required for:

- config field disposition (`live` remains live; do not downgrade the fields to restart-required to avoid implementation work);
- reload result category;
- generation id/publication epoch changes;
- unchanged active state on rejected/failed reloads;
- whether negotiation is enabled;
- accepted cache-capacity bound;
- diagnostic `in_progress` ownership and terminal clearing;
- secret redaction and bounded diagnostic vocabulary.

Semantic parity is permitted for:

- exact wall-clock timing around TTL/cooldown/min-interval tests, provided deterministic clocks establish the same before/after boundary;
- the internal limiter representation used to converge a changed concurrency cap;
- exact order in which already-in-flight old-policy negotiations finish;
- implementation-specific operation IDs/tokens, which must not be externally serialized unless already part of the contract.

Do not normalize away an incorrect active policy, stale diagnostic flag, extra negotiation, cache overflow, or publication mismatch.

## Failure, cancellation, restart, and contention semantics

- Candidate/preflight/persistence failure before acceptance leaves resolver policy unchanged.
- Post-pointer reversible failure restores the prior resolver policy before admission reopens.
- An unrecoverable post-durable-commit compensation failure remains fail-closed under the existing R007 semantics; do not invent a second rollback model in R012.
- Caller cancellation does not cancel the retained reload transaction and cannot strand diagnostics.
- Resolver policy reconfiguration must not hold the M5 routing selection lock, perform provider I/O, or sleep while the reload admission gate is closed.
- Existing negotiations are isolated from policy transition; new work converges to the accepted policy without process restart.
- Restart-required wire-adjacent constructor fields remain restart-required; R012 changes only the already-live `routing.wire_negotiation.*` contract.

## Database/config/API/CLI/SSR compatibility

Database:

- no schema migration;
- no new durable resolver-policy table;
- existing Python-created database remains readable;
- R007 persistence behavior is unchanged except for atomic composition with the process resolver policy stage.

Config:

- preserve the R005 exact path/disposition table;
- do not change defaults merely to match current hard-coded resolver defaults;
- unknown fields remain fail-closed restart-required.

API/CLI:

- no new HTTP route, control socket, or CLI command;
- `ReloadService` result vocabulary remains the M9 handoff surface;
- M9 remains blocked until R012 closes.

SSR/dashboard:

- no visual or DOM change;
- diagnostics remain an in-process typed surface for future M9 exposure.

## Dependency and scope constraints

Expected dependency change: **none**. `arc-swap`, Tokio, and existing synchronization primitives are sufficient.

Do not add:

- another resolver instance per generation;
- actor/workflow/job frameworks;
- a generic dynamic-config framework;
- another scheduler;
- another HTTP/TLS stack;
- an ORM or schema migration;
- M9 daemon/control/CLI/update/backup work;
- M10 platform/CI characterization.

The preferred implementation is a small shared resolver-policy holder/stage plus a small reload-diagnostic ownership guard/token.

## Verification

Minimum targeted Rust verification:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r003 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r010 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
```

Python/oracle verification:

```text
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/unit/test_config_reload_policy.py tests/integration/test_rehash_acceptance.py tests/integration/test_rehash_retirement_edge_cases.py tests/integration/test_rehash_streaming_swap.py tests/integration/reload/test_stale_app_state.py tests/integration/reload/test_diagnostics_contract.py tests/integration/reload/test_reload_diagnostics_assertions.py -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

If repository wrappers such as `rtk` are required in the implementation environment, use the equivalent wrapped commands and record the exact commands in closure evidence.

## Closure criteria

R012 may re-close M8 only when all of the following are demonstrated:

1. every live `routing.wire_negotiation.*` field is consumed by the actual process-owned resolver at startup and after accepted live reload;
2. a rejected/failed reload cannot mutate resolver policy or compatible state;
3. compatible learned/rejected state survives policy-only reload without unbounded growth, while new TTL/cooldown/capacity/concurrency/interval/enabled policy becomes authoritative;
4. the resolver remains one process-owned bounded instance and C003/C012 negotiation/cancellation semantics remain green;
5. caller cancellation cannot strand `reload_in_progress` after the retained transaction terminates;
6. a concurrent `Busy` caller cannot falsely clear or overwrite the active reload's ownership state;
7. reload diagnostics remain bounded, coherent, and secret-free across success/failure/cancellation/shutdown storms;
8. R012 contains failing-before/passing-after evidence for both original defects and the related `Busy` race;
9. R003/R005/R007/R009/R010/R011 and the full Rust/Python migration suites remain green;
10. no new schema, production dependency, M9 surface, or unresolved high/medium M8 correctness/security finding remains.

## Closure and registry transition

Write `migration-rs/closure/runtime-lifecycle/012-status.md` with implementation commit(s), failing-before/passing-after evidence, exact verification commands/results, resource/security review, and any supported differences.

Only accepted R012 closure may:

- mark M8 closed again;
- move R012 from dependency-ready to completed;
- restore M9 eligibility for its own planning/implementation review.

Until then, R011 is historical aggregate closure evidence and M9 remains blocked.
