# Plan 175 — Codegg Consumption of Shared Semantic Model Routing

Date: 2026-09-11
Status: ready for handoff
Parent roadmap: `plans/173-shared-model-routing-crate-roadmap.md`
Depends on: `plans/174-model-routing-core-extraction.md`
Priority: P1 cross-repository integration / routing ownership
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Objective

Consume `eggpool-model-routing` from Codegg as the common semantic model-policy implementation without moving EggPool provider/account routing into Codegg and without weakening Codegg's explicit provider-connection/session ownership.

This plan documents the downstream implementation contract in EggPool because the crate is owned here. The actual Codegg changes should be implemented in `dbowm91/codegg` and may receive Codegg-local handoff plans if that repository's workflow requires them.

## Existing Codegg boundaries to preserve

Codegg already has two relevant but distinct components:

1. `crates/codegg-providers/src/eggpool.rs` is a bounded EggPool `/models` endpoint probe. It validates/normalizes an endpoint, performs a bounded model-catalog request, redacts errors and credentials, and computes a stable digest. It is not semantic routing and should not be replaced by the shared crate.

2. `src/core/session_selection.rs` owns durable session -> provider-connection/model selection. Its explicit invariant is that a missing/disabled/stale connection never causes Codegg to silently choose a different credentialed endpoint. This remains authoritative.

The new crate therefore belongs above concrete provider execution and below Codegg-specific orchestration/configuration. It must never mutate a session's provider connection as a side effect of semantic route selection.

## 1. Pin an immutable shared-crate revision

After Plan 174 lands and is green, add a Git dependency pinned to an immutable EggPool commit/tag rather than `branch = "main"`.

Prefer the narrowest Codegg crate that owns semantic routing. Do not automatically put the dependency in the root `codegg` package if a smaller ownership location is appropriate.

Likely choices:

- `codegg-core` if semantic policy becomes daemon/session orchestration state;
- `codegg-config` only for neutral config translation if doing so does not create an undesirable dependency direction;
- a small Codegg-side routing module that depends on both config and the shared crate.

Avoid putting semantic routing into `codegg-providers`: provider implementations should execute concrete model requests, not decide application policy.

The dependency must compile under Codegg's declared Rust 1.81 baseline. Do not raise Codegg's `rust-version` as part of this integration.

## 2. Define a Codegg-facing configuration adapter

Do not expose EggPool TOML structures in Codegg. If Codegg offers semantic routing configuration, define it in Codegg's existing configuration system and translate it into `eggpool_model_routing::ModelRouterPolicy` (or the final neutral type name).

The Codegg schema may reuse the same concepts where useful:

- virtual/role alias;
- concrete selector model;
- concrete default model;
- labeled concrete routes and descriptions;
- sticky/session behavior;
- selector timeout/input bounds/repair count.

Do not force identical file syntax if Codegg's configuration conventions differ. Shared semantics matter; shared application config does not.

Validation should be layered:

```text
Codegg config parsing / local schema
        -> neutral shared policy validation/compilation
        -> Codegg-specific provider/model availability checks at runtime
```

Do not duplicate the shared byte bounds, route ordering, default-membership or route-ID rules in Codegg.

## 3. Keep durable provider connection selection separate

A semantic route should resolve to a concrete model reference within the already selected/allowed provider context; it must not be interpreted as permission to silently switch provider connections.

For sessions with an explicit `ProviderConnectionId`, the integration must preserve the existing fail-closed lifecycle semantics:

- stale connection revision remains stale;
- missing credentials remain a typed diagnostic;
- disabled/deleted connections remain non-selectable;
- unknown model/catalog revision remains explicit;
- semantic routing does not search for a different credentialed connection to make the route succeed.

If Codegg intentionally supports an EggPool connection as the selected provider connection, the concrete model chosen by the semantic router may be sent to that EggPool endpoint; EggPool then remains responsible for its own internal provider/account routing behind that endpoint.

That produces the intended two-level topology:

```text
Codegg semantic policy -> concrete model
Codegg selected connection -> EggPool endpoint
EggPool account router -> actual upstream account/provider
```

Do not collapse these levels.

## 4. Implement selector execution through Codegg's existing provider abstraction

The shared crate does not perform inference. Codegg must execute the selector model through its existing provider/request machinery so authentication, streaming/non-streaming semantics, cancellation, telemetry and provider behavior remain Codegg-owned.

Use a bounded non-streaming selector call with the compiled static policy and bounded semantic request representation. Reuse the shared exact route-ID validator for the result.

Required failure semantics should match the shared policy contract where applicable:

- valid route ID -> concrete route;
- successful but invalid route text -> optional one bounded repair if configured;
- selector transport/provider failure -> default route;
- timeout -> default route after cancellation/cleanup;
- caller cancellation -> propagate cancellation; do not continue default work after user/session cancellation;
- once a concrete target request has begun, do not semantically switch to another target because that target's provider call failed ambiguously.

