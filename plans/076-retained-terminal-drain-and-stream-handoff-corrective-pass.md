# Plan 076 — Retained Terminal Drain and Stream Handoff Corrective Pass

Date: 2026-08-05
Status: ready for implementation
Parent roadmap: `plans/070-failure-resilience-router-recovery-and-sbc-simplification-roadmap.md`
Depends on:

- `plans/071-attempt-scoped-failure-classification-and-effects.md`
- `plans/072-upstream-dispatch-retry-and-response-isolation.md`
- `plans/074-restart-safe-runtime-and-database-simplification.md`

Planning baseline: `cf0edf3dde52e744e8cd9d7392c343f923f8a0d2`

## Purpose

Close the two remaining request-lifecycle defects found after implementation of Roadmap 070:

1. retained failed-attempt cleanup and post-commit claim compensation are stored in aliased dictionaries containing incompatible progress types, so shutdown drain can resume a claim-compensation entry with the failed-attempt runner;
2. streaming `downstream_started` remains tied to the first yielded body bytes rather than the actual ASGI `http.response.start` boundary, so an empty or immediately failing stream can be misclassified as pre-handoff after headers were committed.

This is a narrow corrective pass. It must preserve the simplified single-host/SBC architecture, the existing one-loop runtime, startup crash repair, bounded finalization ownership, and the existing one-job smoke CI surface.

The implementation should remove the unsafe aliases and establish one truthful response-handoff fact without introducing another supervisor, recovery framework, generic workflow engine, response buffer, or test campaign.

## Confirmed defect A — aliased retained registries dispatch the wrong runner

`RequestCoordinator` currently maps these historical properties to the same dictionaries:

- `_attempt_cleanup_tasks` and `_claim_compensation_tasks` both reference `_retained_terminal_tasks`;
- `_attempt_cleanup_progress` and `_claim_compensation_progress` both reference `_retained_terminal_progress`.

The shared progress dictionary therefore contains both:

- `AttemptCleanupProgress`;
- `ClaimCompensationProgress`.

`drain_retained_cleanup()` first iterates `_attempt_cleanup_progress` and starts `_start_attempt_cleanup_task()` for every incomplete entry. Because claim-compensation entries are visible through that alias, a resumable `ClaimCompensationProgress` can be passed to `_run_failed_attempt_cleanup()`, which expects fields such as `selected` and `effect_progress` that claim compensation does not own.

The second compensation loop then sees the incorrect task in the same aliased task dictionary and does not start the correct compensation runner during that drain pass.

Consequences:

- shutdown drain can fail to resume post-commit compensation correctly;
- a bounded graceful shutdown may leave more work for startup crash repair than necessary;
- diagnostics claim one consolidated owner while the implementation has no type-safe command discriminator;
- tests of each runner independently do not exercise mixed shutdown-drain dispatch.

This does not justify restoring the old parallel recovery architecture. The fix is a small typed retained-command registry with one dispatcher.

## Confirmed defect B — stream handoff is inferred from body output

The streaming generator currently sets:

```python
context.client_metadata["downstream_started"] = True
```

immediately before yielding a non-empty translated or pass-through body chunk.

However, Starlette sends `http.response.start` before it begins iterating the streaming body. The following cases can therefore have committed response status and headers while the coordinator still records `downstream_started=False`:

- an empty upstream stream;
- premature EOF before the first body chunk;
- an idle or transport failure after response start but before body output;
- local streaming-transcoder failure on the first frame;
- finalization-supervisor saturation after headers are committed but before bytes are yielded.

Consequences:

- finalization capacity handling can choose the pre-handoff branch after handoff;
- EOF and stream diagnostics can report “before body” when the response has already started;
- terminal cleanup can retain an incorrect handoff fact;
- future retry logic could accidentally treat a started response as replayable if it consumes the same metadata.

