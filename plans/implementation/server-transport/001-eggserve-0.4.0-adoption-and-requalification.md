# Server Transport Milestone 001 — EggServe 0.4.0 adoption and requalification

Status: ready

Repository baseline: `777dcdd597b5606997df99ffe9191691ba6785fb`

Source roadmap:

- `plans/subsystems/server-transport-roadmap.md#milestone-001--eggserve-040-adoption-and-requalification`

Long-term requirements:

- `plans/000-long-term-specification.md` — thin server adapters; EggPool retains application policy/process lifecycle.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — preserve the Plan-250 transport posture across dependency bumps.
- `plans/003-planning-process.md` — dependency/closure evidence requirements.

Applicable ADRs:

- None required. The durable EggServe direct-H1/Tower dependency was already selected and closed by Plans 246–250; this milestone requalifies a published version of the same owner/protocol boundary.

Primary class: infrastructure

## 1. Objective

Upgrade EggPool from exact-pinned `eggserve-server =0.3.0` to published `=0.4.0` and requalify the downstream HTTP/1 transport without changing application admission, runtime configuration, ownership, or process-lifecycle semantics.

Expected production code change is the manifest/lockfile update. Source edits beyond dependency-version documentation and focused regression coverage must be justified by actual 0.4.0 compatibility evidence.

## 2. Why this milestone is ready

Hard dependencies are closed: Plans 246–250 established and qualified the direct server/Tower integration.

The crates.io index is live and non-yanked for:

- `eggserve-server 0.4.0`, published 2026-09-25T19:23:22Z, checksum `fb601019a2914ae99f264640b66c80496a67b7ea3315fd0809747d4f22327e40`;
- `eggserve-primitives 0.2.2`, published 2026-09-25T19:22:37Z, checksum `78fd797e45a374bfa419bc75f7e497653ce25e24cde0e771cc332e267f18355c`.

The published server depends on primitives `^0.2.2` and exposes the `tower` feature as `http-interop + tower-service`; it no longer activates the optional `tower-layer` dependency itself.

No new server protocol, dependency owner, or public EggPool contract is being selected, so the ADR threshold is not crossed.

## 3. Current implementation evidence

At the baseline:

- `rust/Cargo.toml`:
  - exact-pins `eggserve-server =0.3.0`;
  - disables default features;
  - enables only `tower`;
  - has no direct `eggserve-primitives`, `eggserve-core`, or `eggserve-static` dependency.
- `rust/Cargo.lock` resolves server `0.3.0` and primitives `0.2.1`.
- `rust/src/server/mod.rs::ServerRuntime::serve_listener` uses:
  - `eggserve_server::Server::builder()`;
  - the caller-bound Tokio `TcpListener`;
  - `TowerToEggserve::with_policy`;
  - `RequestBodyPolicy::Stream { max_bytes: EGG_SERVE_REQUEST_BODY_LIMIT }`;
  - `ServerHandle::into_parts()` and passive completion;
  - EggPool quiesce → `control.shutdown()` → await completion → EggPool resource close.
- `eggserve_runtime_config()` explicitly preserves:
  - 1024 connections / 1024 in-flight requests;
  - 1 GiB transport body ceiling;
  - 256 KiB buffer;
  - 256 headers / 128 KiB aggregate header / 16 KiB target;
  - total lifetime disabled;
  - 24 h handler/body bounds;
  - 15 s header, 120 s keep-alive, 120 s response-write, 5 s EggServe graceful shutdown.
- `rust/tests/server_transport.rs` already contains raw loopback coverage for:
  - HTTP/1.0 + HTTP/1.1 health;
  - authenticated/unauthenticated application routes;
  - keep-alive reuse;
  - malformed/oversized targets and headers;
  - incomplete and over-limit request bodies;
  - both Content-Length and chunked live body limits;
  - client upload disconnect;
  - finite Responses;
  - streaming first-event behavior and stalled-reader shutdown;
  - compact routing/admission;
  - bounded transport shutdown.
