# Plan 245 — EggServe 0.2.0 Phase 0 Gate Verdict and Adoption Stop

Date: 2026-09-22
Status: complete
Planning baseline: `ad4fadb3` (main, Eggpool 0.8.0)
Corrects: implementation path opened by `plans/244-eggserve-0.2.0-downstream-transport-adoption-and-runtime-requalification.md`
Priority: P1 downstream transport ownership (gate verdict, no runtime change)

Related Eggpool work:
- `plans/241-eggfetch-0.2.0-adoption-and-provider-transport-requalification.md`
- `plans/243-eggress-1.0.8-outbound-crate-adoption-and-typed-route-error-requalification.md`
- `plans/244-eggserve-0.2.0-downstream-transport-adoption-and-runtime-requalification.md`

## Verdict

Plan 244 implementation **stops at the Phase 0 gate**. No EggServe
dependency was added to `rust/Cargo.toml`, no `rust/Cargo.lock` entry was
created, and `axum::serve` remains the production downstream connection
driver in `rust/src/server/mod.rs::serve_listener`. No runtime behavior,
configuration surface, dependency graph, or release footprint changed.

Two independent Phase 0 failures were proven against the exact crates.io
`eggserve-core =0.2.0` surface with the exact plan-prescribed feature
profile (`default-features = false, features = ["tower"]`):

1. **Compile gate fails inside the upstream crate itself** (before any
   Eggpool/Axum code is involved). `eggserve-core 0.2.0` with the `tower`
   feature does not compile.
2. **Mandatory lifecycle gate fails**: 0.2.0 exposes no public passive
   terminal-observation future or subscription. `ServerHandle::wait()`
   initiates shutdown when the server is still running, and the only other
   observers are a synchronous `state()` getter (explicitly insufficient
   per Plan 244) and `pub(crate)` internals.

Either failure alone is a hard stop per Plan 244's rollback rule. No
Phase 1–11 work was started. No local workaround adapter, polling bridge,
forked accept loop, or detached-runtime composition was introduced.

## Evidence — compile gate (upstream defect, reproduced 2026-09-22)

Repository-local probe crate (kept outside the repo, never committed):

```toml
[dependencies]
axum = "0.8"
eggserve-core = { version = "=0.2.0", default-features = false, features = ["tower"] }
```

`cargo build` fails with 5 errors, all inside
`eggserve-core-0.2.0/src/primitives/interop.rs` (resolved lock:
`eggserve-core 0.2.0`, `eggserve-primitives 0.2.0`,
`eggserve-server 0.2.0`, `eggserve-static 0.2.0`):

- **E0117 (structural, version-independent)**: `interop.rs:492`
  `impl http_body::Body for RequestBody` violates the orphan rule because
  Plan 217 moved `RequestBody` to the external `eggserve_primitives` crate
  while the impl stayed in `eggserve_core`. `eggserve-core`'s own
  `src/primitives/request_body.rs` is now a pure re-export
  (`pub use eggserve_primitives::request_body::*`). An
  external-trait-for-external-type impl can never compile; no feature
  combination or downstream version pin can fix it from Eggpool's side.
- **E0599 x3 (missing feature forwarding)**: the trailer-sync accessors
  used by the `http_body` adapter — `completed_trailer_failure`,
  `take_completed_trailers`, `completed_trailers_snapshot` — exist in
  `eggserve-primitives 0.2.0` only behind `#[cfg(feature =
  "http-interop")]`, but `eggserve-core`'s `http-interop = ["dep:http"]`
  (enabled via `tower = ["http-interop", ...]`) does not enable
  `eggserve-primitives/http-interop`. Its dependency declaration is a bare
  `version = "0.2.0"` with no features.
- **E0004 (stale match)**: `interop.rs:212` matches on `HttpVersion`
  without a wildcard, but the 0.2.0 primitives type is `#[non_exhaustive]`
  with a fourth `Http3` variant.

Control: the same probe with `eggserve-core = { version = "=0.2.0",
default-features = false }` (no `tower`/`http-interop`) compiles cleanly,
so the defect is precisely the `tower`/`http-interop` surface Plan 244
requires. Upstream's own `application_service` example exercises the same
broken surface. Latest available release as of this writing is 0.2.0 for
all `eggserve-*` crates (verified via `cargo search`/`cargo info`); no
fixed release exists to re-probe.

Because the failure is inside the upstream crate, Plan 244 items 1–6 of
the compile gate (router through `TowerToEggserve`, canonical body model,
incremental `Body::from_stream` crossing, duplicate headers, no buffering)
are unprovable. Per Plan 244, no second custom generic HTTP adapter was
written inside Eggpool.