Body-byte accounting and response handoff are separate facts. `bytes_emitted` remains accounting; one ASGI response-start signal must become the authority for `downstream_started`.

## Governing decisions

1. **One retained registry, one tagged command type.** Keep the Plan 074 consolidation, but store a small tagged command object rather than aliasing dictionaries of incompatible progress types.
2. **No generic command framework.** The registry supports exactly two command kinds: failed-attempt cleanup and claim compensation.
3. **One durable identity key.** Commands remain keyed by `(proxy_request_id, attempt_id)`.
4. **One runner dispatcher.** Initial execution, rejoin, and shutdown drain all call the same kind-aware dispatcher.
5. **Conflicting command kind fails closed.** The same durable identity cannot silently change from one retained command kind to another.
6. **Capacity remains global and bounded.** The existing retained cleanup capacity applies to the single registry.
7. **ASGI response start owns handoff.** A streaming response marks handoff when forwarding `http.response.start`, not when yielding body data.
8. **Handoff is monotonic.** Once response start is attempted, the lifecycle fact never becomes false.
9. **Body accounting remains separate.** Zero bytes with a started response is valid and must be represented truthfully.
10. **No retry after response start.** All failure classification and terminal paths consume the same handoff state.
11. **No full-stream buffering or response replay.** This pass only fixes lifecycle facts and retained command dispatch.
12. **No CI expansion.** Add focused regressions to existing capability-oriented test files and keep the current smoke job.

## Phase A — Introduce one typed retained terminal command

### Required shape

Add one small coordinator-owned record, for example:

```python
RetainedTerminalKind = Literal[
    "failed_attempt_cleanup",
    "claim_compensation",
]

@dataclass(slots=True)
class RetainedTerminalCommand:
    kind: RetainedTerminalKind
    progress: AttemptCleanupProgress | ClaimCompensationProgress
    task: asyncio.Task[None] | None = None
```

The exact naming can follow local style, but the implementation must retain these properties:

- the kind is explicit and immutable after registration;
- progress is stored once;
- the active task is stored once;
- one dictionary owns all retained commands;
- no compatibility property exposes the same dictionary as two incompatible typed maps.

Do not add a general command protocol, plugin registry, serializer, database table, or class hierarchy.

### Registration

Replace the current aliasing behavior with one helper that:

1. accepts the durable key and expected command kind;
2. returns the existing command when kind and identity match;
3. rejects a conflicting kind for the same key with a local invariant error;
4. enforces the existing global capacity before adding a new command;
5. constructs the appropriate progress record exactly once;
6. does not start detached work before registry ownership exists.

The failed-attempt path must register `AttemptCleanupProgress`.

The post-commit publication path must register `ClaimCompensationProgress`.

A completed command may be removed after its caller or drain path has confirmed `progress.completed`. Removal must not occur merely because the child task returned successfully.

### Execution

Create one dispatcher such as:

```python
async def _run_retained_terminal_command(command: RetainedTerminalCommand) -> None:
    if command.kind == "failed_attempt_cleanup":
        await self._run_failed_attempt_cleanup(...)
    else:
        await self._run_claim_compensation(...)
```

The dispatcher must validate the progress type before invoking the runner. A kind/progress mismatch is a local invariant failure and must not be converted into provider health evidence.

All task creation must flow through one starter that:

- uses the command kind in the task name;
- stores the task on the command before returning it;
- clears only that command's task reference in the done callback;
- leaves incomplete progress registered for bounded rejoin;
- logs only bounded identity and exception-class details;
- never creates a second task while the first remains active.

### Rejoin and drain

Update:

- `_cleanup_failed_attempt()`;
- `_compensate_or_rollback_claim()`;
- `_join_attempt_cleanup()`;
- cancellation convergence helpers;
- `drain_retained_cleanup()`;
- `retained_cleanup_snapshot()`.

`drain_retained_cleanup()` must iterate the single registry once. For each incomplete command it must start or join the runner selected by `command.kind`.

