# Plan 250 — EggServe 0.3.0 direct-Tower migration, static-graph removal, and runtime requalification

Date: 2026-09-25  
Status: implementation handoff  
Planning baseline: `880d48a39ea18a7119fb27c11daf95d153be3210` (main, EggPool 0.8.0)  
Priority: P1 dependency/runtime qualification

Related:

- Plan 246 — EggServe 0.2.2 downstream transport re-entry and production cutover
- Plan 247 — EggServe transport requalification, footprint, and closure
- Plan 248 — EggServe downstream transport closure pass
- Plan 249 — Responses compact admission and generation-lease corrective
- EggServe Plan 276 — direct-server HTTP/Tower adapter extraction
- EggServe Plan 277 — direct Tower adapter publication candidate
- EggServe Plan 286 — consolidated 0.3.0 publication and registry-only embedding closure
- current downstream server boundary: `rust/src/server/mod.rs`
- dependency authority: `rust/Cargo.toml`

## Purpose

Migrate EggPool from the published EggServe 0.2 composition:

```text
eggserve-server =0.2.1
eggserve-core   =0.2.2 + tower
```

to the published direct Tower profile:

```text
eggserve-server =0.3.0 + tower
```

and remove EggPool's now-unnecessary dependency on the EggServe
compatibility/static umbrella.

The migration must preserve EggPool's current HTTP/runtime semantics while
proving that the production dependency graph no longer reaches
`eggserve-core`, `eggserve-static`, or the PHF MIME-table family through
EggServe.

This is a downstream migration and qualification plan, not a redesign of
EggPool's application-policy ownership.

## Upstream publication baseline

EggServe Plan 286 records the published registry artifacts required by this
migration:

- `eggserve-primitives 0.2.1`;
- `eggserve-server 0.3.0`;
- `eggserve-core 0.3.0`;
- `eggserve-static 0.3.0`.

The relevant upstream result is that `eggserve-server 0.3.0` now owns:

- the optional standard-HTTP interop adapter;
- `TowerToEggserve`;
- `EggserveToTower`;
- `RequestBodyPolicy` re-export;
- direct H1 runtime/configuration/supervision.

Its `tower` feature is registry-qualified with Axum and has no
`eggserve-core`, `eggserve-static`, or PHF ancestry.

EggServe's registry-only Tower fixture recorded:

```text
eggserve-server 0.3.0 + tower
eggserve-primitives 0.2.1

45 lockfile packages
41 no-dev dependency nodes
no eggserve-core
no eggserve-static
no PHF ancestry
```

Those fixture numbers are upstream evidence only. EggPool must measure its own
graph and executable.

## Current EggPool boundary

At the planning baseline, EggPool declares:

```toml
eggserve-core = { version = "=0.2.2", default-features = false, features = ["tower"] }
eggserve-server = { version = "=0.2.1", default-features = false }
```

and constructs the application bridge as:

```rust
let service = eggserve_core::server::TowerToEggserve::with_policy(
    app,
    eggserve_core::primitives::RequestBodyPolicy::Stream {
        max_bytes: EGG_SERVE_REQUEST_BODY_LIMIT,
    },
);
```

The actual listener/runtime already belongs to `eggserve-server`.

Plan 248 documented that the core dependency also resolved
`eggserve-static` and PHF-related packages even though EggPool does not use
EggServe static serving.

## Architecture decision

The target production boundary is:

```text
caller-bound TcpListener
  -> eggserve-server 0.3.0
       -> TowerToEggserve
       -> EggPool Axum Router
       -> EggPool middleware/handlers/coordinator
```

EggServe continues to own:

- HTTP/1 parsing/framing;
- connection lifecycle;
- transport-level parser ceilings;
- configured transport timeouts;
- bounded runtime/service admission;
- server shutdown/drain.

EggPool continues to own:

- API-key authentication;
- generation acquisition;
- live application request-body admission;
- inference/coordinator behavior;
- provider transport/routing;
- persistence;
- process lifecycle and outer shutdown;
- all LLM-specific policy.

Do not introduce `eggserve-core` through another path after removing the
direct dependency.

## Track A — Freeze pre-migration dependency and footprint evidence

Before changing manifests, capture the current baseline on one named
host/toolchain/profile.

At minimum record:

```bash
cargo tree --manifest-path rust/Cargo.toml -e no-dev -p eggpool
cargo tree --manifest-path rust/Cargo.toml -e features -p eggpool
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo tree --manifest-path rust/Cargo.toml -i eggserve-static
cargo tree --manifest-path rust/Cargo.toml -i phf
```