- `rust/tests/runtime_lifecycle_r009.rs` covers database-last process shutdown and bounded forced-close behavior.
- EggPool currently resolves `tower 0.5.3` and Axum `0.8.9`, both of which already depend on `tower-layer 0.3.3`. Therefore EggServe 0.4.0's removal of its own `tower-layer` feature activation is not expected to remove that crate from EggPool's total graph.
- Upstream 0.4.0 includes transparent direct-Tower changes relevant to this path:
  - exact-size response bodies can retain known-length framing;
  - already-end-streamed request bodies avoid unnecessary keep-alive close under Stream policy;
  - H1 response trailers are declaration-aware and runtime-owned.
  EggPool currently declares no response trailers, so trailer support should remain dormant.

## 4. Invariants that must not regress

- EggServe remains downstream H1 transport/framing/drain authority only.
- EggPool's Axum router remains application authority.
- `rust/src/server/` remains free of coordinator retry/finalization logic.
- Generation-owned live body admission remains authoritative for all four inference routes.
- The 1 GiB EggServe transport ceiling and 1 GiB Tower Stream ceiling remain finite and unchanged.
- Existing EggServe runtime-limit values and default ownership modes remain unchanged.
- Request-target mode remains origin-form behavior; no forward-proxy semantics.
- No `eggserve-core`, `eggserve-static`, or EggServe PHF ancestry returns.
- No direct `eggserve-primitives` dependency is added unless existing EggPool behavior demonstrably needs a primitive not re-exported by the server crate.
- Normal EggPool JSON/SSE responses do not acquire a `Trailer` declaration or terminal trailer section accidentally.
- EggServe child completion is awaited before process resources/database close.
- Unexpected child completion remains `ServerError::EggServe` / `ShutdownReason::ServerCompleted`.
- Default and no-default feature builds/tests remain valid.
- Credentials, prompts, raw bodies, provider data, and cache keys remain absent from diagnostics/evidence.

## 5. Scope

### In scope

- exact-pin `eggserve-server =0.4.0` in `rust/Cargo.toml`;
- normal Cargo lock resolution to `eggserve-primitives 0.2.2`;
- compile fixes only if the existing public direct-server/Tower API genuinely changed;
- focused transport regression assertions needed to make the 0.4.0 intersections explicit;
- real-socket and lifecycle requalification;
- dependency/security/no-default/release-build gates;
- before/after dependency/package/release-binary footprint evidence;
- current-authority documentation/version references;
- hierarchy closure record + registry/roadmap status.

### Explicitly out of scope

- no upstream EggServe edits;
- no `eggserve-core`/`eggserve-static`/PHF reintroduction;
- no direct H1 raw-driver rewrite;
- no H2/H3/TLS/static/tunnel/forward-proxy adoption;
- no response-trailer product capability;
- no external policy/admission ownership;
- no runtime-rejection presenter;
- no config/reload/schema/database migration;
- no provider/coordinator/routing changes;
- no physical SBC campaign unless a material regression requires a separate plan;
- no rewriting legacy Plans 244–250.

## 6. Required production changes

### Dependency metadata

Change only the existing direct dependency:

```toml
eggserve-server = { version = "=0.4.0", default-features = false, features = ["tower"] }
```

Refresh `rust/Cargo.lock` through normal Cargo resolution. Expected EggServe resolution is:

- `eggserve-server 0.4.0`;
- `eggserve-primitives 0.2.2`.

Do not add a direct primitives dependency preemptively.

The lockfile checksum for each expected registry package must match the live-index values in §2.

### Server integration

No `rust/src/server/mod.rs` production change is expected. Compile against 0.4.0 first.

If a source edit is required, keep it to a public-API compatibility adjustment that preserves exactly this lifecycle:

```text
pre-bound TcpListener
  -> eggserve Server::builder + existing RuntimeConfig
  -> TowerToEggserve(existing Axum Router, Stream 1 GiB)
  -> handle.into_parts()
  -> completion vs EggPool quiesce select
  -> control.shutdown()
  -> await completion
  -> close EggPool process resources/database
```

Do not add a compatibility facade merely to retain a 0.3 symbol path.

### Runtime configuration and admission

Keep every current `eggserve_runtime_config()` value unchanged. Do not opportunistically use new upstream ownership/configuration APIs.

Keep:

```text
EggServe hard body ceiling: 1 GiB
  -> Tower RequestBodyPolicy::Stream: 1 GiB
     -> EggPool generation-owned live request ceiling
```

### Focused 0.4.0 regression guards

Reuse `rust/tests/server_transport.rs`; do not create a parallel transport harness unless the current fixture cannot express the assertion.

Ensure coverage makes these properties explicit:

1. Two sequential bodyless HTTP/1.1 health requests reuse one keep-alive connection under the Stream adapter. Existing keep-alive coverage may satisfy this if it clearly asserts both responses.
2. A normal small JSON/health response remains standards-valid with no contradictory `Content-Length` / `Transfer-Encoding` framing. Add a narrow raw-head assertion only if current coverage does not already pin this.
3. EggPool's normal SSE path remains incremental/chunked and does not emit a `Trailer` response header or trailer section without an EggPool declaration.
4. HTTP/1.0 remains functional.
5. Malformed/over-limit client failures still isolate to the request/connection and do not poison a subsequent healthy request.
6. The existing compact and live body-admission contract remains unchanged.
7. Shutdown still joins the EggServe child before database close.

Do not add tests for declaration-aware trailers themselves; that is upstream capability and EggPool does not consume it.

### Dependency/footprint evidence

Record immediately before and after the pin change using the same host/toolchain/profile:

- `Cargo.lock` package count;
- `cargo tree -e no-dev -p eggpool` node count;
- resolved EggServe package/version set;
- release executable bytes.

Record:

```bash
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e no-dev
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e features
cargo tree --manifest-path rust/Cargo.toml -p eggpool -i eggserve-server
cargo tree --manifest-path rust/Cargo.toml -p eggpool -i tower-layer
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo tree --manifest-path rust/Cargo.toml -i eggserve-static
cargo tree --manifest-path rust/Cargo.toml -i phf
```

Absence of core/static is expected; inverse-tree absence may exit nonzero and should be recorded as expected evidence.

Do not claim `tower-layer` removal as an EggPool win: the direct Tower/Axum graph already owns it independently of EggServe.

Runtime benchmark/RSS repetition is not mandatory for this dependency-only milestone unless tests, binary footprint, or observed behavior indicate a material regression.

### Documentation

After the upgrade is qualified, update current-authority version statements in at least:

- `README.md`;
- `architecture/README.md`;
- `architecture/overview.md`;
- `AGENTS.md`;
- `.opencode/skills/architecture/SKILL.md`;
- `.opencode/skills/development/SKILL.md`.

Search current-authority docs for other `EggServe 0.3.0` / `eggserve-server =0.3.0` statements and update only statements describing the current runtime.

In `plans/000-long-term-specification.md`, de-version the unchanged invariant from “EggServe 0.3” to the “exact-pinned EggServe direct H1 runtime” (or equivalent durable wording) so future maintenance bumps do not require canonical architecture churn. This is a factual durability correction under the user-directed upgrade; it does not change ownership.

Do not edit historical Plans 244–250 to make them look current.

## 7. Ordered work packages

### Work package A — Freeze baseline and package evidence

Intent: establish a truthful 0.3.0 immediate pre-change comparison and verify the exact registry artifacts.

Required changes: none.

Acceptance evidence:

- baseline SHA/toolchain;
- current lock/package/node/binary measurements;
- live-index 0.4.0/0.2.2 versions, checksums, non-yanked state.

### Work package B — Exact-pin and lockfile upgrade

Intent: consume the published downstream package as an external user.

Required changes:

- `rust/Cargo.toml` pin 0.3.0 → 0.4.0;
- normal `Cargo.lock` resolution.

Acceptance evidence:

- lock resolves exact server 0.4.0 + primitives 0.2.2 with expected checksums;
- no git/path patch;
- no core/static dependency;
- no source edit unless compilation proves one necessary.

### Work package C — Compile/API compatibility gate

Intent: prove 0.4.0 preserves EggPool's direct integration surface before changing runtime behavior.

Required changes: only minimal public-API adaptation if unavoidable.

Acceptance evidence:

- locked debug build;
- default + no-default checks;
- `TowerToEggserve::with_policy`, `RequestBodyPolicy::Stream`, RuntimeConfig builder, handle split/control/completion lifecycle compile as the same ownership shape.

