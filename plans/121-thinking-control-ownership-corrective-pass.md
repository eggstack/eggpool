# Plan 121 — Thinking-Control Ownership Corrective Pass

Date: 2026-08-13
Status: ready
Planning baseline: `fa530408157ff4ebb76b47c8ba914659133b7fe6`
Scope: standalone corrective pass following completed Roadmap 113

## Purpose

Close the remaining request-payload ownership inefficiency identified after Roadmap 113 without reopening EggPool's architecture.

The residual problem is narrow. `RequestCoordinator._adapt_provider_thinking_controls()` currently obtains `request.provider_payload_copy()`, which recursively deep-copies the provider graph, passes it to the mostly path-local `adapt_thinking_controls()`, then sends a changed result through `replace_provider_payload()`. That replacement performs a whole-payload equality comparison and recursively materializes the graph again. A large request whose only provider-specific change is a reasoning-control field can therefore incur substantially more object-graph work than necessary.

This pass also fixes a planning-record defect: Plans 117 and 120 identify Plan 117's implementation as non-resolving SHA `21f5ba0`. The actual cache-dialect implementation commit is `52793638a54a60b99585df87afc3492f2acd9edd` (`Correct provider cache dialect handling`).

This is one corrective plan. Record closure in this file; do not create another roadmap or closure-plan chain solely for this work.

## Current expensive path

For a changed thinking-control request the current flow can effectively be:

```text
provider_payload_copy()
  -> recursive deepcopy of messages/tools/etc.
  -> adapt_thinking_controls() path-local edits
  -> replace_provider_payload()
  -> whole-payload equality traversal
  -> recursive _owned_json_value() materialization
```

The actual provider-visible edits are normally limited to root controls or the top-level `thinking` mapping:

- `reasoning_effort`;
- `thinking.type`;
- `thinking.effort`;
- `thinking.budget_tokens`;
- `thinking_budget`;
- removal of unsupported thinking-control fields.

Roadmap 113 already supplies the required ownership primitives. `adopt_provider_payload()` is the trusted boundary for an EggPool-owned request-local graph whose changed ancestors have been copied. `mutate_top_level_mapping()` already performs root + affected-child copy-on-write. `PreparedTranscode` is request-local and no longer recursively frozen. Do not introduce a new COW framework.

## Governing constraints

1. Preserve all existing thinking/reasoning reject, drop, map, warning, effort-alias, and budget semantics.
2. Preserve canonical `client_payload` immutability and PreparedTranscode source immutability.
3. Preserve provider generation, serialization, retry, and freeze semantics.
4. Do not change routing, retry/backoff, health, quarantine, database, finalization, crash recovery, rehash, provider pools, HTTPX transport, compression, or cache-dialect behavior.
5. Do not add a runtime dependency.
6. Do not expand CI or add benchmark, soak, profiling, allocation-telemetry, or hardware-CI infrastructure.
7. Test-local identity/call-count instrumentation is allowed; production telemetry is not.
8. No live provider credentials, Raspberry Pi workload, or full retained-suite run is required.
9. Stop when this plan's acceptance criteria are met.

## Workstream A — Audit ownership helper callers

Before editing, inventory production and test callers of:

- `provider_payload_copy()`;
- `replace_provider_payload()`;
- `adopt_provider_payload()`;
- `mutate_provider_payload()`;
- `mutate_top_level_mapping()`.

Classify each production use as:

1. conservative ownership of an unknown/external graph;
2. trusted EggPool-owned path-COW graph;
3. arbitrary legacy mutator requiring full ownership;
4. read-only inspection that does not require a detached graph.

Answer specifically:

- whether `_adapt_provider_thinking_controls()` is the common production reason for `provider_payload_copy()`;
- whether any production `mutate_provider_payload()` caller mutates nested children;
- whether any caller genuinely relies on `replace_provider_payload()` performing whole-graph equality detection;
- whether adaptation can run after PreparedTranscode reuse, and how that source remains unchanged.

Do not commit a permanent caller matrix. Record the final helper disposition in this plan's closure.

## Workstream B — Remove duplicate whole-graph work from thinking adaptation

### Required ownership contract

`adapt_thinking_controls()` must treat its source payload as read-only. It may return the source unchanged, or construct a changed request-local result by copying only the root/path ancestors needed for the provider-visible edit.

Preferred implementation direction:

1. pass the current provider payload to `adapt_thinking_controls()` without first recursively deep-copying it;
2. if useful, widen the adapter boundary to `Mapping[str, Any]` to make the read-only input contract explicit, but avoid unrelated typing churn;
3. retain shallow root copies for root control changes;
4. retain root + `thinking` copies for nested thinking changes;
5. when `result.changed` is false, leave `ProviderBoundRequest` untouched;
6. when `result.changed` is true, adopt the result through the existing trusted `adopt_provider_payload(..., reason="thinking_control")` boundary or an equivalently narrow existing API;
7. do not run the already-classified changed result through `replace_provider_payload()` merely to compare and recursively re-own it.

### Path-local rules

