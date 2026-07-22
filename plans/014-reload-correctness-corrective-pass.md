# Reload Correctness Corrective Pass

Date: 2026-07-22
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Supersedes closure claims in: `plans/007-phase-06-transactional-rehash.md`, `plans/008-phase-07-active-generation-state-authority.md`, `plans/011-phase-10-control-plane-and-xdg-hardening.md`, `plans/012-phase-11-reload-diagnostics.md`, and `plans/013-phase-12-ci-soak-and-performance-closure.md` where the acceptance criteria below remain unmet.

## Objective

Close the remaining correctness and hardening defects discovered after implementation of the twelve-phase reload roadmap.

The repository now has the right high-level primitives: atomic reload admission, generation leases, asynchronous retirement, explicit candidate ownership, a shared generation factory, a reload state machine, cached readiness probing, canonical diagnostics, and partitioned CI. This corrective pass must preserve those gains while repairing the places where the implementation still violates the roadmap's core invariants.

The target end state is:

- a reload failure never leaves SQLite provider/account state ahead of the active runtime;
- candidate preparation has no externally visible process-owned side effects;
- one request uses one generation's complete dependency graph for its entire lifetime;
- every post-publication operation is represented by the transaction state machine and has a completion or compensation rule;
- control responses report the actual reload and retirement outcome;
- the Unix control socket fails closed, validates its protocol strictly, honors XDG paths, and cannot unlink an active peer's socket;
- roadmap-relevant tests are strict, non-duplicated, and prove the intended invariants rather than documenting known defects.

## Why this pass is required

The current implementation still has several production-blocking gaps.

1. `_apply_persistence_delta()` commits its SQLite transaction before `_publish_generation()` runs. A publication failure therefore leaves candidate providers/accounts persisted while the old runtime remains active.
2. `tests/integration/reload/test_persistence_publication_split.py` explicitly asserts this mixed-state behavior instead of enforcing all-or-nothing semantics.
3. `RuntimeGenerationFactory.prepare()` reconfigures process-owned routing-trace state during candidate preparation, before commit eligibility is known.
4. The proxy request path leases a coordinator and span recorder from one generation, then reads config, catalog, transcoder policy, compression policy, and tuning state from `app.state`, allowing old/new generation mixing during a reload.
5. `_publish_generation()` performs the runtime swap, ownership transfer, and compatibility mirror update inside one exception wrapper. A failure after the swap but before the transaction records `RUNTIME_PUBLISHED` can leave the new runtime active while the transaction reports a publication failure.
6. Cancellation at `on_publish_started` can produce a `TransactionStateError` because `COMMIT_STARTED` is not a cleanly abortable pre-publication state.
7. The control handler still maps `retirement_pending` to `result.ok`, losing the actual retirement state derived by `ReloadManager`.
8. The control server assumes decoded JSON is a mapping, accepts unknown commands, returns handler exception text, treats permission restriction failure as warning-only, and uses a hard-coded state directory.
9. Stale-socket cleanup unlinks any socket at the configured path without first proving it is stale. A second process can detach a live server's pathname and bind a replacement socket.
10. CI still executes reload integration tests through both the general unit/integration selection and the dedicated reload job, while roadmap-relevant xfails/skips remain allowlisted.

## Scope and priority

This is a corrective closure pass, not a new roadmap. Work must proceed in dependency order:

1. strict failing tests and transaction-state corrections;
2. side-effect-free preparation and prepared transitions;
3. atomic persistence/publication commit;
4. generation-coherent request handling;
5. control-result and diagnostics integration;
6. control socket and XDG hardening;
7. CI and closure validation.

Do not implement the control-plane polish first while the transaction can still expose mixed durable/runtime state.

## Non-goals

- Do not replace FastAPI, Granian, aiosqlite, or the runtime-generation architecture.
- Do not add a second database or an external transaction coordinator.
- Do not turn every background service into a generation-owned service solely to avoid defining transitions.
- Do not remove generation leases or asynchronous retirement.
- Do not broaden live reload to fields currently classified as restart-required.
- Do not weaken SQLite durability, request persistence, or dispatch-writer backpressure.
- Do not use sleeps as the primary concurrency test mechanism.
- Do not claim closure based only on test counts or documentation status fields.

## Primary implementation seams

Expected production files:

