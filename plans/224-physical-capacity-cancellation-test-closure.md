# Plan 224 — Physical-Capacity Cancellation Test Closure

Date: 2026-09-19  
Status: complete
Planning baseline: `13c0c53a25f6e38ed646300489bc3ff088d4b463`  
Closes residual from: `plans/223-cancellation-test-determinism-cleanup.md`  
Priority: P2 narrow test-contract cleanup  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Close the one remaining scheduler assumption left after Plan 223.

Plan 223 correctly removed the observed coordinator and proxy cancellation
flakes and established the repository rule that cancellation tests synchronize
on observable state rather than fixed sleeps or yield counts.

One adjacent test still contains:

```rust
tokio::task::yield_now().await;
waiting.abort();
```

in:

```text
rust/tests/provider_transport.rs
cancellation_during_pool_wait_releases_no_permit
```

That single yield is still a scheduler guess. It does not prove that the second
request reached the capacity-wait boundary before cancellation.

This plan replaces that guess with a deterministic future-poll boundary and
aligns the test name/assertions with the actual Eggpool/Eggfetch ownership
contract. No production transport behavior or dependency change is expected.

## Research result: where the capacity limit actually lives

Eggpool currently builds Eggfetch like this in
`rust/src/providers/transport.rs::build_eggfetch_client`:

```rust
.physical_connection_policy(PhysicalConnectionPolicy {
    max_live: Some(config.max_connections),
    admission_timeout: Some(config.pool_timeout),
})
```

and explicitly does **not** configure Eggfetch's logical
`max_connections`/request-permit pool.

Therefore Eggpool's `ProviderHttpConfig.max_connections = 1` test is testing
Eggfetch's **physical connection admission** contract.

Pinned `eggfetch-core = 0.1.7` confirms:

- `PhysicalConnectionPolicy.max_live` owns a semaphore in
  `transport/lifecycle.rs`;
- a permit is retained by the live Hyper connection, including while it is
  pooled;
- when no permit is available, a new connection future awaits physical
  admission;
- cancellation drops that pending future/permit-acquisition state;
- Eggfetch owns the exact semaphore implementation and low-level lifecycle
  tests.

Eggpool should therefore test its wrapper-level contract:

> while the only allowed physical connection is occupied, a second provider
> request becomes pending; cancelling that pending request must not poison the
> client, leak capacity, reach the origin, or prevent a later request from
> succeeding.

Eggpool should **not** add a public transport-metrics accessor or duplicate
Eggfetch's internal semaphore diagnostics merely to prove this.

## Required end state

After this pass:

1. the test no longer uses `yield_now()` as readiness;
2. no millisecond sleep is introduced as a replacement;
3. the second request is explicitly polled and proven to return
   `Poll::Pending` while capacity is saturated;
4. cancellation is performed by dropping that pending request future;
5. the cancelled request never reaches the origin;
6. after releasing the held response, the same client successfully performs a
   recovery request;
7. the test name describes the observable wrapper contract rather than an
   unobservable Eggfetch implementation detail;
8. no production API, dependency, Eggfetch version, or runtime policy changes.

## Workstream 1 — Replace spawn/yield/abort with explicit pending-future cancellation

### File

- `rust/tests/provider_transport.rs`

### Current pattern

Current code:

```rust
let waiting_client = client.clone();
let waiting = tokio::spawn(async move {
    waiting_client
        .send(Method::GET, "/cancelled", HeaderMap::new(), Bytes::new())
        .await
});
tokio::task::yield_now().await;
waiting.abort();
let cancellation = waiting.await.expect_err("cancelled pool-wait task");
assert!(cancellation.is_cancelled());
```

Delete the scheduler-yield dependency.

### Preferred deterministic pattern

Construct and pin the second send future directly:

```rust
let mut waiting = Box::pin(
    client.send(
        Method::GET,
        "/cancelled",
        HeaderMap::new(),
        Bytes::new(),
    )
);
```

Poll it exactly enough to observe the first `Poll::Pending` without waiting
for a wakeup.

A suitable shape is:

