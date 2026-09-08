# R013 — Wire-Policy Acceptance and Boundary Requalification

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Repository baseline: `a4495488d9071efa22eb7eff6447e6298f85d73d` (R012 implementation and historical re-closure).

Primary class: invariant/corrective

Hard dependencies: accepted R001/R005/R007/R010 runtime contracts, historical R011 aggregate qualification, and historical R012 corrective closure.

## Objective

Correct the residual M8 defects found by post-R012 repository audit and requalify the exact boundary that R012 intended to close before M9 is allowed to begin.

R013 has four bounded objectives:

1. port the Python `WireNegotiationConfig` validation bounds exactly so hostile or accidental finite-but-extreme values fail configuration validation rather than reaching a panicking `Duration` conversion;
2. make shared process wire-policy publication coherent with the R007 durable/runtime/task acceptance transaction so an aborted reload cannot become externally visible to already accepted work;
3. make rollback restore the old policy **and** its bounds immediately; and
4. replace R012's underpowered qualification with real `ReloadService` fault/rejection coverage and an actual Axum inference request through M7 after a live wire-policy reload.

R013 is not an M8 redesign. R001-R010 remain accepted. R011 and R012 remain append-only historical closure evidence, but neither authorizes M9 after this audit. Only an accepted R013 closure may re-close M8 and restore M9 planning eligibility.

## Why R012 closure is not sufficient

R012 fixed the two headline ownership defects: the process resolver now has mutable policy state and reload diagnostics are retained by the owned reload worker. However, repository review found correctness and evidence gaps that the R012 focused suite did not exercise.

### Finding 1 — Rust omits authoritative Python upper bounds

Python's `WireNegotiationConfig` validates:

- `max_concurrent_per_provider`: `1..=8`;
- `min_negotiation_interval_s`: `0..=1800`;
- `rejection_cooldown_s`: `0..=1800`;
- `learned_preference_ttl_s`: `0..=604800`;
- `cache_max_entries`: `1..=65536`.

Rust currently validates the floating-point fields only as finite/non-negative and then converts them with `Duration::from_secs_f64`. A sufficiently large but finite TOML value can therefore pass configuration validation and panic the process during runtime construction/policy staging. That violates both Python parity and the migration's fail-closed configuration invariant.

### Finding 2 — candidate wire policy is published before durable acceptance

The R012 reload path currently commits candidate wire policy before the SQLite transaction commits. Because the resolver is process-owned and shared by active/retiring generations, already accepted work can observe that policy while the reload is still reversible. If the durable commit then fails and policy rolls back, an aborted reload has transiently affected production work.

R013 must restore one coherent acceptance boundary: a candidate policy may be prepared while reversible, but it must not become externally authoritative until the durable acceptance has crossed its irreversible point and admission is still closed.

### Finding 3 — rollback does not immediately restore old bounds

`WireResolverPolicyStage::rollback()` reapplies the old policy but does not necessarily enforce the old capacity/provider-state bounds immediately. If candidate policy temporarily relaxed a bound and state grew before rollback, the restored policy can remain over its old bound until unrelated later activity.

### Finding 4 — R012 did not test the claimed production request path

The R012 closure claims a production request path after live policy reload, but the focused router check uses `/v1/healthz`. Health does not enter M7 inference or exercise wire selection/negotiation. The closure therefore proved shared object identity, not request-visible policy authority.

R013 must add failing-before/passing-after evidence for all four findings.

## Authoritative Python and migration sources

Before editing Rust, inspect current repository evidence rather than relying only on this plan. At minimum use:

- `src/eggpool/models/config.py` — exact `WireNegotiationConfig` bounds;
- `src/eggpool/config_reload_policy.py` — live classification;
- `src/eggpool/runtime_manager.py`;
- `src/eggpool/generation_factory.py`;
- `src/eggpool/reload_transaction.py`;
- `src/eggpool/reload_diagnostics.py`;
- startup/reload wiring in `src/eggpool/app.py`;
- `migration-rs/fixtures/runtime-lifecycle/r001-python-observations.json`;
- `migration-rs/authority/runtime-lifecycle.md`;
- historical R005, R007, R010, R011, and R012 plans/closures/tests.

Relevant Rust sources include:

- `rust/src/config.rs`;
- `rust/src/coordinator/wire_resolver.rs`;
- `rust/src/runtime_lifecycle.rs`;
- `rust/src/reload.rs`;
- `rust/src/server.rs`;
- `rust/tests/runtime_lifecycle_r005.rs`;
- `rust/tests/runtime_lifecycle_r007.rs`;
- `rust/tests/runtime_lifecycle_r010.rs`;
- `rust/tests/runtime_lifecycle_r011.rs`;
- `rust/tests/runtime_lifecycle_r012.rs`.

If current Python tests do not expose a needed boundary deterministically, add a bounded R013 oracle fixture rather than rewriting R001 history.

## Part A — exact fail-closed wire configuration bounds

### A1. Port Python bounds exactly

Rust configuration validation must reject values outside the Python ranges:

| Field | Minimum | Maximum |
|---|---:|---:|
| `routing.wire_negotiation.max_concurrent_per_provider` | 1 | 8 |
| `routing.wire_negotiation.min_negotiation_interval_s` | 0 | 1800 |
| `routing.wire_negotiation.rejection_cooldown_s` | 0 | 1800 |
| `routing.wire_negotiation.learned_preference_ttl_s` | 0 | 604800 |
| `routing.wire_negotiation.cache_max_entries` | 1 | 65536 |

`enabled` remains boolean.

Do not silently clamp invalid user configuration at the config layer. Python rejects these values, so Rust must reject them with the established configuration-validation category.

### A2. Runtime conversion must remain non-panicking independently

Even after exact config validation is added, runtime conversion from seconds to `Duration` must not rely on validation as its only panic barrier. Constructors/test helpers can receive programmatically-created config values that bypass TOML parsing.

Use an explicitly checked/fallible conversion or another bounded non-panicking mapping. Invalid runtime policy input must return a typed construction/staging error or otherwise fail closed; it must never panic the process.

Do not introduce a general numeric-validation framework for this correction.

### A3. Boundary tests

Add exact tests for:

- every documented minimum;
- every documented maximum;
- one value immediately below/above each legal bound where representable;
- extremely large finite values;
- negative values;
- non-finite values through direct Rust object construction where TOML cannot represent them;
- startup construction and live staging with invalid programmatic values do not panic.

The test must demonstrate failing-before behavior for at least one huge finite duration on the R012 baseline.

## Part B — coherent process wire-policy acceptance

### B1. Preserve one process resolver

Keep the R012/M8 ownership decision: one process-owned resolver survives generation swaps and retains structurally compatible learned/rejected state.

Do not solve acceptance by moving the resolver back into each generation or by cloning a second live resolver.

### B2. Prepare versus publish

`stage_wire_resolver_policy()` may validate/prepare candidate policy before the acceptance gate, but preparation must not mutate externally observable resolver behavior.

The shared policy's externally authoritative commit must occur only when:

- the candidate generation is already staged against the expected active generation;
- the DB/config-derived acceptance transaction has successfully crossed the durable commit point;
- task/runtime state has the deterministic post-commit adoption path required by R007; and
- request admission remains closed until the process policy and accepted generation/task state agree.

The exact code ordering may differ from Python, but the observable invariant is mandatory: **a reload that ultimately returns a pre-accept abort/rejection may not affect request-visible wire policy at any point.**

### B3. Post-durable-commit failure semantics

R007 already has fail-closed adoption/compensation semantics for failures after durable mutation becomes irreversible. R013 must explicitly include wire policy in that state machine.

If shutdown or another post-commit failure requires adopting the new durable/runtime state, the matching wire policy must also be adopted before admission reopens. It is invalid to leave new DB/runtime/task authority paired with old resolver policy, or vice versa.

Do not add a distributed transaction abstraction. Keep the existing short local acceptance window and typed fail-closed outcomes.

### B4. Old in-flight work

Freeze and test the intended shared-policy semantics:

- an old-generation request already accepted before a **successful** live policy publication may observe the newly accepted process policy when it later reaches shared wire resolution, because the resolver is process-owned;
- an old-generation request must **never** observe a policy from a reload that later aborts before accepted publication;
- request generation graph/provider pool/body/config ownership remains generation-pinned exactly as R003/R010 require.

If current Python behavior materially contradicts this process-policy rule, stop and update the plan/ADR rather than inventing a silent normalization.