Do not perform two loops over aliased views.

The drain result must count unique unresolved durable identities, not task-list duplicates.

Diagnostics may continue exposing the historical attempt/compensation counts for compatibility, but they must derive them by filtering command kinds rather than by reading aliased registries. The authoritative fields should remain:

- active retained terminal tasks;
- resumable retained terminal entries;
- capacity rejections.

### Shutdown and startup boundary

A bounded shutdown timeout may still leave commands unresolved. In that case:

- do not cancel process-owned work merely to make the count zero;
- log the unique unresolved identities;
- allow startup crash repair to reconcile durable leftovers after restart;
- do not claim graceful convergence for a command whose progress is incomplete.

This pass does not alter startup crash-recovery SQL or introduce persistence for in-memory command progress.

## Phase B — Add an authoritative ASGI stream handoff state

### Lifecycle state

Add a small monotonic handoff state owned by the request context, for example:

```python
@dataclass(slots=True)
class ResponseHandoffState:
    started: bool = False

    def mark_started(self) -> None:
        self.started = True
```

Requirements:

- every proxy request has one state object;
- non-streaming responses may continue using their current terminal ordering, but the state must not conflict with it;
- streaming response construction and stream finalization share the same object;
- the state stores no body, headers, credentials, or traceback;
- setting it repeatedly is idempotent.

Avoid a global registry and avoid a broad middleware that tracks unrelated dashboard/static responses.

### Streaming response boundary

Introduce a narrowly scoped proxy streaming response wrapper or subclass in the API response layer.

It must wrap the ASGI `send` callable and, when handling `http.response.start`:

1. mark the shared handoff state immediately before forwarding the start message;
2. forward the original status and headers unchanged;
3. remain transparent for all body and disconnect messages.

This wrapper must be used only for EggPool upstream streaming proxy responses.

Do not infer handoff from:

- `bytes_emitted`;
- `downstream_bytes_emitted`;
- the first body chunk;
- generator creation;
- handler return alone.

A response that starts and emits zero body bytes must have:

```text
downstream_started = true
bytes_emitted = 0
```

A failure before the response object reaches ASGI response start must retain:

```text
downstream_started = false
```

### Coordinator integration

Replace authoritative reads of `context.client_metadata["downstream_started"]` with the shared handoff state.

The metadata key may remain temporarily as a diagnostic compatibility mirror only if existing tests or external embedders require it. If retained:

- the state object is authoritative;
- the mirror must be updated from the state, not independently;
- new code must not read the mirror for retry or finalization decisions;
- documentation must state the deprecation.

Thread the state into every `FinalizationData.downstream_started` construction used by streaming paths, including:

- canonical completion;
- compatibility completion;
- premature or malformed EOF;
- client cancellation;
- idle/first-byte/transport timeout;
- local stream translation failure;
- finalization-capacity handling.

### Retry boundary

The request retry loop already returns the streaming response before body iteration. Preserve that behavior and add a direct assertion that no reroute path is reachable after `ResponseHandoffState.started` is true.

Do not add stream replay, buffering, hedging, or a second upstream request after response start.

### Response and lease wrapping

The existing generation-lease wrapper must continue to release the lease on:

- normal stream completion;
- generator failure;
- cancellation/disconnect.

Ensure the handoff-aware response wrapper and lease wrapper compose without double-wrapping the body iterator or losing headers/media type.

Prefer one custom streaming response construction path rather than nesting multiple `StreamingResponse` instances.

## Phase C — Closure truthfulness and narrow schema cleanup

### Roadmap status

When implementation begins, update Roadmap 070 to reference Plan 076 and mark its status as corrective follow-up in progress.

Do not mark Roadmap 070 complete again until Plan 076 acceptance criteria pass.

When closing:

- check Roadmap 070 acceptance boxes that are actually proven;
- leave any unproven item open rather than marking it by inference;
- record the exact focused and smoke commands run;
- do not create another roadmap or evidence bundle.