If `phf` has multiple unrelated ancestors, retain the complete inverse tree
rather than assuming EggServe is the only source.

Record:

- `Cargo.lock` package count;
- no-dev dependency-node count;
- EggServe package set;
- release executable bytes;
- startup RSS;
- idle RSS.

Use the same measurement method/profile as Plan 248 where practical so the
comparison remains interpretable.

Do not treat the Plan-248 measurements as a substitute for a fresh immediate
pre-change baseline if the dependency graph has changed since that plan.

## Track B — Cut over to the published direct Tower package

In `rust/Cargo.toml`:

1. remove `eggserve-core`;
2. exact-pin the published direct runtime:

```toml
eggserve-server = {
    version = "=0.3.0",
    default-features = false,
    features = ["tower"],
}
```

Keep the declaration compact according to repository formatting.

Do not add a direct `eggserve-primitives` dependency unless compilation shows
EggPool genuinely needs a primitive that `eggserve-server` does not expose.
The expected migration needs no direct primitives dependency.

Update `Cargo.lock` only through normal Cargo resolution.

Reject:

- git/path patches for EggServe;
- a temporary `eggserve-core 0.3.0` dependency;
- copying the Tower adapter into EggPool;
- a local compatibility wrapper whose only purpose is to retain old import
  paths.

The goal is to consume the published direct API exactly as an external
downstream.

## Track C — Rewrite only the adapter imports

Change the production bridge to the direct server-owned API:

```rust
let service = eggserve_server::TowerToEggserve::with_policy(
    app,
    eggserve_server::RequestBodyPolicy::Stream {
        max_bytes: EGG_SERVE_REQUEST_BODY_LIMIT,
    },
);
```

Using `eggserve_server::tower::TowerToEggserve` is also acceptable if the
implementation prefers explicit module qualification. Use one style
consistently.

Do not otherwise restructure `ServerRuntime::serve_listener`.

The following lifecycle shape must remain unchanged:

```text
Server::builder()
  -> caller-bound listener
  -> start_with_service()
  -> ServerHandle::into_parts()
       -> ServerControl
       -> ServerCompletion
  -> select completion vs EggPool quiesce
  -> control.shutdown()
  -> await completion
  -> only then close EggPool runtime resources/database
```

Unexpected EggServe completion must continue to become
`ShutdownReason::ServerCompleted` and an EggPool `ServerError::EggServe`.

## Track D — Preserve the current RuntimeConfig semantics exactly

EggServe 0.3.0 adds explicit policy/admission ownership controls. Do **not**
change them during the dependency cutover.

The initial migration must retain the current builder values:

```text
bind                          127.0.0.1:0 placeholder
max_connections               1024
max_in_flight_requests        1024
max_request_body_bytes        1 GiB
max_buf_size                  256 KiB
max_headers                   256
max_header_bytes              128 KiB
max_request_target_bytes      16 KiB
connection_total_timeout      disabled
handler_timeout               24 h
body_read_timeout             24 h
header_read_timeout           15 s
keep_alive_idle_timeout       120 s
response_write_timeout        120 s
graceful_shutdown_timeout     5 s
```

The builder methods used by EggPool remain present in the 0.3.0 direct API.

Also verify the new defaults remain:

```text
http1_request_target_mode     OriginOnly
policy_ownership              EggServe-owned
admission_ownership           EggServe-owned
```

Prefer a focused configuration regression test over explicitly setting every
new 0.3 knob in production code. The test should make accidental ownership
changes visible without coupling the runtime constructor to unnecessary
upstream options.

Do not enable:

- `Http1RequestTargetMode::OriginOrAbsolute`;
- external deadline/semantic-limit ownership;
- external service/tunnel admission;
- custom runtime rejection presentation.

EggPool is not a forward proxy and does not need the new absolute-form seam.

## Track E — Preserve the two-level request-body safety model

The migration must retain the current hierarchy:

```text
EggServe transport/runtime hard ceiling: 1 GiB
  -> TowerToEggserve Stream policy:       1 GiB
     -> EggPool generation-owned live application limit
        (server.max_request_body_bytes; default/configurable independently)
```

Plan 249 established that all four inference routes use the generation-owned
application limit.

Do not use EggServe 0.3's external global-body-ceiling ownership in this
migration. Removing the transport ceiling would broaden the maximum body
surface even though EggPool normally rejects much earlier.