## Part C — rollback restores bounds immediately

Any policy-stage rollback or Drop-based rollback must restore:

- the exact old policy;
- the old cache capacity;
- provider-state/gate bounds;
- metric-label bounds where policy/config affects them;
- semaphore/concurrency target;
- TTL/cooldown authority.

After rollback returns, bounded snapshots must already satisfy the restored old limits. Do not depend on a future `resolve`, maintenance tick, or unrelated request to trim state.

Required regression:

1. start with a small old bound;
2. stage a larger candidate policy;
3. create enough candidate-era state to exceed the old bound through the supported stage/test hook;
4. roll back/Drop the stage;
5. immediately assert old policy and old bounds without another resolver operation.

Also test rollback while concurrency permits are outstanding; accepted work may complete, but new acquisition must converge to the restored limit without deadlock or permit underflow.

## Part D — full ReloadService rejection/fault isolation matrix

R013 must exercise wire-policy authority through the real `ReloadService`, not predominantly through direct `WireResolverPolicyStage` helpers.

For each case below, capture before/after:

- active generation id and publication epoch;
- process resolver policy snapshot;
- learned/rejected state counts or a deterministic structural probe;
- task specs/transition count;
- relevant DB provider/account rows where mutated by the candidate;
- admission-gate state;
- reload diagnostic terminal state.

Cases:

- no-op;
- restart-required;
- mixed live + restart-required;
- invalid TOML;
- Python-bound-invalid wire setting;
- stale expected digest;
- candidate construction failure;
- task preflight failure;
- task commit failure;
- persistence begin failure;
- persistence apply failure;
- persistence commit failure;
- generation-stage/retirement-backlog rejection;
- caller cancellation before observed completion;
- shutdown racing with acceptance;
- accepted live wire-policy reload.

Use existing narrow R007 test fault hooks or add similarly scoped test-only hooks. Do not add production fault-injection infrastructure.

For every pre-accept rejection/abort, the old DB/runtime/task/wire-policy authority must remain coherent and externally unchanged.

## Part E — actual Axum/M7 inference qualification

R013 must replace `/healthz` as wire-policy evidence with a deterministic local provider and a real inference request.

At minimum:

1. boot the Rust Axum router/runtime with a deterministic local provider and a wire configuration whose behavior can be observed without paid/network services;
2. POST a valid request to `/v1/chat/completions` (or another qualified public inference surface) and establish baseline provider/wire behavior;
3. perform an accepted **live** `routing.wire_negotiation.*` reload through `ReloadService`;
4. issue a new inference request and prove the new shared resolver policy is used by the M7 coordinator/provider attempt path;
5. perform an intentionally aborted/rejected policy reload while a request is blocked at a deterministic stage and prove no request observes the rejected policy;
6. assert finalization/claim/reservation convergence and no resolver flight/gate leak.

Acceptable observations include deterministic candidate ordering, negotiation enabled/disabled behavior, concurrency gating, learned/cooldown behavior, or another direct resolver effect. Merely inspecting `policy_snapshot()` is insufficient for this test.

No live paid provider is allowed.

## Part F — retain and strengthen reload-diagnostic correction

The R012 retained diagnostic guard is directionally correct and should not be redesigned unless tests expose another bug.

Requalify:

- caller drops the public reload future while owned worker succeeds;
- caller drops while owned worker fails;
- second concurrent caller returns `Busy` without clearing the first transaction's `reload_in_progress` state;
- cancellation/Busy storm followed by a normal reload;
- shutdown races with the retained worker;
- exactly one terminal diagnostic accounting event per owned transaction;
- terminal `reload_in_progress == false` after convergence;
- counters/history remain bounded;
- all diagnostic projections remain secret-free.

A `Busy` caller that never owns a reload transaction must not impersonate or finish another transaction's diagnostic guard.

## Part G — planning/governance repair

The current `migration-rs/subsystems/runtime-lifecycle-roadmap.md` was accidentally replaced with registry content during the R012 planning/closure sequence. Restore it to an actual M8 subsystem roadmap while registering R013.

Do not delete or rewrite historical R011/R012 closure records. The registry and roadmap should describe them as historical closure evidence superseded by this post-close audit.

## Non-goals

R013 must not:

- implement any M9 CLI/control/daemon/update/backup/deploy surface;
- redesign M7 retry/finalization semantics;
- move the process resolver into generations;
- discard all learned/rejected state on every live policy change;
- create a new DB schema/migration;
- add a second scheduler or second HTTP client stack;
- introduce actor/workflow/transaction frameworks;
- broaden CI/platform matrices beyond deterministic local qualification.

No new Cargo dependency is expected. Any proposed runtime dependency requires explicit justification in the closure record.

## Required tests

Create a focused `rust/tests/runtime_lifecycle_r013.rs` (or equivalently scoped files) covering at least:

### Config safety

- exact Python lower/upper bounds;
- above/below-bound rejection;
- huge finite duration does not panic;
- direct non-finite programmatic policy fails closed.

### Acceptance coherence

- aborted pre-commit reload never exposes candidate policy;
- persistence commit failure never exposes candidate policy;
- successful durable commit + accepted publication exposes candidate policy exactly once;
- shutdown/post-commit adoption cannot create new-runtime/old-policy or old-runtime/new-policy mixing.

### Rollback bounds

- candidate-expanded cache/provider state is immediately trimmed to restored old bounds;
- concurrency rollback converges safely with outstanding permits;
- repeated commit/rollback cycles remain bounded.

### Reload fault matrix

- all Part D rejection/fault cases preserve old authority;
- admission gate always reopens or shutdown owns it;
- no candidate/provider pool/task/resolver leak.

### Real inference

- deterministic local Axum + M7 request observes accepted new policy;
- rejected policy is never request-visible;
- finite or streaming terminal ownership still converges.

### Diagnostics

- cancel-success, cancel-failure, Busy overlap, storm recovery, shutdown race;
- exactly-once terminal accounting;
- bounded secret-free diagnostics.

## Regression suites

R013 must rerun the closed boundaries it can affect:

- R003 publication/leases;
- R005 config policy;
- R007 transactional reload;
- R009 shutdown;
- R010 authority/diagnostics;
- R011 integrated M8 qualification;
- R012 wire-policy/diagnostic correction;
- C003/C012/C013 wire-resolution qualification as applicable.

Do not weaken an older test to make R013 pass.

## Verification

Run at minimum:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r013 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r010 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/unit/test_config_reload_policy.py tests/integration/test_rehash_acceptance.py tests/integration/test_rehash_retirement_edge_cases.py tests/integration/reload/ -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

Adjust exact Python file paths only for current repository layout; record the exact commands/results in closure.

## Closure evidence

Write `migration-rs/closure/runtime-lifecycle/013-status.md` containing:

- implementation commit(s);
- exact Python-bound parity table and Rust validation locations;
- failing-before/passing-after evidence for huge finite duration;
- acceptance-order diagram before/after;
- fault/rejection matrix results;
- immediate rollback-bound evidence;
- real Axum/M7 inference reload evidence;
- reload-diagnostic cancellation/Busy evidence;
- aggregate Rust/Python verification counts;
- dependency/schema/security review;
- unresolved findings;
- registry transition.

## Acceptance criteria

R013 closes only when all of the following are true:

- Rust wire-negotiation validation exactly matches Python's documented bounds;
- no legal config/runtime construction path can panic on wire duration conversion;
- candidate wire policy is not externally visible before coherent durable acceptance;
- every aborted/rejected pre-accept reload leaves old resolver policy/state authoritative throughout;
- post-commit adoption cannot mix DB/runtime/task/wire-policy generations;
- rollback immediately restores old policy **and** old bounds;
- a real public inference request demonstrates accepted live wire-policy authority;
- rejected policy is proven non-observable by a real inference path;
- R012 retained reload diagnostics converge under cancellation, Busy overlap, failure and shutdown;
- all affected M8/M7 regression suites remain green;
- no new schema, architecture framework, M9 implementation, or unjustified dependency is introduced;
- no unresolved high/medium M8 correctness, resource, security, compatibility or lifecycle finding remains.

Only then may the closure mark M8 re-closed and make M9 eligible for its separate planning/implementation review.

## Handoff

R013 is the sole dependency-ready M8 plan. M9 remains blocked. After accepted R013 closure, update the registry/roadmaps to re-close M8 and make M9 eligible for planning; do **not** auto-create or auto-promote M9 implementation work from the R013 implementation commit.