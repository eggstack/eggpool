# Plan 177 — Codegg Semantic Routing Provider-Selection Corrective Pass

Date: 2026-09-12
Status: verification in progress
Parent roadmap: `plans/173-shared-model-routing-crate-roadmap.md`
Follows: Plans 174–176
EggPool baseline: `eca8c4e33672ffc3c783274a8218c6eac6832fee`
Codegg baseline: `881c61720f9a80162094b85c6beeaa68da6af6d0`
Priority: P1 correctness / routing ownership invariant
Execution target: GPT-5.6 Luna/Sol or comparable implementation model
Primary implementation repository: `dbowm91/codegg`

## Purpose

Correct one ownership mismatch found after Plans 173–176 were marked complete.

The shared `eggpool-model-routing` crate extraction itself is correct and should
remain unchanged. EggPool still owns provider/account routing, health, quota,
retry, reservations, transport, and release/update behavior. Codegg correctly
reuses the shared semantic compiler/validator through `codegg-core` and keeps the
shared crate pinned to an immutable EggPool revision.

The corrective issue is in Codegg's semantic-route execution path. A semantic
route currently resolves a configured concrete model such as
`anthropic/claude-sonnet`, extracts the provider prefix, looks up that provider,
and assigns the provider object used for the turn. That makes semantic model
selection also act as runtime provider selection. Even if the durable
`ProviderConnectionId` record is not rewritten, this crosses the ownership
boundary established by Plan 175: semantic routing must not silently become a
second provider/session-selection mechanism.

This plan is intentionally narrow. Do not reopen the shared-crate extraction,
provider discovery, EggPool account routing, release tooling, dependency cleanup,
or general Codegg provider architecture.

## Current evidence

At the Codegg baseline, `src/agent/request_preparation.rs` performs semantic
resolution and then:

1. parses the provider prefix from `decision.resolved_model`;
2. looks that provider up in the registry;
3. replaces `self.services.provider` with `provider.clone_box()`;
4. rewrites `request.model` to the concrete model suffix.

This means a route can effectively change the executing provider based on
semantic policy rather than on Codegg's provider/session selection authority.

The current architecture documentation says semantic routing does not change
provider-connection selection. The implementation and documentation therefore
need to be reconciled in favor of the ownership invariant, not by weakening the
documentation.

A second, smaller clarification is required: Codegg passes `sticky` and
`affinity_ttl_s` through the shared policy types but does not currently maintain
the EggPool async sticky-affinity cache. This was permitted by Plans 175–176,
but Codegg's configuration/docs must not imply that `sticky = true` has active
Codegg-side affinity semantics unless that behavior is actually implemented.

## Required invariants

The implementation must preserve all of the following:

- EggPool remains the sole owner of account/provider routing behind an EggPool
  endpoint.
- Codegg remains the owner of its durable provider-connection/session selection.
- Shared semantic routing chooses among configured model routes; it does not
  silently replace a selected provider connection.
- A semantic selector may use Codegg's normal provider abstraction, but selector
  execution must not mutate durable provider/session selection.
- Concrete models continue to bypass semantic routing.
- Invalid selector output cannot inject an arbitrary model/provider; only exact
  compiled route IDs are accepted.
- Selector timeout/failure behavior remains bounded and falls back to the
  configured default route, except caller cancellation must still propagate.
- Once a concrete target request begins, ambiguous transport/upstream failure
  must not trigger semantic reselection to another route.
- The shared crate must remain application-neutral and must not gain Codegg
  provider/session types.
- Codegg must not import EggPool's stateful `rust/src/routing/` provider/account
  router.

## Phase 1 — Re-state the Codegg execution boundary

Before editing behavior, trace the actual provider-selection authority in Codegg
and document the exact objects involved:

- durable selected provider connection/session state;
- provider registry entries;
- per-turn provider object in `AgentLoopServices` / equivalent;
- model string carried in `ChatRequest`;
- EggPool provider adapter behavior when Codegg is connected to EggPool;
- direct-provider behavior when Codegg is not using EggPool.

Confirm whether `self.services.provider` is always derived from the durable
selected connection or can represent a registry-wide provider choice independent
of it. The fix must target the actual authority boundary rather than merely
renaming fields.

Record this as a short implementation note in the Codegg change or architecture
documentation.

## Phase 2 — Remove semantic routing as an implicit provider switch

Refactor semantic-route application so semantic resolution cannot silently
replace the provider selected for the current turn/session.

Preferred behavior:

1. semantic routing resolves `virtual:<name>` to an exact configured concrete
   route;
2. Codegg validates that the resolved route is executable through the provider
   connection already selected for the turn/session;
3. if compatible, only the concrete model portion is applied to the request;
4. if incompatible, return a typed configuration/routing error before starting
   the concrete request rather than silently changing provider authority.

Examples:

- selected connection: EggPool endpoint; route:
  `openai/gpt-5.6` -> allowed when the EggPool adapter accepts the routed model;
  EggPool remains responsible for choosing the underlying account/provider.
- selected connection: direct OpenAI; route:
  `openai/gpt-5.6` -> allowed.
- selected connection: direct OpenAI; route:
  `anthropic/claude-*` -> fail closed unless Codegg has an explicit,
  user-selected cross-provider connection model that makes this legal.
- selected connection: direct Anthropic; route:
  `anthropic/claude-*` -> allowed.

Do not implement automatic cross-provider migration as part of this corrective
pass.

### Compatibility helper

Introduce one small explicit helper if needed, for example:

```text
validate_semantic_route_against_selected_connection(...)
```

It should answer whether the shared route is compatible with the already
selected provider connection and return a typed error with bounded, non-secret
context when incompatible.

Do not create a new routing subsystem or registry abstraction for this.

## Phase 3 — Keep selector execution side-effect free

Audit `src/agent/semantic_router.rs` and all call sites to ensure selector
execution itself cannot mutate provider/session selection.

Selector execution may:

- read the configured selector model;
- obtain a provider handle needed to perform the selector request;
- send the bounded selector prompt;
- perform the configured bounded repair attempt;
- validate exact route IDs;
- return a semantic decision.

Selector execution must not:

- persist a different provider connection;
- mutate the active session's provider selection;
- cause the resolved target provider to become the new durable selection;
- reuse a selector provider handle as the concrete target provider unless that
  provider is already the selected execution connection and validation permits
  it.

If Codegg's provider registry currently makes selector invocation impossible
without picking a provider by prefix, that is acceptable for the selector call
only. Keep it a local read-only dependency; do not promote it into execution
provider authority.

## Phase 4 — Clarify `sticky` / affinity semantics

Do not add Codegg-side affinity merely to make the option appear implemented.
First decide the intended Codegg behavior.

Preferred minimal option for this corrective pass:

- Codegg continues to use shared deterministic compilation and session identity
  primitives;
- Codegg does **not** implement EggPool's process-owned async affinity cache;
- Codegg docs/config comments explicitly state that shared `sticky` /
  `affinity_ttl_s` fields are not active Codegg affinity behavior, or Codegg
  rejects/ignores them explicitly with documented semantics.

Choose one behavior and make it unambiguous.

If the existing Codegg config schema exposes these fields publicly, prefer a
backward-compatible clarification over removing them abruptly. Do not introduce
Tokio or cache state into `eggpool-model-routing` for this pass.

## Phase 5 — Focused tests

Add tests in Codegg that prove the ownership boundary.

At minimum cover:

1. **same-provider route succeeds**
   - selected direct provider and resolved route share the same provider family;
   - concrete model is applied;
   - provider selection is unchanged.

2. **EggPool endpoint route succeeds without local provider switching**
   - selected connection is EggPool;
   - semantic route resolves to a model accepted through EggPool;
   - Codegg keeps the EggPool provider object/connection;
   - only the model target changes.

3. **cross-provider direct route fails closed**
   - selected direct OpenAI connection;
   - semantic route resolves to Anthropic;
   - no registry provider replacement occurs;
   - typed error is returned before a concrete target request starts.

4. **selector provider differs from target provider**
   - selector call may use its configured selector provider;
   - returned route does not mutate durable/current execution provider.

5. **invalid selector output still defaults safely**
   - arbitrary provider/model text is rejected as a route ID;
   - default route is chosen only if compatible with selected connection;
   - incompatible default fails closed.

6. **cancellation propagation remains unchanged**
   - cancelled selector does not start default concrete work.

7. **concrete model bypass remains unchanged**
   - explicit concrete model requests do not enter semantic selection.

8. **no semantic reselection after concrete start**
   - a simulated target transport failure does not pick another semantic route.

9. **provider selection identity unchanged**
   - where Codegg exposes provider/session connection IDs, assert the value is
     identical before and after semantic resolution.

10. **sticky semantics are explicit**
    - whichever Codegg behavior is selected in Phase 4 is covered by a focused
      test/config validation case.

Retain the existing exact shared policy/fingerprint/identity parity vector tests.

## Phase 6 — Documentation correction

Update Codegg architecture/config documentation so the behavior is described in
terms of two independent decisions:

```text
Codegg selected provider connection
        |
        +-- semantic model policy chooses a compatible concrete model
        |
        +-- request goes through the already selected connection
                |
                +-- if that connection is EggPool, EggPool performs
                    account/provider routing behind the endpoint
```

Document that semantic model routing is not a provider failover mechanism.

If direct cross-provider semantic routing is desired in a future release, it
must be designed explicitly around Codegg's durable provider-connection model
rather than emerging from route strings.

Also document Codegg's actual affinity behavior from Phase 4.

## Phase 7 — Verify the shared crate and EggPool remain untouched