```rust
use std::{
    future::Future as _,
    task::Poll,
};

std::future::poll_fn(|cx| {
    match waiting.as_mut().poll(cx) {
        Poll::Pending => Poll::Ready(()),
        Poll::Ready(result) => {
            panic!("capacity-saturated request completed before cancellation: {result:?}")
        }
    }
})
.await;
```

Then cancel by dropping the actual owned pinned future:

```rust
drop(waiting);
```

Use `Box::pin`, not `std::pin::pin!` followed by dropping only the
`Pin<&mut _>` handle: the cancellation point must actually drop the owned
future.

No spawned task is needed.

### Why first Pending is a valid boundary here

Keep the fixture setup:

- HTTP/1.1 only;
- `max_connections = 1`;
- first `/held` response remains alive and unconsumed;
- Eggpool maps that limit to Eggfetch's physical `max_live = 1`;
- a second request cannot obtain another physical connection while the first
  live connection is occupied.

Pinned Eggfetch 0.1.7's native request path validates the request and enters
transport dispatch under the configured physical lifecycle policy before it can
send a second request to the origin. Under this deliberately saturated fixture,
the second send future returning `Poll::Pending` is the correct observable
Eggpool-level boundary.

Do not try to expose Eggfetch's private semaphore or waiter list.

## Workstream 2 — Align the test name and assertions with the contract

### Rename

Prefer renaming:

```text
cancellation_during_pool_wait_releases_no_permit
```

to something like:

```text
cancelling_request_while_physical_capacity_is_saturated_preserves_client_recovery
```

or another equally precise name.

Reason: Eggpool does not own or expose Eggfetch's internal admission waiter.
The test should assert observable behavior at the wrapper boundary, while
Eggfetch owns exact semaphore bookkeeping.

### Required assertions

After polling the second request to `Pending` and dropping it:

1. drop the held response;
2. immediately send `/released` using the **same**
   `ProviderHttpClient`;
3. require a successful 200 response;
4. consume/drop the response as appropriate;
5. assert the origin observed:
   - `/held`;
   - `/released`;
   - **not** `/cancelled`.

Prefer an exact request-target assertion when fixture ordering is stable:

```text
["/held", "/released"]
```

This is stronger than only checking recovery status because it proves the
cancelled request never escaped the capacity boundary.

Do not assert a specific physical socket count unless it is necessary to the
contract. Hyper may choose reuse vs replacement after the held response is
dropped; the product requirement is recoverability and no cancelled-origin
request.

## Workstream 3 — Keep Eggfetch ownership boundaries intact

Do not modify:

- `rust/src/providers/transport.rs`;
- `ProviderHttpClient` public API;
- Eggfetch dependencies;
- `PhysicalConnectionPolicy`;
- `pool_timeout`;
- connection retry behavior;
- Eggress integration.

Do not add a production or public:

- `pool_waiter_count()`;
- semaphore accessor;
- raw Eggfetch-client accessor;
- transport-metrics accessor solely for this test.

Eggfetch 0.1.7 already owns physical-admission implementation and its own
lifecycle tests. This Eggpool test should remain an integration-level
consumer-contract test.

If the explicit first poll unexpectedly returns `Ready` while the held
response remains alive, stop rather than adding sleeps. That would mean the
assumption about the wrapper's capacity boundary is wrong or the underlying
transport behavior changed, and it should be investigated before revising the
test.

## Workstream 4 — Stress qualification

Run the corrected test repeatedly.

At minimum, 200 consecutive runs:

```bash
for i in $(seq 1 200); do
  cargo test --manifest-path rust/Cargo.toml     --test provider_transport     cancelling_request_while_physical_capacity_is_saturated_preserves_client_recovery     -- --exact --test-threads=1 || exit 1
done
```

Use the final exact test name if it differs.

Then run:

```bash
cargo test --manifest-path rust/Cargo.toml   --test provider_transport -- --test-threads=1

cargo test --manifest-path rust/Cargo.toml   --test provider_transport --features test-support -- --test-threads=1
```

This ensures the change coexists with the deterministic encrypted-proxy and SSH
cancellation fixtures from Plan 223.

## Full validation