## Evidence — lifecycle gate (missing public contract, read 2026-09-22)

From the published `eggserve-core 0.2.0` source
(`src/server/handle.rs`, `src/server/lifecycle.rs`, `src/server/mod.rs`):

- `ServerHandle::wait(mut self)` (`handle.rs:326-347`) triggers graceful
  shutdown first when the server is still running, then waits. It is an
  active shutdown initiator, not a passive observer — exactly the shape
  Plan 244 declares insufficient.
- The only passive observer is the synchronous `state()` getter
  (`handle.rs:218-221`), which Plan 244 declares "not an acceptable
  production replacement" on its own.
- The terminal subscription exists but is not public: `Lifecycle` is a
  `pub(crate)` struct (`lifecycle.rs:92`) and `subscribe_terminal` is
  `pub(crate)` (`lifecycle.rs:301`). It is not re-exported through
  `server/mod.rs` (which re-exports only `ServerHandle`, `LifecycleState`,
  config, and service types).
- `ServerHandle` is not `Clone` (exactly one handle per server by design)
  and `Drop` triggers graceful shutdown (`handle.rs:361-369`), so Eggpool
  cannot retain a passive observation handle alongside an owned driver
  without shutdown coupling.

No public passive terminal/failed-state future or subscription exists in
the 0.2.0 API, and no other non-polling composition preserves Eggpool's
current "explicit quiesce request plus unexpected HTTP server
completion/failure" observation semantics (`rust/src/server/mod.rs`
`select!` over the `axum::serve` task vs `wait_for_quiesce`). Adding
periodic `state()` polling, reaching into lifecycle internals, forking the
accept loop, or detaching the runtime would each violate an explicit
Plan 244 prohibition, so none was attempted.

## Upstream follow-up required (narrow, two items)

Filed against EggServe (upstream issue to be created from this record;
no workaround lands in Eggpool first):

1. **Fix the `tower`/`http-interop` build**: move the `http_body::Body`
   impl for `RequestBody` to the owning crate (`eggserve-primitives`,
   behind its existing `http-interop` feature) or a local newtype;
   forward `eggserve-primitives/http-interop` from
   `eggserve-core/http-interop`; handle the `HttpVersion::Http3` arm.
   Acceptance: the Plan 244 Phase 0 probe crate above builds, and
   upstream's `application_service` example builds under the same
   feature set.
2. **Expose a passive terminal-observation contract**: a public,
   cloneable/shared subscription or future that resolves on
   terminal (`Stopped`/`Failed`) state without initiating shutdown and
   without requiring ownership of the sole `ServerHandle`, so a
   downstream runtime can compose unexpected HTTP-server failure into its
   own shutdown ownership without polling. Acceptance: Eggpool can await
   unexpected accept-loop failure passively while `ServerRuntime` remains
   the sole shutdown initiator.

A new Eggpool implementation plan may only be opened after an upstream
release carrying both fixes lands on crates.io; that plan must re-run the
full Plan 244 Phase 0 gate against the new exact version before any
production driver edit.

## Scope confirmation (what did not change)

- `rust/src/server/mod.rs` (`serve_listener`, `build_router`, middleware,
  handlers, `ServerRuntime`/`ShutdownReport`): untouched.
- `rust/Cargo.toml` / `rust/Cargo.lock`: untouched (no `eggserve-core`
  entry; no feature changes to Eggress/Eggfetch/SSH/no-default contracts
  from Plans 241/243).
- `config.example.toml`, reload policy (`server.max_request_body_bytes`
  stays live-reloadable `u64`; no 1 GiB validation ceiling added since no
  EggServe hard ceiling exists in the build).
- `architecture/`, `docs/`, `README.md`, `rust/README.md`,
  `.opencode/skills/`: unchanged — they describe the shipped
  `axum::serve` boundary, which is still accurate. Only `AGENTS.md`
  gains the standard one-paragraph plan-outcome note, matching the
  Plan 239–243 convention.
- Historical Plans 230–244: untouched (append-only).

## Qualification

No behavior changed, so no requalification matrix applies beyond proving
the tree is green as left:

- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings`
- `cargo check/clippy/test --no-default-features` (SSH-off provider contract)
- `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1`
- `cargo build --locked`, `cargo build --locked --release`
- `cargo deny check`, `cargo tree -e features`, `cargo tree --duplicates`
- `uv sync --frozen`, `ruff format --check`, `ruff check`, `pyright`, `pytest tests/tooling/`

Full command outputs are recorded in the implementation commit's CI run,
which is expected to skip per the docs-only filter (`plans/`,
`AGENTS.md`) with local gates green.