This corrective pass should normally require no behavioral change in EggPool's
shared crate.

Verify:

- `rust/crates/eggpool-model-routing` remains edition 2021 / Rust 1.81;
- default dependency graph remains only `sha2`;
- protocol version and canonical policy bytes/fingerprint do not change;
- EggPool provider/account router remains outside the shared crate;
- EggPool package/update/release transition behavior remains unchanged;
- Codegg retains an immutable Git revision for the shared crate.

If no EggPool code change is required, do not manufacture one.

## Suggested implementation locations in Codegg

Likely touch points:

- `src/agent/request_preparation.rs`
- `src/agent/semantic_router.rs`
- provider/session selection helpers in `codegg-core` if an existing typed
  compatibility boundary can be reused
- focused unit/integration tests near semantic routing
- `architecture/agent.md`
- `AGENTS.md` or config documentation only where needed

Avoid changes to `crates/codegg-providers/src/eggpool.rs` unless a tiny capability
predicate is genuinely required to identify that the selected connection is an
EggPool/OpenAI-compatible aggregation endpoint.

Do not duplicate EggPool model catalog or account-selection logic in Codegg.

## Verification

Run Codegg's focused checks first, then the normal repository verification that
is practical for the current workspace.

Suggested minimum:

```bash
cargo fmt --all -- --check
cargo test -p codegg-core model_routing -- --nocapture
cargo test semantic_router -- --nocapture
cargo test request_preparation -- --nocapture
cargo check --locked
```

If the repository's documented quick-verification command exists, run it as the
primary handoff check as well.

The pre-existing whole-workspace Cargo 1.81 lockfile limitation documented in
Plan 176 is not part of this corrective pass. Do not expand scope to re-resolve
unrelated edition-2024 transitive dependencies. The shared crate's standalone
Rust 1.81 qualification remains the relevant MSRV invariant.

For EggPool, if no code changes are made, a full re-release qualification is not
required. Confirm the pinned revision and shared crate metadata by inspection.
If EggPool source is changed unexpectedly, rerun the Plan 176 verification set.

## Non-goals

Do not:

- redesign Codegg provider/session persistence;
- add automatic cross-provider failover;
- move EggPool's stateful provider/account router into the shared crate;
- publish `eggpool-model-routing` to crates.io;
- add permanent cross-repository CI coupling;
- add shared async affinity/cache state unless a separate requirement justifies
  it;
- reopen Plans 168–172 dependency/release cleanup;
- change EggPool exact-version rollback/update behavior;
- refactor unrelated Codegg model-profile or heuristic `ModelRouter` behavior;
- consolidate all routing concepts into one generalized abstraction.

## Handoff deliverables

The implementation handoff is complete when the implementing agent provides:

- the Codegg commit(s) containing the corrective behavior;
- focused test evidence for same-provider, EggPool endpoint, and incompatible
  cross-provider cases;
- confirmation that semantic resolution does not mutate provider-connection
  selection;
- documentation of actual Codegg sticky/affinity semantics;
- confirmation that the shared crate pin remains immutable;
- confirmation that EggPool's shared crate/provider router were not broadened.

Append the implementation commit IDs and verification evidence to this plan or a
small closure follow-up after the Codegg changes land.

## Definition of done

This corrective pass is complete when Codegg semantic routing can choose only a
model compatible with the provider connection already selected for execution,
an incompatible cross-provider semantic route fails closed instead of silently
replacing the execution provider, selector execution remains side-effect free
with respect to session/provider ownership, Codegg's affinity semantics are
unambiguous, all existing shared deterministic parity vectors still pass, and
EggPool's provider/account routing and shared-crate boundaries remain unchanged.

## Execution record

The Codegg corrective implementation landed in
`8d4082bba3906fc0eb9b1dd8e1dda03dbbfa8dfa` (`fix: preserve provider ownership
during semantic routing`) and was pushed to `dbowm91/codegg` `main`.

- `cargo fmt --all -- --check`: passed.
- `cargo check --workspace --all-targets --locked`: passed in the clean
  committed checkout.
- `scripts/verify.sh quick`: passed.
- strict Clippy: passed.
- focused `semantic_router`: 11 passed with `LZMA_API_STATIC=1` on this
  macOS host.
- focused `request_preparation`: 3 passed with `LZMA_API_STATIC=1`.
- the shared EggPool model-routing crate standalone check and tests passed;
  its Rust 1.81 metadata and direct `sha2` dependency are unchanged.
- Codegg's `sticky` and `affinity_ttl_s` settings are documented as policy and
  fingerprint compatibility inputs only; Codegg does not implement EggPool's
  process-owned asynchronous affinity cache.
- the shared crate pin, EggPool provider/account router, and EggPool runtime
  were not broadened or changed.
- Codegg CI run `34695979013` is still in progress; the local full suite is
  also still running after reaching the workspace test phase.