Run from repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

No Cargo dependency change is expected. If `Cargo.toml` or `Cargo.lock`
changes, stop and justify it before continuing.

## Plan 223 consistency check

After implementation, verify there are no cancellation-readiness patterns in
the touched provider-transport tests of the form:

```text
sleep(short_duration)
abort
```

or:

```text
yield_now
abort
```

where the sleep/yield is serving only as proof that the cancelled future
entered a target state.

Legitimate timing tests such as idle expiry and actual timeout classification
remain out of scope and should retain duration-based assertions where time is
the behavior under test.

## Documentation

No user-facing documentation change is required.

The cancellation-test guidance added by Plan 223 already states the correct
rule. Do not add another general policy paragraph merely for this one test.

At completion, append an implementation record to this plan containing:

- final test name;
- exact pending-future polling mechanism;
- confirmation that cancellation drops the owned future;
- exact origin request sequence observed;
- 200x stress result;
- provider-transport suite results;
- full validation result;
- confirmation that production source and Cargo dependencies were unchanged.

Then set `Status: complete`.

## Out of scope

Do not use this pass to:

- reopen Plan 223's coordinator changes;
- modify encrypted-proxy or SSH gates unless a regression is discovered;
- change Eggfetch 0.1.7;
- change Eggress;
- expose new production diagnostics;
- redesign ProviderHttpClient;
- change connection limits or timeout defaults;
- replace legitimate timeout tests with manual polling;
- perform unrelated provider fixture cleanup.

## Acceptance criteria

Plan 224 is complete when:

1. the residual `yield_now().await` readiness guess is removed from the
   physical-capacity cancellation test;
2. the second provider request is directly polled and demonstrably reaches
   `Poll::Pending` while capacity is saturated;
3. the owned pending future is dropped to model cancellation;
4. no task sleep or scheduler-yield count is used to reach the cancellation
   boundary;
5. the cancelled `/cancelled` request never reaches the fixture origin;
6. the same provider client successfully sends `/released` after the held
   response is released;
7. the test name describes the observable Eggpool contract rather than
   claiming visibility into Eggfetch's private semaphore internals;
8. the corrected test passes 200 consecutive targeted runs;
9. default and `test-support` provider-transport suites pass;
10. the full repository validation baseline passes;
11. no production code, dependency, Eggfetch version, or transport policy
    changes are required.

## Handoff note

Do not solve this by exposing more internals.

The existing test is only one scheduler yield away from being correct. Replace
that yield with direct polling of the actual request future. Once the
capacity-saturated send has returned `Pending`, dropping the owned future is
the cancellation event, and the subsequent same-client request plus exact
origin request log are the useful product-level proof.

Eggfetch owns how physical admission is implemented. Eggpool only needs to
prove that its client wrapper behaves correctly when cancellation occurs at
that boundary.

## Implementation record

Completed 2026-09-19.

- Final test: `cancelling_request_while_physical_capacity_is_saturated_preserves_client_recovery`.
- The second request is owned by `Box::pin`, polled directly with
  `std::future::poll_fn` until it returns `Poll::Pending`, and cancelled by
  dropping that owned future.
- The held response is then dropped and the same provider client successfully
  completes `/released`, consuming both chunked body fragments.
- The exact fixture origin sequence was `[/held, /released]`; `/cancelled`
  was not observed.
- The targeted test passed 200 consecutive exact invocations.
- The default provider-transport suite passed 30 tests; the
  `test-support` suite passed 35 tests.
- Full validation passed: formatting, default and no-default-feature Clippy,
  no-default-feature check, 702 serial workspace tests across 60 suites,
  locked release build, frozen `uv` sync, Ruff format/check, Pyright, 83
  tooling tests with 1 skip, release-doc validation, runtime-package-boundary
  validation, and `git diff --check`.
- No production source, Cargo manifest/lockfile, Eggfetch version, or runtime
  transport policy changed. Existing cancellation guidance in `AGENTS.md`,
  `.opencode/skills/development/SKILL.md`, and the architecture provider
  documentation already covered the resulting contract; no redundant edits
  were made.