### Architecture documentation

Update the request-lifecycle documentation and `AGENTS.md` to state:

- one tagged retained terminal registry owns failed-attempt cleanup and claim compensation;
- shutdown drain dispatches by retained command kind;
- `downstream_started` means ASGI response start was sent/attempted;
- body byte count is not the handoff signal;
- empty started streams are post-handoff terminal paths.

Remove wording that says handoff is set “immediately before stream delivery” if that could be read as first body yield.

### Duplicate dispatch-writer config surface

Audit the two current schema surfaces:

- top-level `[dispatch_writer]`;
- nested `[database.dispatch_writer]`.

This cleanup is subordinate to the two correctness fixes and must remain small.

Required decision:

1. identify all production consumers and shipped examples;
2. retain one canonical surface;
3. if the nested field is unused compatibility residue, stop presenting it as active configuration;
4. preserve existing configuration compatibility only when there is evidence users could rely on it;
5. use at most one bounded deprecation warning and one normalization path if compatibility is needed;
6. do not build a general configuration migration subsystem.

If removal would require a broader compatibility design, document the canonical field and defer physical removal to the next intentional breaking release. That deferral does not block Plan 076 closure.

## Focused verification

Add tests to existing capability-oriented files. Do not create plan-numbered test directories or a new test harness.

### Retained command tests

Required representative cases:

1. a failed-attempt cleanup command resumes with the failed-attempt runner;
2. a claim-compensation command resumes with the claim-compensation runner;
3. shutdown drain containing one command of each kind starts each correct runner exactly once;
4. a failed claim-compensation task remains registered and is resumed correctly by drain;
5. a failed attempt-cleanup task remains registered and is resumed correctly by drain;
6. a kind/progress mismatch fails as a local invariant before any runtime/provider mutation;
7. registering a conflicting command kind for the same durable identity fails closed;
8. completed commands retire and free capacity;
9. unresolved counts use unique durable identities;
10. capacity remains globally bounded across both command kinds;
11. cancellation rejoin still submits one canonical client-cancelled terminal outcome;
12. no failure-shape or lifetime-global dedupe state is reintroduced.

Prefer extending `tests/unit/test_request_coordinator_cleanup.py` and the existing finalization integration coverage.

### Stream handoff tests

Required representative cases:

1. ASGI `http.response.start` marks handoff before the first body iteration;
2. an empty streaming iterator records `downstream_started=True` and zero bytes;
3. first-frame local translation failure after response start is post-handoff;
4. premature EOF after response start but before body output is post-handoff;
5. cancellation after response start but before body output is post-handoff;
6. failure before response start remains pre-handoff;
7. finalization capacity with response started and zero bytes uses the post-handoff branch;
8. no retry/reroute occurs after response start;
9. response status, headers, and generation-lease release behavior are unchanged;
10. non-streaming adaptation-before-success tests remain green.

Use a direct ASGI send collector or Starlette response invocation. Do not require a live server, network provider, timing sleep, or browser.

### Existing gates

Run focused checks first, then the existing repository gate:

```bash
uv run ruff format src/eggpool/request src/eggpool/api tests/unit tests/integration
uv run ruff check src/eggpool/request src/eggpool/api tests/unit tests/integration
uv run pyright src/eggpool/request src/eggpool/api
uv run pytest tests/unit/test_request_coordinator_cleanup.py <affected stream/finalization tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

If the configuration surface changes, also run:

```bash
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
uv run pytest tests/unit/test_config.py tests/unit/test_config_validation_extended.py -q --tb=short --maxfail=1
```

No live-provider, long-stream soak, process farm, systemd CI service, coverage threshold, benchmark gate, or matrix is required.

## Recommended implementation sequence

1. Add the tagged retained-command record and single registry.
2. Route failed-attempt cleanup and claim compensation registration through one helper.
3. Replace task creation with one kind-aware starter and dispatcher.
4. Rewrite rejoin, drain, and diagnostics to iterate the single registry once.
5. Add mixed-command and failed-resume regressions.
6. Add the request-owned response handoff state.
7. Add the handoff-aware proxy streaming response boundary.
8. Replace stream lifecycle reads of body-yield metadata with the state object.
9. Add empty-stream, pre-first-body failure, cancellation, capacity, and no-retry regressions.
10. Audit and document the canonical dispatch-writer config field without expanding scope.
11. Update Roadmap 070, architecture docs, and `AGENTS.md` truthfully.
12. Run focused checks and the existing smoke gate.
13. Close Plan 076 and Roadmap 070 only after the mixed drain and ASGI boundary regressions pass.

## Plan acceptance criteria

- [ ] Retained failed-attempt cleanup and claim compensation are represented by one explicitly tagged command type.
- [ ] One registry owns command progress and active task state without incompatible alias dictionaries.
- [ ] The same durable identity cannot silently change command kind.
- [ ] One dispatcher chooses the correct runner from the stored command kind.
- [ ] Shutdown drain iterates the registry once and resumes each command with the correct runner.
- [ ] A failed claim-compensation command remains resumable and is not passed to failed-attempt cleanup.
- [ ] A failed attempt-cleanup command remains resumable and is not passed to claim compensation.
- [ ] Completed commands retire only after component convergence is proven.
- [ ] Capacity remains globally bounded and fail closed.
- [ ] Diagnostics count command kinds without using aliased registries.
- [ ] One request-owned state records streaming ASGI response start.
- [ ] `downstream_started` no longer depends on first body bytes.
- [ ] An empty started stream records handoff true and bytes emitted zero.
- [ ] Pre-start failure remains distinguishable from post-start, pre-body failure.
- [ ] Every streaming terminal path uses the authoritative handoff state.
- [ ] Finalization saturation after response start uses the post-handoff behavior even with zero bytes.
- [ ] Retry/reroute is impossible after ASGI response start.
- [ ] Generation lease release and upstream response closure remain correct.
- [ ] No provider health effect is created by retained-command type mismatch or local response wrapper failure.
- [ ] Roadmap 070 references Plan 076 and is not marked complete before this pass closes.
- [ ] The canonical dispatch-writer configuration surface is documented truthfully without a migration framework.
- [ ] Focused regressions, Ruff, Pyright, and the existing smoke suite pass.
- [ ] CI remains one Python 3.11 job with no matrix, coverage, performance, soak, or fault-campaign gate.
- [ ] No new supervisor, generic workflow engine, persistent cleanup queue, response buffer, replay mechanism, or distributed coordination is introduced.

## Rejection conditions

Do not close this plan if:

- attempt cleanup and claim compensation still share an untagged dictionary of incompatible progress objects;
- shutdown drain can select a runner from the property used to access the registry rather than from stored command kind;
- a child task returning successfully is treated as convergence without checking progress;
- command progress is discarded on failure before startup repair can cover durable leftovers;
- response handoff is still inferred from bytes emitted or first generator yield;
- a zero-byte response-started stream is treated as pre-handoff;
- finalization capacity can raise a pre-handoff invariant after headers were committed;
- the fix adds stream replay, buffering, hedging, or retry after response start;
- the fix restores multiple competing retained supervisors or database recovery controllers;
- a configuration cleanup introduces a generalized migration layer;
- focused tests are replaced with a timing-dependent server soak;
- CI or release ceremony expands beyond the existing scope.

## Definition of done

Plan 076 is complete when retained terminal work has one type-safe command registry and one correct shutdown dispatcher, streaming handoff is recorded at the actual ASGI response-start boundary rather than first body output, empty and immediately failing streams are classified truthfully, focused regressions prove both fixes, and Roadmap 070 can be closed without restoring any of the recovery or verification complexity removed by Plans 071–075.