The real-socket tests must continue to prove:

- below-live-limit inference bodies reach application logic;
- over-limit Content-Length receives EggPool's existing 413 contract;
- over-limit chunked bodies receive the same contract;
- a rejected request does not poison subsequent connections/runtime;
- compact follows the same invariant.

## Track F — Requalify the direct Tower transport path

Run the focused transport/lifecycle suites that closed Plans 248–249.

At minimum:

```bash
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1

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

Use exact current target names if they have changed since planning.

The real-socket transport suite must still cover, directly or through retained
tests:

- HTTP/1.0 and HTTP/1.1 health;
- keep-alive reuse and idle keep-alive shutdown;
- authentication rejection/success;
- EggPool dashboard static assets;
- malformed/oversized target/header isolation;
- Content-Length and chunked body-limit handling;
- client disconnect during upload;
- finite Responses;
- streaming response first event before upstream completion;
- stalled downstream reader cancellation;
- bounded EggServe child shutdown before database close;
- compact generation/body admission.

Do not replace real-socket coverage with adapter-only unit tests.

## Track G — Prove the static/core branch is gone

After the manifest/source migration, record:

```bash
cargo tree --manifest-path rust/Cargo.toml -e no-dev -p eggpool
cargo tree --manifest-path rust/Cargo.toml -e features -p eggpool
```

Required result:

- no production `eggserve-core`;
- no production `eggserve-static`;
- no EggServe-owned PHF family.

Check explicitly:

```bash
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo tree --manifest-path rust/Cargo.toml -i eggserve-static
cargo tree --manifest-path rust/Cargo.toml -i phf
```

An inverse-tree command may exit nonzero when the package is absent; absence is
the expected result for core/static.

If PHF remains because an unrelated package uses it, record
`cargo tree -i phf` and prove no EggServe ancestor exists. Do not remove an
unrelated dependency merely to make a flat grep empty.

Also verify `Cargo.lock` contains no stale EggServe core/static packages after
normal resolution unless another legitimate workspace package directly uses
them.

## Track H — Measure EggPool's actual footprint/runtime delta

Using the same host/toolchain/profile as Track A, record post-migration:

- `Cargo.lock` package count;
- no-dev dependency-node count;
- EggServe package set;
- release executable bytes;
- startup RSS;
- idle RSS.

Re-run the same local transport benchmark profile used by Plan 248 where
available and record:

- finite p50/p95;
- finite throughput;
- streaming first-byte p50/p95;
- streaming throughput;
- streaming bytes/s if still emitted by the harness.

The expected primary win is dependency/package simplification, not a guaranteed
runtime speedup. Rust dead-code elimination may make linked-size changes smaller
than dependency-graph changes.

Investigate before closure if:

- the direct dependency graph fails to shrink;
- the release executable materially grows without an explained upstream/API
  reason;
- a repeatable latency/throughput regression exceeds normal Plan-248-class
  noise;
- startup/idle RSS materially regresses.

Do not invent a hard binary-size success threshold.

A physical SBC rerun is not mandatory for this packaging-only migration if
behavior and dev-host resource measurements remain within the previously
qualified envelope. If a material regression appears, write a separate narrow
target-class qualification/corrective plan rather than expanding this plan
indefinitely.

## Track I — Evaluate, but do not opportunistically adopt, EggServe 0.3 policy ownership

After the migration is green, review whether any new 0.3 API could remove a
truly duplicate authority:

- `H1PolicyOwnership`;
- `AdmissionOwnership`;
- `RuntimeRejectionPresenter`;
- `H1ConnectionPolicy`;
- `Http1RequestTargetMode`.

For each, record one of:

- `KEEP EGGSERVE-OWNED`;
- `FOLLOW-UP CANDIDATE`;
- `NOT APPLICABLE`.

Default expected decisions for this migration:

- request-target mode: KEEP `OriginOnly`;
- parser/header limits: KEEP EggServe-owned;
- global transport body ceiling: KEEP EggServe-owned;
- body-read deadline: KEEP EggServe-owned;
- handler deadline: KEEP EggServe-owned;
- response-write progress: KEEP EggServe-owned;
- keep-alive idle: KEEP EggServe-owned;
- service-call admission: KEEP EggServe-owned at the existing 1024 ceiling;
- tunnel admission: NOT APPLICABLE to normal EggPool traffic;
- rejection presenter: NOT APPLICABLE unless a concrete mismatch is found.

The existing 24-hour handler/body deadlines are intentionally very loose so
they do not become provider/application timeout policy, but they still provide
a finite transport safety bound. Do not convert them to External merely
because 0.3.0 makes that possible.

If evidence shows one of these authorities is genuinely redundant and
removable without weakening malformed/slow-client isolation, create a new
append-only plan after Plan 250. Do not bundle that semantic change into the
dependency migration.

## Track J — Full repository qualification

After focused tests pass, run the standard repository gates:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Use the exact current repository commands if policy changes before execution.

Because this plan changes production dependency resolution, `cargo deny`,
feature-tree, duplicate-tree, locked debug build, and locked release build are
mandatory even if focused tests are green.

Require hosted CI success on the exact implementation/closure candidate before
marking the plan complete.

## Track K — Documentation and closure evidence

Update current-authority docs only where they describe the active EggServe
package boundary or exact versions.

Inspect at minimum:

- `AGENTS.md`;
- `architecture/overview.md`;
- `architecture/deep-dive-request-lifecycle.md`;
- `architecture/dependencies.md` if present;
- `README.md`;
- `.opencode/skills/architecture/SKILL.md`;
- `.opencode/skills/development/SKILL.md`.

Required current-state wording after migration:

```text
EggPool downstream HTTP:
  eggserve-server 0.3.0 + tower
  -> Axum Router
