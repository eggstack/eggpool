# Plan 249 — Responses Compact Admission and Generation-Lease Corrective

Date: 2026-09-24
Status: implementation handoff
Planning baseline: `cc17d2ccfa2233453fe9f39d2621122d0180b3e2` (main, EggPool 0.8.0)
Priority: P1 public inference route correctness
Related:
- Plan 248 — EggServe downstream transport closure
- current server boundary: `rust/src/server/mod.rs`
- body/generation admission: `rust/src/server/middleware.rs`
- compact endpoint: `rust/src/server/inference.rs::responses_compact`
- compact compatibility suite: `rust/tests/codex_compaction_compat.rs`

## Problem

`POST /v1/responses/compact` is registered as a production inference route and
its handler requires:

```rust
Extension(lease): Extension<Arc<GenerationLease>>
```

The only production middleware that acquires a live generation, enforces that
generation's `server.max_request_body_bytes`, and inserts the
`GenerationLease` extension is
`server::middleware::admit_inference_body`.

That middleware is gated by `is_inference_path()`, whose current route set is:

```rust
"/v1/chat/completions" | "/v1/messages" | "/v1/responses"
```

It omits `"/v1/responses/compact"`.

Therefore the compact route does not traverse the generation/body-admission
path required by its own handler contract. Depending on Axum extractor
resolution, the request can fail before `responses_compact` executes because
the required `GenerationLease` extension was never inserted. Even apart from
that extractor failure, the route bypasses the live generation-owned request
body ceiling.

This defect predates the EggServe migration. The same omission exists at
baseline `d492eade677acd6fc932c9a0c487b744a3070a91`. Do not reopen Plans
246–248 or alter EggServe ownership as part of this correction.

## Objective

Make `/v1/responses/compact` use the exact same generation acquisition and
bounded body-admission boundary as the other public inference endpoints,
without changing compact coordinator semantics or introducing a second body
collector.

The desired request path is:

```text
EggServe HTTP/1 transport
  -> Axum authenticate middleware
  -> admit_inference_body
       -> acquire active GenerationLease
       -> enforce that generation's max_request_body_bytes
       -> collect one bounded Bytes body
       -> insert Arc<GenerationLease>
  -> responses_compact
  -> execute_compact_finite
```

## Track A — Correct inference-route classification

In `rust/src/server/middleware.rs`, add
`"/v1/responses/compact"` to `is_inference_path()`.

Do not:

- add route-specific lease acquisition inside `responses_compact`;
- add a second body-size check in the compact handler;
- special-case compact in `build_router`;
- move body admission into EggServe;
- create a second generation lookup after body collection.

The compact route must reuse the same middleware-owned invariant as Chat,
Messages, and Responses.

## Track B — Add a route-classification regression test

Add a focused unit test adjacent to the middleware classifier, or in the
smallest existing server test module that owns this behavior.

Assert all four production inference routes are classified:

```text
/v1/chat/completions
/v1/messages
/v1/responses
/v1/responses/compact
```

Also assert at least one nearby non-inference route remains false, for example:

```text
/v1/models
/api/status
```

The test should make future route additions visibly fail if they require the
generation/body-admission contract but are omitted from the classifier.

Prefer a table-driven test.

## Track C — Real-socket compact execution regression

Extend `rust/tests/server_transport.rs` or add the smallest dedicated
server-side integration test if reuse would make that target unclear.

Exercise the actual production chain over a TCP listener:

```text
TcpStream
  -> EggServe
  -> TowerToEggserve
  -> Axum middleware
  -> responses_compact
  -> compact coordinator path
```

At minimum prove an authenticated, syntactically valid compact request no
longer fails due to a missing `GenerationLease` extension.

Use a deterministic local fixture. No external provider credentials or network
access.

The assertion must distinguish the old failure mode from normal compact
semantics. Prefer a fixture that can complete the compact operation successfully
and returns the expected compact response. If the current compact coordinator
fixture naturally returns a deterministic application error, assert that exact
application-level response and explicitly prove it is not the Axum
missing-extension rejection.