Do not import EggPool's `RequestCoordinator` or account retry logic. Codegg uses its own provider execution path.

## 5. Build semantic input from Codegg context deliberately

Codegg has richer coding-agent context than a generic OpenAI proxy, but the selector input must remain bounded and stable. Do not send the full tool schema, full repository context, large command outputs, binary/image payloads, or an entire growing transcript merely because they are available.

Prefer a compact view derived from information that helps classify the task, for example:

- system/manager intent category where already available;
- latest user instruction;
- concise indicators such as coding/research/review/test/planning mode;
- coarse presence of images or other special modalities when relevant.

Keep route descriptions and static policy first/stable to preserve prefix-cache utility. Codegg-specific semantic extraction remains Codegg-owned; only compilation and route interpretation are shared.

## 6. Use Codegg session IDs for stickiness instead of reconstructing identity when possible

Codegg already owns durable session identity. Prefer hashing a stable Codegg session identifier through the shared explicit identity primitive rather than using transcript-derived automatic identity.

This is both cheaper and semantically stronger than attempting to infer a session key from prompts.

Do not persist raw session IDs in the shared routing cache/logs. If the shared crate exposes the same hashed `SessionIdentity`, use it directly.

If Plan 174 leaves async affinity EggPool-side, Codegg may maintain a tiny Codegg-owned mapping keyed by the shared fingerprint + hashed session identity, or initially run non-sticky selection. Do not duplicate the full EggPool TTL/LRU/single-flight cache unless Codegg actually requires it; if identical affinity behavior is needed, revisit Plan 174's optional shared affinity feature rather than copying implementation.

## 7. Preserve Codegg user override semantics

Explicit user/session model selection must remain able to bypass automatic semantic routing. Semantic routing should apply only when the configured virtual/automatic routing target is selected.

Recommended precedence:

```text
explicit concrete model selected by user/session
    -> use it directly
configured semantic/virtual model selected
    -> run shared semantic policy
no selection / legacy path
    -> retain current Codegg behavior
```

Do not silently reinterpret every existing concrete model as a route alias.

## 8. Observability without duplicate infrastructure

Expose enough Codegg-side diagnostics to answer:

- requested virtual/semantic policy;
- resolved concrete model;
- route label/decision source;
- selector latency/attempt count/fallback reason.

Do not log raw selector prompts or session identifiers by default. Do not create a new SQLite subsystem solely for routing traces if existing Codegg event/session telemetry can carry bounded metadata.

EggPool metrics remain independent when EggPool is the selected provider connection. Do not attempt cross-process distributed traces in this plan.

## 9. Tests required in Codegg

Add focused tests at the relevant ownership layers:

- shared crate dependency compiles at Codegg MSRV;
- Codegg policy config translates to expected shared compiled policy/fingerprint;
- explicit concrete model bypasses semantic selector;
- virtual policy selects exact configured route only;
- invalid selector output cannot inject arbitrary model names;
- selector failure/default and cancellation semantics;
- stable Codegg session identity yields stable sticky key/selection if stickiness is implemented;
- distinct sessions do not share affinity accidentally;
- explicit provider connection is never replaced by semantic routing;
- stale/disabled/missing-credential connection diagnostics are unchanged;
- EggPool provider connection continues to use the bounded `/models` probe and normal provider implementation;
- target request failure does not invoke semantic reselection after ambiguous submission.

Keep tests deterministic; no live model/provider is required for the normal suite.

## 10. Documentation

Document semantic routing as an optional Codegg orchestration capability, not as part of provider discovery.

Make the responsibility boundary explicit:

- shared crate chooses/validates a semantic model route;
- Codegg owns sessions and provider connections;
- an EggPool connection may internally route among its own accounts after Codegg has chosen the model.

Do not tell users that Codegg and EggPool must both configure semantic routing simultaneously. In the common case, semantic routing should have one owner at a time to avoid selector-on-selector behavior.

## Acceptance criteria

- Codegg consumes `eggpool-model-routing` at an immutable revision/tag and still builds at Rust 1.81.
- No EggPool application/binary crate is pulled into Codegg.
- Shared route compilation/validation is used rather than copied.
- `codegg-providers` remains concrete provider execution/discovery code; semantic policy is not embedded there.
- Existing `ProviderConnectionId`/revision/session-selection fail-closed behavior is unchanged.
- Semantic routing never silently switches credentialed provider connections.
- Codegg executes selector inference through its own provider abstraction.
- Explicit concrete model selection bypasses semantic routing.
- Cancellation/default/invalid-output semantics are deterministic and tested.
- EggPool endpoint probing remains bounded and unchanged.
- No duplicate account/quota/fairness/circuit routing is introduced into Codegg.

## Definition of done

The integration is complete when Codegg can use the same deterministic semantic routing policy implementation as EggPool while remaining the authority for its sessions/provider connections, EggPool remains the authority for account/provider routing behind an EggPool endpoint, and neither repository contains a second copy of the shared semantic compiler/validator.