If compilation requires a direct primitives dependency, core/static fallback, lifecycle ownership change, or broad adapter rewrite, stop and reassess instead of continuing.

### Work package D — Focused real-socket qualification

Intent: requalify the downstream effects of upstream 0.4 changes.

Required changes: narrow assertions in existing transport fixtures where needed.

Acceptance evidence:

```bash
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
```

Plus explicit evidence for keep-alive, valid finite framing, SSE without accidental trailers, body-limit isolation, compact, and bounded child shutdown.

### Work package E — Application/wire regression qualification

Intent: prove the transport bump does not alter EggPool-owned application semantics.

Acceptance evidence:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
```

Use exact current target names if they change before execution.

### Work package F — Graph/security/footprint qualification

Intent: prove dependency hygiene and characterize the actual downstream footprint.

Acceptance evidence:

- graph/inverse-tree commands from §6;
- `cargo deny` clean;
- no EggServe core/static/PHF ancestry;
- before/after package/node/release-binary measurements;
- `tower-layer` ancestry correctly attributed to EggPool/Axum/Tower if retained;
- no unexplained new dependency family.

### Work package G — Full repository gates and hosted CI

Intent: close on repository authority, not focused tests alone.

Acceptance evidence: all §11 commands green and hosted CI/dependency audit green on the exact closure candidate.

### Work package H — Current-authority docs and closure

Intent: make the repository describe the shipped 0.4.0 boundary truthfully.

Required changes: current-authority docs in §6; durable de-versioning of the canonical transport invariant; no historical-plan rewrite.

Acceptance evidence:

- doc search finds no stale current-state 0.3.0 claims;
- `plans/closure/server-transport/001-status.md` records exact resolution, tests/CI, graph, footprint, residuals;
- roadmap/registry move M001 to the evidence-supported disposition.

## 8. Failure, cancellation, restart, contention semantics

No new runtime state machine is introduced. Existing behavior remains authoritative:

- malformed/incomplete/over-limit requests fail without corrupting later traffic;
- bodyless keep-alive requests may reuse the connection;
- downstream disconnect cancels the active response/body owner through existing paths;
- EggPool quiesce requests EggServe shutdown and awaits terminal completion;
- active generation/body tasks follow existing bounded process shutdown;
- database close remains last;
- unexpected EggServe terminal completion is surfaced as an EggPool error.

Any observed change to these semantics is a migration defect, not a reason to adjust the contract opportunistically.

## 9. Compatibility and migration

No config/schema/data migration.

Client HTTP/API compatibility should be unchanged. The dependency migration is:

```text
eggserve-server 0.3.0 / primitives 0.2.1
    ->