- `src/eggpool/control/reload_manager.py`
- `src/eggpool/reload_transaction.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/generation_factory.py`
- `src/eggpool/app.py`
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/control/server.py`
- `src/eggpool/runtime_paths.py`
- `src/eggpool/reload_diagnostics.py`
- `src/eggpool/runtime_tasks.py`
- `.github/workflows/ci.yml`
- `.github/workflows/extended-soak.yml`

Expected test files:

- `tests/integration/reload/test_persistence_publication_split.py`
- `tests/integration/reload/test_reload_atomicity.py`
- `tests/integration/reload/test_reload_fault_matrix.py`
- `tests/integration/reload/test_process_mutation_timing.py`
- `tests/integration/reload/test_stale_app_state.py`
- `tests/integration/reload/test_diagnostics_contract.py`
- `tests/integration/reload/test_lease_acquisition_fallback.py`
- `tests/integration/test_rehash_d3_acceptance.py`
- `tests/integration/test_rehash_d3_operator_workflow.py`
- new control-server protocol/security tests as described below.

---

# Workstream A — Convert known defects into strict gates

## A1. Invert the persistence/publication split test

Replace `test_reconcile_then_publish_failure_leaves_db_split` with a strict invariant test whose required post-state equals the complete pre-state when publication fails.

The test must compare:

- active generation ID and digest;
- generation config values;
- persisted provider IDs;
- persisted account names and enabled state;
- process task specs and version;
- routing-trace writer configuration;
- compatibility/effective-state digest;
- candidate ownership state;
- active and retiring generation counts;
- closeable resource counts.

The desired assertion is not merely “generation unchanged.” It is “no externally observable state changed.”

## A2. Add the post-swap bookkeeping failure test

Inject failures separately after each of these operations:

1. runtime slot swap;
2. candidate ownership transfer;
3. transaction `mark_runtime_published()`;
4. compatibility mirror/effective-state update;
5. retirement scheduling.

The transaction must never report a pre-publication failure after the active slot has changed. The state machine and diagnostics must agree with the runtime manager's actual active generation.

## A3. Make cancellation barriers strict

Replace the documented `TransactionStateError` at `on_publish_started` with a strict outcome:

- cancellation before the runtime publication linearization point aborts to the complete old state;
- cancellation after the linearization point completes or compensates the candidate commit under bounded shielding;
- the admission claim is always released;
- the transaction completion event is always set;
- the candidate is either aborted or transferred, never left prepared.

Update `ReloadTransaction` transitions so `COMMIT_STARTED` can abort when publication has not occurred. Do not use `txn.is_committing` as a proxy for `publication_occurred`; represent publication explicitly.

## A4. Remove tests that simulate the fix instead of exercising production wiring

Tests for compatibility mirrors and active-generation consumers must invoke the real reload manager with a real app/state object. Do not manually re-point fake mirrors after reload and then assert that the manual update worked.

## Acceptance criteria

- All known-defect tests fail on the pre-corrective implementation.
- No roadmap-critical invariant is represented only by a documentation test, non-strict xfail, or skip.
- Fault tests assert complete snapshots, not isolated generation IDs.
- Cancellation at every observer stage has a documented strict terminal state.

---

# Workstream B — Make candidate preparation side-effect free

## B1. Define the mutation boundary

`RuntimeGenerationFactory.prepare()` may create and initialize candidate-owned objects and read process-owned state. It must not mutate any process-owned service that is visible to the active generation.

Forbidden during preparation:

- `routing_trace_writer.configure(...)`;
- mutation of a process-wide routing-trace guard singleton;
- process supervisor task-spec application;
- replacement of `app.state` mirrors;
- effective config/digest mutation;
- dispatch-writer start/stop/reconfiguration;
- process metrics configuration changes.

## B2. Replace singleton guard mutation

Prefer a generation-owned `RoutingTraceGuard` instance rather than `get_routing_trace_guard()` returning a mutable process-wide singleton.

If a singleton must remain temporarily, preparation must produce an immutable `RoutingTraceGuardConfig` and the commit path must apply it through a reversible process transition.

## B3. Add explicit prepared transitions

Extend `ProcessTransitionPlan` with typed transitions for every process-owned change:

- `TaskSpecTransition`;
- `RoutingTraceWriterTransition`;
- `RoutingTraceGuardTransition` if the guard remains process-owned;
- `DispatchWriterTransition` for any future live writer settings;
- `EffectiveStateTransition` or equivalent compatibility-state update.

Each transition must implement:

- `preflight()` — validate inputs and capture previous state without mutation;
- `apply()` — perform the bounded change;
- `rollback()` — restore the captured previous state;
- `finalize()` — release transition snapshots after successful commit;
- idempotence rules for repeated apply/rollback attempts;
- structured diagnostics without secrets.

## B4. Prove no mutation at every preparation barrier

At `on_candidate_complete` and `on_reconcile_prepared`, compare process-owned state against the pre-reload snapshot. Inject a failure immediately afterward and require total equality.

## Acceptance criteria

- Candidate preparation changes no process-owned writer, guard, supervisor, effective config, or compatibility mirror.
- Every live process-owned change appears in `ProcessTransitionPlan`.
- Transition preflight is read-only.
- Apply/rollback behavior is covered for each transition type.
- Repeated failed preparations do not drift writer configuration or task specs.

---

# Workstream C — Establish a real transaction linearization point

## C1. Separate publication primitives

Refactor `RuntimeManager.install_candidate()` into a prepared-swap protocol or equivalent API that makes the boundaries explicit.

A recommended shape:

```python
pending = await runtime_manager.prepare_swap(
    generation,
    expected_active_generation_id=old_generation_id,
)