- Changing/dropping `reasoning_effort`: copy root only.
- Changing/dropping `thinking_budget`: copy root only.
- Changing a field inside `thinking`: copy root and `thinking` only.
- Removing the whole `thinking` block: copy root; do not mutate source nested state.
- Mapping budget/effort inside `thinking`: copy root and `thinking` only.
- Unchanged `messages`, `tools`, and unrelated content remain shared read-only where permitted.

No adapter branch may mutate the incoming root or nested source mapping in place.

### No-op behavior

When the selected provider already accepts the control:

- no full request copy solely for validation;
- no payload-generation increment;
- no provider-byte invalidation;
- no forced serialization;
- native/prepared byte reuse remains available according to the existing lifecycle.

A reject path must likewise inspect without creating a provider generation merely to validate fields.

### Changed behavior

When provider-visible content changes:

- create exactly one provider generation for the adaptation stage;
- invalidate bytes for the prior generation;
- keep canonical/prepared source graphs unchanged;
- serialize the final changed generation once at the existing boundary;
- do not perform a second whole-graph equality traversal to rediscover `result.changed`.

## Workstream C — Preserve PreparedTranscode and retry behavior

Explicitly cover the case where a cross-protocol preflight is reused and the selected provider then requires thinking-control normalization.

Required behavior:

- `PreparedTranscode.translated_payload` remains unchanged;
- unchanged prepared reuse keeps using the existing encoded body;
- later thinking adaptation copies only affected paths and creates one provider generation;
- the changed provider generation is encoded once before first dispatch;
- retry after freeze reuses the same frozen bytes and does not rerun adaptation;
- dispatch-buffer release behavior remains unchanged.

Do not make PreparedTranscode recursively immutable again.

## Workstream D — Resolve `mutate_provider_payload()` safely

The generic arbitrary-mutator helper is an ownership sharp edge because an adopted provider graph may intentionally share unchanged descendants with its source.

Audit all callers and choose one of two dispositions:

### If no production caller needs it

Remove it and migrate remaining tests/compatibility uses to explicit APIs:

- `mutate_top_level_mapping()` for known narrow changes;
- `adopt_provider_payload()` for EggPool-owned COW results;
- conservative setters for unknown ownership.

### If production use remains

Retain it only as an explicitly conservative path. It must establish a fully detached mutable graph before invoking an arbitrary mutator, even when `_provider_payload` already exists. Document that this path is intentionally more expensive than path-level COW.

Do not attempt to make arbitrary Python mutation automatically copy-on-write.

`provider_payload_copy()` and `replace_provider_payload()` may remain as conservative helpers if caller audit shows they are still useful. Do not force repository-wide deletion; simply keep the known thinking-control hot path off them.

## Workstream E — Correct Plan 117 SHA records

Correct these files:

- `plans/117-provider-cache-dialect-correctness.md`;
- `plans/120-sbc-characterization-and-roadmap-closure.md`.

Replace the Plan 117 implementation reference `21f5ba0` with the actual commit:

```text
52793638a54a60b99585df87afc3492f2acd9edd
```

Plan 120 may use the corresponding short SHA in its table if it follows that file's existing style.

Run a bounded repository search, for example:

```bash
rg -n '21f5ba0|52793638' plans docs README.md AGENTS.md
```

Correct only references intended to identify Plan 117's implementation. Do not rewrite unrelated historical commit records.

## Workstream F — Focused regression coverage

Put tests into existing behavior-oriented suites; do not create plan-numbered test files.

### Large no-op request

Use a deterministic request with large message history, nested tool schemas, and an already-valid thinking control.

Verify:

- passthrough/unchanged decision;
- no production-path full deepcopy solely for adaptation;
- generation unchanged;
- cached bytes remain valid;
- messages/tools/source payload unchanged.

### Root-level mapping/drop

Use an effort alias or drop policy that changes a top-level control.

Verify root changes, messages/tools retain source identity, source graph is unchanged, generation increments once, and final serialization occurs once.

### Nested `thinking` adaptation

Change `thinking.effort` or `thinking.budget_tokens`.

Verify root and `thinking` are distinct from source, unrelated large descendants retain identity, source `thinking` is unchanged, and generation increments once.

### Reject path

Use an unsupported control with reject policy.

Verify the existing `CapabilityError`, no payload mutation/generation before rejection, selected-attempt finalization remains correct, and no provider health penalty is introduced.

### Prepared transcode + adaptation

Verify prepared source remains unchanged, the provider result uses path-COW, one new provider generation is encoded, and frozen retry reuses those bytes.

### Arbitrary-mutator safety

If `mutate_provider_payload()` is retained, deliberately mutate a nested descendant after adopting a graph and prove canonical/prepared state cannot be modified. If it is removed, update owning tests to exercise the explicit APIs instead.

### Generation truthfulness

Cover:

- no-op adaptation: generation unchanged;
- changed adaptation: +1 generation;
- repeated no-op validation: no additional generation;
- frozen retry: no transform rerun;
- serialized bytes always correspond to current generation.

Likely owning suites include `test_provider_bound_request.py`, `test_transform_pipeline.py`, prepared-transcode tests, existing provider-control/thinking adaptation tests, selected-provider capability rejection tests, and `test_thinking_reasoning_matrix.py`. Discover the current owners rather than duplicating tests.