eggserve-server 0.4.0 / primitives 0.2.2
```

Existing EggPool source should remain source-compatible at the direct API it uses. New upstream declaration-aware response-trailer APIs are not adopted.

Rollback before an EggPool release remains a normal exact-pin/lockfile revert to 0.3.0 if 0.4.0 qualification fails. Do not yank or alter upstream packages.

## 10. Required tests

Focused unit/integration:

- existing server module/unit tests affected by compilation;
- `server_transport`;
- `health`;
- `runtime_lifecycle_r009`.

Protocol/compat:

- HTTP/1.0 + HTTP/1.1;
- keep-alive bodyless requests;
- finite JSON/framing;
- SSE incremental/chunked/no unsolicited trailers;
- malformed/oversized target/header;
- Content-Length + chunked application body ceilings;
- compact route;
- auth boundaries.

Failure/cancellation/recovery:

- incomplete body/disconnect;
- subsequent healthy request after rejection;
- stalled reader;
- requested and forced shutdown;
- database-last close.

Application regression:

- coordinator C008/C009/C011 + boundaries/finalization/publication;
- wire stream/runtime/qualification;
- Codex Responses and compact compatibility.

Dependency/security:

- default/no-default Clippy/tests;
- `cargo deny`;
- locked release build;
- feature/duplicate/no-dev/inverse dependency trees.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1

cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1

cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1

cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e no-dev
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo tree --manifest-path rust/Cargo.toml -i eggserve-static
cargo tree --manifest-path rust/Cargo.toml -i phf
cargo tree --manifest-path rust/Cargo.toml -p eggpool -i tower-layer

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Require hosted CI and the dependency-audit workflow to pass on the exact implementation/closure candidate SHA.

## 12. Documentation updates

Update the current shipped-version/boundary statements listed in §6.

Historical Plan 250 remains the closure evidence for the 0.3.0 migration and must not be rewritten.

The canonical specification's transport invariant should become version-agnostic while retaining exact-pin policy, so future dependency maintenance does not create false architecture changes.

## 13. Acceptance criteria

- `rust/Cargo.toml` exact-pins `eggserve-server =0.4.0` with `default-features = false, features = ["tower"]`.
- `rust/Cargo.lock` resolves server 0.4.0 and primitives 0.2.2 with live-index checksums.
- No direct primitives dependency is introduced without a demonstrated existing-path need.
- Existing `Server`, Tower adapter, Stream policy, RuntimeConfig, control/completion lifecycle remain the same ownership shape.
- All current runtime-config values and the 1 GiB transport/Tower ceiling remain unchanged.
- Generation-owned live request admission remains authoritative for every inference route.
- Bodyless HTTP/1.1 keep-alive reuse is green under the Stream adapter.
- Finite response framing is valid and SSE remains incremental/chunked without unsolicited response trailers.
- HTTP/1.0 remains functional.
- Malformed/oversized/incomplete requests remain isolated; later healthy requests work.
- Compact and ordinary inference real-socket paths remain green.
- EggServe child shutdown remains bounded and joined before database close.
- Production graph contains no `eggserve-core` or `eggserve-static` and no EggServe PHF ancestry.
- Retained `tower-layer` is attributed truthfully to EggPool/Axum/Tower rather than reported as an EggServe regression.
- Before/after package count, no-dev node count, and release-binary size are recorded.
- Default/no-default Clippy/tests, locked builds, Cargo deny, feature/duplicate trees, tooling checks, and exact-SHA hosted CI pass.
- Current-authority docs describe 0.4.0; the canonical invariant is de-versioned; historical plans remain unchanged.
- No medium+ unresolved integration defect remains.

## 14. Stop conditions

Stop and report rather than improvise if:

- `eggserve-server 0.4.0` or required primitives are yanked/unavailable/checksum-inconsistent;
- the direct 0.4 API no longer supports the current passive control/completion lifecycle;
- compilation requires reintroducing `eggserve-core`/`eggserve-static` or copying an adapter into EggPool;
- existing behavior requires a new direct `eggserve-primitives` dependency with unclear ownership;
- runtime configuration/body/admission ownership must change to make the upgrade work;
- real-socket keep-alive/body-limit/shutdown/auth semantics regress;
- no-default parity breaks;
- an unexplained dependency family or material binary/resource regression appears;
- solving an upstream defect would expand this milestone into EggServe source work.

In those cases, preserve the 0.3.0 pin and write the narrow corrective/upstream plan instead.

## 15. Closure evidence required

Create `plans/closure/server-transport/001-status.md` containing:

- implementation SHA and exact baseline;
- live-index server/primitives versions, checksums, and final lock resolution;
- any source compatibility change (or explicit “none”);
- focused raw-socket results for keep-alive, finite framing, SSE/no-trailer, body limits, compact, malformed-client isolation, and shutdown;
- exact commands/results for default/no-default/workspace/tooling/security gates;
- hosted CI + dependency-audit run IDs/conclusions for the exact candidate;
- before/after package count, no-dev node count, release executable bytes, EggServe package set;
- inverse-tree evidence for core/static/PHF and attribution of `tower-layer`;
- docs updated and confirmation legacy Plans 244–250 stayed unchanged;
- severity-tagged residuals;
- final roadmap/registry disposition.

## 16. Handoff notes

This is a downstream dependency maintenance pass. Compile the published package before changing source. The likely correct implementation is a two-version manifest/lockfile resolution change plus narrow test/doc updates.

Do not turn upstream 0.4 features into new EggPool capability. In particular, do not add response trailers, external policy ownership, or new server configuration simply because the APIs now exist.

Tests are serial. Preserve unrelated user changes.