# No active pointer change and no retirement yet.
await pending.commit_publication()
# Active pointer changes exactly once here.

await pending.finalize_retirement()
# Old generation retirement is scheduled only after the complete commit.
```

The exact names may differ, but the implementation must distinguish:

- candidate slot allocation/prevalidation;
- active pointer swap;
- rollback eligibility;
- ownership transfer;
- retirement scheduling.

Do not combine pointer swap, candidate transfer, mirror mutation, and retirement scheduling under one broad exception wrapper.

## C2. Keep the old slot recoverable until commit finalization

The old generation must not begin retirement until persistence, process transitions, and effective-state publication have reached the successful commit state.

Before finalization:

- the old slot remains available for rollback;
- the candidate remains owned by the reload candidate container or a pending-swap owner;
- retirement tasks are not spawned;
- no resource is closed.

## C3. Redesign persistence commit ordering

The corrective implementation must guarantee that a publication failure leaves provider/account persistence unchanged.

Acceptable designs:

### Preferred design — one shielded commit coroutine with reversible state

1. Open a SQLite transaction.
2. Apply the prepared persistence delta without committing.
3. Apply reversible process transitions while retaining rollback snapshots.
4. Revalidate the active generation and shutdown state.
5. Execute the runtime pointer swap as the transaction linearization point.
6. Mark `RUNTIME_PUBLISHED` immediately after the pointer swap, before any other fallible operation.
7. Apply a prebuilt effective-state snapshot through a bounded, non-awaiting assignment path.
8. Commit SQLite.
9. Finalize transitions and candidate ownership.
10. Schedule retirement.

If any operation before SQLite commit fails:

- roll back the SQLite transaction;
- roll back applied process transitions in reverse order;
- restore the old runtime pointer if publication occurred;
- abort or retire the candidate according to whether it received leases;
- restore the old effective-state snapshot.

The runtime manager may temporarily gate new lease acquisition during the very short pointer/DB commit window if required to guarantee rollback without exposing the candidate.

### Alternative design — durable compensation

A committed persistence delta before publication is acceptable only if the implementation captures an exact inverse delta and synchronously restores it when publication fails. The inverse operation must be tested for provider additions, removals, account additions, removals, enablement changes, and foreign-key relationships.

This alternative must also add a durable reload-intent record so process death between persistence commit and compensation can be detected and repaired at startup.

Do not retain the current “the next reload will resync it” behavior.

## C4. Make publication bookkeeping non-ambiguous

Introduce explicit fields/state:

- `publication_attempted`;
- `publication_occurred`;
- `active_generation_before`;
- `active_generation_after`;
- `persistence_committed`;
- `process_transitions_applied`;
- `effective_state_updated`;
- `retirement_scheduled`.

`mark_runtime_published()` must occur immediately after the actual active pointer swap. Diagnostics must derive from these facts, not infer them from `txn.is_committing` or exception class.

## C5. Handle post-publication failures precisely

Post-publication failure handling must choose one policy and test it:

- rollback to the old generation before any candidate lease is granted; or
- complete the new generation commit and report compensated success/degraded success.

A failed mirror assignment must not be classified as a failed publication if the new generation is already active.

If compensation fails, readiness must fail closed and diagnostics must expose:

- active generation;
- persistence digest/state;
- failed transition;
- compensation attempt count;
- operator action required.

## Acceptance criteria

- Injected publication failure leaves runtime, SQLite, tasks, writers, and effective state identical to pre-reload state.
- No test asserts or tolerates DB-ahead-of-runtime behavior.
- The transaction state always matches the runtime manager's active generation.
- Retirement starts only after commit finalization.
- Cancellation cannot interrupt the shielded linearization sequence midway.
- Repeated compensation is idempotent.
- A process-transition failure is either fully rolled back or completed; no mixed task-spec/runtime state remains.

---

# Workstream D — Enforce one-generation request coherence

## D1. Pass the leased generation into the complete request path

Change `_handle_proxy_request_inner()` to receive the leased `RuntimeGeneration`, not only the coordinator and span recorder.

Resolve all generation-owned dependencies from that object:

- `config`;
- `registry` and provider IDs;
- `catalog` and catalog cache;
- `coordinator`;
- `transcoder_policy`;
- `compression_policy`;
- `cache_config`;
- `compression_tuning_registry`;
- `dispatch_span_recorder`;
- `local_pre_upstream_recorder` where needed;
- stream diagnostics and other per-generation request services.

Normal production requests must not read these objects from `request.app.state` after acquiring a generation lease.

## D2. Use immutable request state where available

Use `generation.immutable_request_state.provider_ids` instead of rebuilding provider sets from `app.state.config` on each request.

Ensure immutable request state is complete for the parsing and header-filter decisions made before dispatch.

## D3. Restrict compatibility mirrors

Compatibility mirrors may remain temporarily for synchronous dashboard or legacy test paths, but:

- new production code must not add generation-owned `app.state` reads;
- awaited handlers must use a lease;
- short synchronous diagnostics may use `snapshot_active_values()` only within the same non-awaiting call frame;
- `app.state.config` and `app.state.config_digest` must either be updated as compatibility mirrors after commit or replaced by explicit effective-config accessors.

Add a static audit or AST-based test that rejects new direct reads of the known generation-owned mirror names in production request handlers.

## D4. Add overlap tests

Start an old-generation request and pause it after body parsing. Publish a new generation with different:

- provider membership;
- catalog/context limit;
- transcoder loss policy;
- compression mode/policy;
- model override.

Resume the old request and prove that every decision uses the old generation. Then issue a new request and prove that every decision uses the new generation.

## Acceptance criteria

- A leased request never combines an old coordinator with a new catalog or policy.
- Provider parsing uses the leased generation's provider set.
- Context-limit and transcode preflight use the leased catalog and policy.
- Compression policy resolution uses the leased generation.
- New requests immediately use the new generation after publication.
- Old streams complete against their original generation without accessing retired resources.

---

# Workstream E — Correct control results and reload diagnostics

## E1. Carry actual retirement status through the wire result

Extend the canonical reload result returned by `ReloadManager.reload()` with:

- `retirement_pending`;
- `retiring_generation_id`;
- optional retirement state or task count where protocol-compatible.

The control handler must copy the canonical field. It must not derive retirement from `result.ok`.

Required examples:

- semantic no-op: `ok=true`, `retirement_pending=false`;
- ignored-only outcome: `ok=true`, `retirement_pending=false`;
- first generation install with no old generation: `false`;
- successful swap with old stream draining: `true`;
- successful swap whose old generation already closed: `false`;
- failed reload: `false` unless a documented compensated outcome actually has a retiring generation.

## E2. Align terminal stage and result category

Do not report `RETIREMENT` merely because retirement was scheduled. The terminal stage should represent the last completed transaction stage, with retirement status represented separately.

Review CLI formatting and exit-code mapping for compensated success, compensation failure, busy, restart-required, no-op, and ignored-only outcomes.

## E3. Remove string-based error classification

Replace checks such as searching exception text for “digest mismatch” with typed exceptions or explicit diagnostic codes.

Every failure path should provide:

- stable error code;
- exception class for internal diagnostics;
- bounded redacted operator message;
- actual transaction stage;
- publication and persistence facts.

## E4. Make the finalizer truly singular

All terminal paths, including busy rejection where practical, validation mismatch, restart-required, no-op, ignored-only, cancellation, compensated completion, and compensation failure, should pass through one finalization routine or a small explicitly documented pre-admission finalizer.

Avoid hand-constructing `ReloadCounters` repeatedly. Add immutable increment helpers or a mutable internal counter accumulator whose snapshot is immutable.

## Acceptance criteria

- Control responses and runtime diagnostics agree on retirement state.
- No-op and ignored-only results never claim retirement pending.
- Failure stage comes from transaction facts, not exception-message parsing.
- Counter arithmetic has one implementation path and is covered by matrix tests.
- Dashboard, runtime API, CLI, and operational events report the same category and generation IDs.

---

# Workstream F — Complete control socket and XDG hardening

## F1. Strictly validate decoded JSON

After decoding, require a JSON object before accessing fields.

Validate:

- `protocol_version` is the supported integer;
- `request_id` is a non-empty bounded string, preferably UUID-shaped;
- `command` is exactly `reload_config` for protocol version 1;
- `validated_digest`, when present, is exactly 64 hexadecimal characters;
- line size remains bounded;
- unknown commands receive a protocol error and never reach the reload handler.

Do not return raw parser or handler exception strings to the client. Log full internal details locally and return bounded stable messages/codes.

## F2. Fail closed on permissions

Before accepting commands:

1. create the parent directory with mode `0700`;
2. verify directory ownership and mode;
3. bind the socket without following symlinks;
4. set socket mode `0600`;
5. verify the resulting mode and owner;
6. if any enforcement step fails, close the server, unlink the socket created by this process, and raise `ControlServerError`.

Permission failure must never leave a listening server active.

## F3. Do not unlink a live control socket

Replace unconditional socket removal with a liveness protocol.

Recommended behavior:

- reject symlinks unless they are known process-owned stale artifacts;
- when a socket exists, attempt a short protocol probe or connection;
- if a valid server responds, fail startup with “already running” rather than unlinking;
- if connection is refused and ownership/mode are safe, remove the stale socket;
- use a PID/lock file or atomic owner marker to reduce races between two starting processes;
- after bind, verify the path still refers to this server's socket inode where supported.

Add a two-process test proving that a second server cannot detach or replace the first server's pathname.

## F4. Implement XDG path semantics

Use:

- `XDG_RUNTIME_DIR/eggpool/eggpool.sock` for the ephemeral control socket when available;
- an owner-only UID-scoped runtime fallback when `XDG_RUNTIME_DIR` is unavailable;
- `XDG_STATE_HOME/eggpool` for persistent state/log files when available;
- `~/.local/state/eggpool` only as the XDG state fallback;
- path-length-safe fallback logic for Unix-domain socket limits.

`state_dir()` must honor `XDG_STATE_HOME`. Introduce a separate `runtime_dir()` rather than using persistent state paths for sockets.

The CLI and server must call the same path resolver.

## F5. Optional Linux peer validation

Where `SO_PEERCRED` is available, reject a peer UID that differs from the server UID. Keep the feature portable by treating unsupported platforms explicitly rather than failing all control traffic.

## Acceptance criteria

- Non-object JSON returns a parse/protocol error, not an internal server error.
- Unknown commands are rejected before the handler.
- Invalid digests are rejected before config validation/reload.
- Permission restriction failure prevents server startup.
- A second process cannot unlink or replace an active socket.
- Two distinct XDG runtime directories produce isolated sockets.
- `XDG_STATE_HOME` isolation test is strict and passing.
- CLI and server resolve identical control paths.
- No raw exception text crosses the control protocol.

---

# Workstream G — CI and closure cleanup

## G1. Eliminate duplicate suite execution

Mark reload integration tests with `@pytest.mark.reload` at collection scope or exclude `tests/integration/reload/` from the general correctness job.

Required property: each logical test is executed once per intended Python version, not once in the general job and again in the reload job.

A practical split:

- unit/core integration job excludes `reload`, `performance`, `soak`, `extended_soak`, `live`, and `network` as appropriate;
- reload-control job selects `reload` and runs on Python 3.11/3.12;
- process-level live rehash tests run in one explicit job/profile.

## G2. Remove roadmap-critical exemptions

Convert these to strict tests:

- subprocess concurrent reload/busy behavior using a deterministic server-side barrier or test-only hold command;
- retirement timeout with a test-configurable short timeout;
- XDG state/runtime isolation;
- cancellation at `on_publish_started`.

Do not retain them in the skip/xfail allowlist after their implementation lands.

## G3. Strengthen Phase 12 evidence

Replace “implementation complete” with measured evidence generated by CI artifacts.

Require:

- exact workflow run and commit SHA;
- pass/fail counts per partition;
- resource plateau measurements;
- consistency audit result;
- remaining exemptions, which must be zero for this corrective scope;
- fixed-environment performance comparison before and after the corrective pass.

## G4. Add transaction consistency audit checks

Extend consistency audit or runtime diagnostics to detect:

- provider/account rows inconsistent with active effective config;
- active generation digest inconsistent with effective config digest;
- process task specs inconsistent with active config;
- routing-trace writer config inconsistent with active config;
- compensation-failed transactions;
- retirement task/slot registry disagreement.

## Acceptance criteria

- No reload test is unintentionally run in two CI jobs.
- Python 3.11 and 3.12 reload jobs pass.
- No roadmap-critical skip or non-strict xfail remains.
- CI fails when durable provider/account state differs from active committed config.
- The final evidence document references a verified green workflow run.

---

# Required implementation order

## Milestone 1 — Strict evidence and state-machine repair

Implement Workstream A first.

Deliverables:

- inverted split-state test;
- strict cancellation tests;
- real app mirror test;
- post-swap bookkeeping failure tests;
- `ReloadTransaction` transition fixes.

Exit gate: all tests fail for the intended reasons before production fixes are introduced.

## Milestone 2 — Side-effect-free preparation

Implement Workstream B.

Deliverables:

- prepared writer/guard transitions;
- no process mutation in `RuntimeGenerationFactory.prepare()`;
- transition rollback tests.

Exit gate: snapshots at every pre-commit barrier equal the pre-reload process state.

## Milestone 3 — Atomic commit correction

Implement Workstream C.

Deliverables:

- prepared runtime swap API;
- corrected persistence/publication ordering or exact durable compensation;
- explicit publication facts;
- delayed retirement;
- cancellation shielding at the linearization point.

Exit gate: every injected failure produces complete old state or complete new state; no mixed state exists.

## Milestone 4 — Request coherence

Implement Workstream D.

Deliverables:

- leased generation passed through proxy request handling;
- generation-owned `app.state` reads removed from the production proxy path;
- overlap tests.

Exit gate: an old in-flight request cannot observe any new-generation policy or catalog object.

## Milestone 5 — Control semantics and hardening

Implement Workstreams E and F.

Deliverables:

- actual retirement status on the wire;
- typed failure codes;
- strict protocol parser;
- fail-closed permissions;
- live-socket protection;
- XDG runtime/state paths.

Exit gate: control-plane protocol/security and XDG tests are strict and passing.

## Milestone 6 — Closure validation

Implement Workstream G.

Deliverables:

- non-duplicated CI partitions;
- zero corrective-scope xfails/skips;
- consistency audits;
- verified Phase 12 replacement evidence.

Exit gate: green CI on current `main`, short soak, resource plateau, and fault matrix.

---

# Test matrix

At minimum, add or update tests for the following scenarios.

## Pre-publication failures

- candidate DNS backend construction failure;
- client pool construction failure;
- outbound client acquisition failure;
- backoff hydration failure;
- catalog cached-load failure;
- task transition preflight failure;
- routing-trace transition preflight failure;
- persistence delta application failure;
- active-generation revalidation failure;
- cancellation at every observer barrier before pointer swap.

Expected result: complete old state, candidate aborted, no retirement task.

## Publication boundary failures

- injected failure immediately before pointer swap;
- failure immediately after pointer swap but before transaction state update;
- ownership-transfer failure;
- effective-state assignment failure;
- SQLite commit failure;
- retirement scheduling failure;
- cancellation during each boundary.

Expected result: documented complete rollback or completed compensated commit, never mixed state.

## Process transition failures

- task-spec apply fails once, retry succeeds;
- task-spec apply fails persistently;
- routing-trace writer apply fails;
- rollback itself fails;
- multiple transitions apply and a later transition fails.

Expected result: reverse-order rollback or explicit compensation-failed readiness state with accurate diagnostics.

## Request overlap

- old non-streaming request paused during reload;
- old streaming request continues during reload;
- provider removed in new generation;
- context limit changed;
- transcode loss policy changed;
- compression policy changed;
- new request starts immediately after publication.

Expected result: per-request generation coherence.

## Control protocol/security

- empty request;
- oversized request;
- malformed JSON;
- JSON list/string/number/null;
- missing/invalid protocol version;
- missing/oversized/invalid request ID;
- missing/unknown command;
- malformed digest;
- chmod failure;
- unsafe parent mode/owner;
- active socket already present;
- dangling symlink;
- symlink to active socket;
- separate XDG runtime/state environments;
- peer UID mismatch where supported.

## Long-running validation

- at least 100 successful generation-changing reloads;
- at least 100 injected failures covering all transaction stages;
- concurrent streams and dispatch-writer load;
- readiness and dashboard polling;
- repeated control reconnects;
- no positive late-window slope in tasks, descriptors, retiring generations, or client resources;
- active config, persistence, task specs, and writer config agree after every quiescent checkpoint.

---

# Performance constraints

Correctness changes must not reintroduce the original long-running dispatch degradation.

Measure before and after:

- request dispatch overhead p50/p95/p99;
- local pre-upstream p50/p95/p99;
- SQLite lock wait p50/p95/p99;
- reload prepare/commit/total latency;
- active-pointer commit-window duration;
- readiness latency;
- dispatch-writer queue wait and batch size;
- event-loop lag;
- RSS and descriptor plateau.

Guidelines:

- keep the transaction's shielded commit window narrow;
- do not perform remote fetches inside the commit window;
- avoid long process-transition awaits by prebuilding replacement state;
- do not block normal requests for candidate preparation;
- if lease acquisition must be gated during final commit, measure and bound the gate duration;
- keep asynchronous old-generation retirement outside the commit latency.

Any new hard performance threshold must be based on repeated measurements in a documented environment rather than a single CI runner sample.

---

# Documentation updates

After implementation:

- update `architecture/README.md` with the final reload linearization point and ownership-transfer sequence;
- update `.opencode/skills/architecture/SKILL.md` with generation-coherent request rules;
- update `.opencode/skills/development/SKILL.md` with strict reload/control test commands;
- update control protocol documentation with validation and path rules;
- replace or amend `docs/phase12-handoff-evidence.md` with verified corrective-pass evidence;
- mark the affected phase plan statuses accurately rather than leaving contradictory “complete” labels.

Document the final ordering as a sequence diagram or explicit numbered transaction, including rollback and cancellation paths.

---

# Definition of done

This corrective pass is complete only when all of the following are true:

1. A publication failure leaves provider/account persistence identical to the pre-reload state.
2. No passing test documents DB-ahead-of-runtime behavior as acceptable.
3. Candidate preparation mutates no process-owned live service.
4. Every process-owned live change is represented by a preflighted reversible transition.
5. The transaction records runtime publication immediately at the actual pointer swap.
6. Cancellation before publication aborts cleanly; cancellation after publication completes or compensates without `TransactionStateError`.
7. Old-generation retirement begins only after complete commit finalization.
8. One leased request uses one generation's config, catalog, policies, coordinator, and telemetry for its full lifetime.
9. Production proxy handling no longer reads generation-owned services from `app.state` after lease acquisition.
10. Control responses report actual retirement state rather than deriving it from success.
11. Non-object JSON, unknown commands, and invalid digests are rejected as protocol errors.
12. Control socket permission enforcement is fail-closed.
13. A second process cannot unlink or replace an active control socket.
14. Control sockets honor `XDG_RUNTIME_DIR`; persistent state honors `XDG_STATE_HOME`.
15. The XDG, retirement-timeout, concurrent-busy, and publication-cancellation tests are strict and passing.
16. Reload integration tests are not duplicated across CI partitions.
17. The consistency audit proves active config, SQLite provider/account state, process task specs, and process writer config agree.
18. The mixed reload/stream/dispatch soak returns resources to documented plateaus.
19. A verified green GitHub Actions run exists for the final commit on every required partition.
20. The repository's phase/evidence documentation no longer claims closure while any item above is unresolved.
