# Server Transport Roadmap

Status: closing

Long-term references:

- `plans/000-long-term-specification.md` — thin server adapters and EggPool-owned application/process policy.
- `plans/001-terminology-and-domain-model.md` — application admission and lifecycle vocabulary.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — exact-pin dependency requalification.
- `plans/003-planning-process.md` — dependency and closure rules.

Related ADRs:

- None required for Milestone 001. Plans 246–250 already selected the durable EggServe direct-H1/Tower boundary; this roadmap maintains that dependency rather than selecting a new server protocol or owner.

## 1. Purpose and ownership boundary

This subsystem owns EggPool's downstream HTTP/1 transport integration boundary: the exact-pinned EggServe runtime, its Tower adapter into the existing Axum router, the transport-level hard limits supplied by `rust/src/server/mod.rs`, and the lifecycle contract by which the EggServe child is shut down and joined before EggPool closes shared process resources.

It consumes:

- EggServe for HTTP/1 parsing, connection transport, framing, keep-alive, and connection drain;
- Axum/Tower for EggPool's HTTP application router;
- EggPool runtime lifecycle for process quiesce, generation drain, task drain, control shutdown, and database close.

It must not own application routing/retry/finalization, provider transport, generation-owned request admission, authentication policy, SQLite lifecycle, or provider/application timeout policy.

## 2. Work classification

### Invariants

- `rust/src/server/` remains a thin HTTP adapter; coordinator retry/finalization ownership does not move into the server.
- EggServe drives only the pre-bound downstream H1 listener and connection lifecycle.
- EggPool retains authentication, generation-owned body limits, endpoint/coordinator behavior, and process lifecycle.
- The transport/Tower hard body ceiling remains 1 GiB while the generation-owned live application ceiling remains authoritative below it.
- EggServe completion/control is joined before EggPool closes process resources/database.
- Unexpected EggServe completion remains a typed EggPool server failure.
- Direct server consumption stays free of `eggserve-core` and `eggserve-static`; no static-serving/PHF branch is reintroduced.
- Default and `--no-default-features` EggPool builds remain qualified.

### Capabilities

- HTTP/1.0 and HTTP/1.1 downstream service.
- Health/dashboard/inference/compact routes through the existing Axum router.
- Finite and streaming downstream responses.
- Keep-alive and bounded graceful shutdown.

### Infrastructure

- Exact-pinned `eggserve-server` dependency with `default-features = false, features = ["tower"]`.
- `TowerToEggserve` + `RequestBodyPolicy::Stream` bridge.
- EggServe `RuntimeConfig` projection and `ServerControl`/`ServerCompletion` lifecycle split.
- Real-socket transport regression coverage in `rust/tests/server_transport.rs`.

### Polish

- Dependency/feature graph minimization.
- Current-version documentation and dependency evidence.
- Binary/package footprint characterization after upstream version bumps.

## 3. Non-goals

- No `eggserve-core` or `eggserve-static` adoption.
- No HTTP/2, HTTP/3, TLS, forward-proxy, tunnel, or static-file capability migration.
- No external EggServe policy/admission ownership.
- No custom runtime rejection presenter.
- No provider transport/routing/coordinator redesign.
- No direct `eggserve-primitives` dependency unless an EggPool-owned source requirement proves it necessary.
- No performance campaign unless a material downstream regression is observed.
- No upstream EggServe source changes inside this subsystem; upstream defects get a separate upstream plan.

## 4. Current state

At baseline `777dcdd597b5606997df99ffe9191691ba6785fb`:

- `rust/Cargo.toml` exact-pins `eggserve-server =0.3.0` with only the `tower` feature.
- `rust/Cargo.lock` resolves `eggserve-server 0.3.0` and `eggserve-primitives 0.2.1`.
- `rust/src/server/mod.rs` builds EggServe's direct `Server`, adapts the Axum router with `TowerToEggserve::with_policy(... RequestBodyPolicy::Stream { max_bytes: 1 GiB })`, splits the running handle into control/completion, and joins it before process-resource closure.
- Plan 250 removed `eggserve-core`, `eggserve-static`, and EggServe-owned PHF ancestry and qualified real-socket finite/streaming/body-limit/shutdown behavior.
- `rust/tests/server_transport.rs` covers HTTP/1.0/1.1, keep-alive, auth, malformed targets/headers, Content-Length/chunked body limits, disconnects, finite/streaming Responses, compact, and bounded shutdown. `runtime_lifecycle_r009` covers process close ordering.
- The live crates.io index now contains non-yanked `eggserve-server 0.4.0` (published 2026-09-25T19:23:22Z, checksum `fb601019a2914ae99f264640b66c80496a67b7ea3315fd0809747d4f22327e40`) requiring `eggserve-primitives ^0.2.2`, and non-yanked `eggserve-primitives 0.2.2` (published 2026-09-25T19:22:37Z, checksum `78fd797e45a374bfa419bc75f7e497653ce25e24cde0e771cc332e267f18355c`).
- EggServe 0.4.0 keeps the direct `tower` feature but no longer activates its optional `tower-layer` edge. EggPool already resolves `tower-layer 0.3.3` through its own `tower 0.5.3`/Axum graph, so removal of that upstream feature edge is not expected by itself to remove `tower-layer` from EggPool's complete graph.
- Upstream 0.4.0 cumulatively includes the direct-Tower known-length framing improvement, bodyless Stream-policy keep-alive correction, and declaration-aware H1 response-trailer repair. EggPool's current application responses do not intentionally declare response trailers, so no trailer API adoption is required for the dependency bump.

## 5. Target architecture