```

Do not rewrite Plans 244–249. They are historical evidence explaining the
0.2.x adoption and closure.

Record in Plan 250 closure evidence:

- implementation SHA;
- hosted CI run;
- exact resolved EggServe versions;
- dependency ancestry before/after;
- package/node counts before/after;
- release binary/RSS measurements;
- transport benchmark comparison;
- policy-ownership review dispositions;
- any known measurement limitations.

## Acceptance criteria

- [ ] `eggserve-core` is removed from `rust/Cargo.toml`.
- [ ] `eggserve-server =0.3.0` is exact-pinned with
      `default-features = false, features = ["tower"]`.
- [ ] EggPool imports `TowerToEggserve` and `RequestBodyPolicy` directly
      from `eggserve-server`.
- [ ] No direct `eggserve-primitives` dependency is added unless justified
      by a public type not exposed from the server crate.
- [ ] `ServerRuntime::serve_listener` retains the same control/completion
      lifecycle and teardown ordering.
- [ ] Unexpected EggServe completion retains the existing EggPool error path.
- [ ] All existing runtime configuration values remain unchanged.
- [ ] New EggServe 0.3 policy/admission ownership defaults remain EggServe-owned
      during the cutover.
- [ ] The 1 GiB transport/Tower hard ceiling remains in place.
- [ ] Generation-owned live body admission remains authoritative for all four
      inference routes.
- [ ] Real-socket transport/lifecycle/streaming/compact suites pass.
- [ ] The production no-dev graph contains no `eggserve-core`.
- [ ] The production no-dev graph contains no `eggserve-static`.
- [ ] EggServe contributes no PHF-family dependency to EggPool.
- [ ] The before/after package and dependency-node delta is recorded.
- [ ] The before/after release executable and RSS delta is recorded.
- [ ] The Plan-248-style finite/streaming comparison is rerun or its current
      equivalent is recorded.
- [ ] Any remaining PHF package has documented non-EggServe ancestry.
- [ ] The new 0.3 ownership APIs are classified without opportunistic semantic
      changes.
- [ ] Full default/no-default/security/package/tooling gates pass.
- [ ] Hosted CI passes on the exact closure candidate.
- [ ] Current-authority docs describe the direct server-only EggServe boundary.

## Non-goals

- No EggServe upstream changes.
- No reintroduction of `eggserve-core 0.3.0`.
- No static-serving adoption.
- No HTTP/2 or HTTP/3 enablement.
- No TLS ownership migration.
- No caller-owned raw H1 driver migration.
- No forward-proxy absolute-form support.
- No custom runtime rejection presenter.
- No external policy/admission ownership in this cutover.
- No provider transport/routing changes.
- No coordinator redesign.
- No config-surface change.
- No physical SBC campaign unless a material regression creates a separate
  evidence need.

Plan 250 closes when EggPool consumes the published direct
`eggserve-server 0.3.0 + tower` profile, the core/static/PHF EggServe branch
is absent from the production dependency graph, the existing HTTP lifecycle
and application-admission semantics are requalified, and the measured
dependency/resource delta plus policy-ownership review are recorded.