## Verification

Run the focused ownership/thinking/prepared/rejection union, then the normal gate:

```bash
uv sync --frozen --extra ci
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

The full retained suite is optional/manual.

Use focused test-local identity or call-count evidence to prove that ordinary thinking adaptation no longer performs the reviewed sequence of full deepcopy plus whole-graph equality plus recursive re-ownership. Do not turn those checks into runtime metrics or performance thresholds.

## Explicit acceptance criteria

- [ ] `_adapt_provider_thinking_controls()` no longer recursively deep-copies the complete provider payload merely to validate/adapt ordinary controls.
- [ ] `adapt_thinking_controls()` has an explicit read-only-source/path-COW contract or an equivalently safe narrow contract.
- [ ] No-op thinking adaptation performs no whole-request ownership copy solely for validation.
- [ ] No-op adaptation does not increment `payload_generation`, invalidate bytes, or force serialization.
- [ ] Root-level changes copy only the root unless another affected path genuinely requires ownership.
- [ ] Nested `thinking` changes copy root + `thinking` without recursively copying unchanged messages/tools.
- [ ] Changed adaptation no longer performs a whole-payload equality traversal followed by recursive ownership materialization merely to adopt its own result.
- [ ] Changed adaptation creates exactly one provider generation for that stage.
- [ ] Canonical client payload remains unchanged in passthrough, mapped, dropped, and rejected branches.
- [ ] Reused PreparedTranscode source remains unchanged after selected-provider adaptation.
- [ ] Final changed provider bytes are encoded once and frozen retry reuses them.
- [ ] Reject/drop/map/warning behavior, effort aliases, budget bounds, and capability policies are unchanged.
- [ ] Capability rejection finalization and no-provider-health-penalty behavior remain correct.
- [ ] `mutate_provider_payload()` is either removed after caller audit or retained with a conservative full-ownership contract that cannot mutate shared adopted descendants.
- [ ] New path-local transforms are not routed through the arbitrary generic mutator.
- [ ] Conservative `provider_payload_copy()` / `replace_provider_payload()` helpers are removed only if caller audit proves they are unnecessary; otherwise they remain off the corrected hot path.
- [ ] Plan 117 records `52793638a54a60b99585df87afc3492f2acd9edd` as its implementation commit.
- [ ] Plan 120's Plan 117 audit row resolves to that same commit.
- [ ] No remaining repository reference incorrectly identifies `21f5ba0` as Plan 117's implementation.
- [ ] Cache-dialect production behavior is unchanged by the planning-record correction.
- [ ] Routing, retry/backoff, database, finalization, rehash, provider pool, compression, and transport behavior remain unchanged.
- [ ] No runtime dependency is added and ordinary CI remains one Python 3.11 Ruff/Pyright/smoke job.
- [ ] No benchmark, soak, profiling, hardware-CI, allocation telemetry, or performance threshold is introduced.
- [ ] Focused ownership/thinking/prepared/rejection tests pass.
- [ ] Ruff format, Ruff lint, Pyright, 14 smoke tests, and both config checks pass.
- [ ] Implementation evidence is appended to this plan; no separate closure plan is created solely to close Plan 121.

## Rejection conditions

Reject the implementation if:

1. canonical or prepared source state can be mutated through an adopted graph;
2. `deepcopy()` is merely replaced with another recursive full-graph clone on the common thinking path;
3. a new generalized COW/immutable JSON framework is introduced;
4. no-op adaptation still changes generation or forces serialization;
5. one logical adaptation stage creates multiple provider generations without separate provider-visible semantics;
6. whole-request equality remains mandatory even though the adapter deterministically reports `changed`;
7. arbitrary-mutator safety depends on future callers remembering not to mutate nested shared objects;
8. PreparedTranscode is recursively frozen again;
9. provider-control behavior changes independently of an ownership defect;
10. routing, health, database, finalization, rehash, or cache-dialect semantics change;
11. the stale Plan 117 SHA is replaced with another non-resolving identifier;
12. CI/test/profiling infrastructure grows for this correction.

## Handoff sequence

1. Read this plan, completed Plans 114/115/116/117/120, `ProviderBoundRequest`, coordinator thinking adaptation, provider-adaptation helpers, and owning tests.
2. Audit all ownership-helper callers.
3. Add/adjust focused behavioral tests proving current copy behavior and source isolation.
4. Make adapter input read-only and changed results path-COW.
5. Adopt changed results through the existing trusted ownership boundary; remove the redundant equality/rematerialization step from this path.
6. Remove or conservatively harden `mutate_provider_payload()` based on actual callers.
7. Correct Plan 117/120 SHA references and search for other stale occurrences.
8. Run focused tests and the ordinary gate.
9. Append to this file: implementation SHA, helper dispositions, deterministic before/after ownership behavior, exact focused test results, standard gate results, and acceptance reconciliation.
10. Mark this plan complete and stop. Do not create a follow-up closure plan unless implementation discovers a genuinely separate defect.