```text
caller-bound TcpListener
    |
    v
eggserve-server =0.4.0
  default-features = false
  features = ["tower"]
    |
    v
TowerToEggserve
  RequestBodyPolicy::Stream { hard ceiling = 1 GiB }
    |
    v
EggPool Axum Router
    |
    +--> authentication + generation-owned live body admission
    +--> coordinator/inference lifecycle
    +--> dashboard/health/control projections

EggPool quiesce
    -> EggServe ServerControl::shutdown
    -> await ServerCompletion
    -> close EggPool control/tasks/generations/database
```

The version changes; the ownership diagram does not.

## 6. Dependency graph

```text
Plans 246–250 direct H1/Tower adoption (closed)
    |
    +-- hard --> published eggserve-server 0.4.0 / primitives 0.2.2
    |             (available)
    |
    `--> Milestone 001 — exact-pin upgrade + downstream requalification
```

Milestone 001 has no unresolved hard/interface dependency. Crates.io availability is an operational dependency already satisfied at roadmap creation.

## 7. Milestones

### Milestone 001 — EggServe 0.4.0 adoption and requalification

Class: infrastructure

Objective: move EggPool's existing direct-Tower downstream transport from exact-pinned `eggserve-server 0.3.0` to published `0.4.0` while preserving the Plan-250 ownership/config/admission/lifecycle contract and requalifying real-socket, feature-graph, security, no-default, and footprint evidence.

Dependencies:

- Plans 246–250 transport adoption: hard, closed.
- Published `eggserve-server 0.4.0` / `eggserve-primitives 0.2.2`: operational, satisfied.

Deliverable boundary:

- exact manifest/lockfile upgrade;
- only compatibility source changes proven necessary by compilation/tests;
- focused 0.4 framing/keep-alive/non-trailer guards in the existing transport suite where current coverage does not make them explicit;
- dependency/footprint evidence;
- current-authority documentation refresh;
- closure record.

User or operator value: EggPool receives upstream direct-H1 correctness/performance maintenance without acquiring new server ownership, static dependencies, or application behavior.

Exit conditions:

- exact `0.4.0` pin and `0.2.2` transitive resolution;
- current direct APIs compile without a compatibility facade;
- Plan-250 runtime configuration and 1 GiB transport/Tower ceiling remain unchanged;
- real-socket and process-lifecycle suites pass, including keep-alive, finite/streaming, compact, malformed-input isolation, and bounded shutdown;
- normal EggPool responses do not accidentally advertise H1 trailers;
- no EggServe core/static/PHF ancestry returns;
- default/no-default, locked release, Cargo-deny, feature/duplicate-tree, tooling, and exact-SHA hosted CI gates pass;
- package/node/release-binary footprint delta is recorded;
- closure record accepted.

Deferred work: adopting declaration-aware H1 response trailers in EggPool is unnecessary unless an EggPool endpoint gains a concrete trailer contract; that requires separate capability planning.

## 8. Cross-cutting requirements

Storage/migration: none.

Protocol/compatibility: HTTP/1.0/1.1 behavior and public EggPool routes remain unchanged. The dependency bump must not alter client authentication, OpenAI-compatible wire surfaces, or compact semantics.

Security/auth: malformed request isolation, parser/header/request-target ceilings, auth boundaries, and request-body DoS ceilings stay intact. No new secrets or diagnostics.

Concurrency/cancellation/recovery: preserve keep-alive reuse, downstream disconnect cancellation, graceful child drain, and database-last shutdown ordering.

Observability: no new logs/metrics required. Dependency and closure evidence remain secret-free.

Performance/resources: record dependency/package and release-binary footprint. Do not claim a binary reduction from EggServe's `tower-layer` feature change because EggPool's own Tower/Axum graph already needs that crate. Broader runtime benchmarking is only required if focused tests or footprint evidence show a material regression.

Docs/ops: current-authority docs must identify the new exact pin without rewriting historical Plans 244–250.

## 9. Verification strategy

Use the existing real-socket `server_transport` suite as the transport authority, `runtime_lifecycle_r009` for process shutdown ordering, and the standard repository default/no-default gates. Dependency changes additionally require Cargo deny, locked release build, feature and duplicate trees, and explicit inverse ancestry checks for EggServe core/static/PHF.

Qualify upstream-behavior intersections that matter to EggPool:

- bodyless health keep-alive under the Stream policy;
- ordinary known-length JSON/health response framing remains valid;
- SSE/streaming stays incremental and trailer-free unless EggPool explicitly declares trailers;
- body limits and malformed-client failures do not poison later requests;
- child shutdown remains bounded and joined.

Rust tests run serial with `--test-threads=1`.

## 10. Risks and decision points

- A 0.x minor can contain source/feature changes. Do not paper over incompatibility with a local compatibility facade or reintroduce core/static.
- The upstream Tower feature loses a `tower-layer` activation edge, but EggPool already needs `tower-layer`; dependency-count expectations must be based on actual Cargo trees.
- New H1 response-trailer support is opt-in/declaration-driven. EggPool should not adopt it speculatively.
- If 0.4.0 changes the current server lifecycle/config ownership or requires a direct primitives dependency for existing behavior, stop and reassess rather than silently enlarging the migration.
- Any material binary/resource regression should be characterized before closure; a target-class/SBC campaign is separate unless evidence warrants it.

## 11. Completion definition

Milestone 001 closes when EggPool consumes the published 0.4.0 direct server package from crates.io with the same application/process ownership as Plan 250, all required transport/security/no-default/dependency gates are green, footprint effects are recorded truthfully, current docs are updated, and no medium+ unresolved integration defect remains.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 | closing | `plans/implementation/server-transport/001-eggserve-0.4.0-adoption-and-requalification.md` | — | — |