Do not weaken `responses_compact`'s existing finite-only/stateless contract.

## Track D — Prove live body-limit enforcement on compact

Add production-socket evidence that `/v1/responses/compact` now shares the
same live generation body ceiling.

Use a deliberately small configured
`server.max_request_body_bytes` in the test fixture and verify:

1. an authenticated compact request below the limit reaches the handler;
2. an authenticated Content-Length compact request above the limit returns the
   existing EggPool 413 JSON contract;
3. if practical within the existing `server_transport` helpers, a chunked
   compact request crossing the limit also returns 413;
4. a healthy request succeeds afterward, proving the rejection does not poison
   the connection/runtime.

Do not test the 1 GiB EggServe hard ceiling here; Plan 248 already owns the
transport-level ceiling. This plan is about the lower live generation-owned
limit.

## Track E — Preserve generation/reload semantics

The middleware must continue acquiring the active generation before body
collection and attaching that exact lease to the request.

If there is an existing deterministic reload fixture that can be extended
cheaply, add a compact-route assertion showing a request admitted under one
generation retains that generation's body limit even if a newer generation is
published before handler execution.

Do not create substantial new reload harness solely for this corrective if the
existing middleware contract already has direct coverage. The mandatory
closure condition is that compact uses the same middleware path as the other
inference routes.

No reload-policy change is expected:
`server.max_request_body_bytes` remains live-reloadable.

## Track F — Focused qualification

Run:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
```

Also run the relevant server/middleware library tests that contain the new
classification regression.

Then run the normal serial workspace suite:

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Because this should be a source/test-only correction with no dependency or
feature change, `cargo deny`/dependency-tree requalification is not required
unless implementation unexpectedly changes `Cargo.toml` or `Cargo.lock`.

Run `--no-default-features` if the changed integration target is compiled
under that profile in ordinary CI; do not change the existing SSH/provider
feature contract.

## Track G — Documentation and closure

This is a narrow correctness fix. No architecture rewrite should be necessary.

Update only current authority that explicitly enumerates inference routes or
claims all public inference routes already traverse the generation-owned body
admission path.

At minimum inspect:

- `AGENTS.md`;
- `architecture/overview.md`;
- `architecture/deep-dive-request-lifecycle.md`;
- `.opencode/skills/architecture/SKILL.md`.

Do not churn docs that are already correct at the conceptual level.

After implementation, update this plan's status/evidence or add the repository's
normal append-only closure pass if additional corrective work was needed.

## Acceptance criteria

- [ ] `is_inference_path("/v1/responses/compact")` returns true.
- [ ] All four public inference endpoints are covered by a regression test.
- [ ] Compact requests receive a `GenerationLease` through the common
      middleware path.
- [ ] Compact body collection uses the generation's live
      `max_request_body_bytes`.
- [ ] Over-limit compact Content-Length requests return EggPool's existing
      413 JSON response.
- [ ] Chunked over-limit compact requests are covered if supported by the
      existing deterministic socket fixture.
- [ ] A valid/authenticated compact request reaches compact application logic
      rather than Axum's missing-extension rejection.
- [ ] Compact remains finite-only and stateless.
- [ ] No duplicate body collector or route-specific generation lookup is added.
- [ ] EggServe transport code and dependency versions are unchanged.
- [ ] Existing Responses/Chat/Messages admission remains unchanged.
- [ ] Focused compact/server/coordinator tests pass.
- [ ] Full serial workspace suite passes.

## Non-goals

- No EggServe changes.
- No reopening Plans 246–248.
- No compact protocol redesign.
- No remote-compaction capability expansion.
- No stateful Responses continuation.
- No new public configuration.
- No provider transport/routing changes.
- No dependency cleanup.
- No performance/SBC campaign.

This plan closes when the production compact route demonstrably shares the same
generation/body-admission invariant as every other public inference route.
