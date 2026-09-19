# Plan 223 — Cancellation Test Determinism and Cleanup Pass

Date: 2026-09-19  
Status: implementation handoff  
Planning baseline: `2acb09ca6297058afc730862e40b14dd04226187`  
Priority: P2 test determinism / cancellation-path confidence  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Remove the two known scheduler-sensitive cancellation test patterns that have
produced intermittent failures during otherwise-green validation runs.

This is primarily a **test determinism cleanup**, not evidence of a production
cancellation defect.

The two known symptoms are:

1. `coordinator_publication::cancelling_the_waiter_cannot_strand_a_claim_or_durable_rows`
   can fail because it gives the detached publication/compensation worker only
   100 `yield_now()` iterations to converge.
2. `provider_transport::extended_encrypted_proxy_cancellation_recovers_through_same_client`
   uses a 1 ms sleep to guess that the first encrypted proxy connection has
   started, followed by another 50 ms sleep before retrying. Under a different
   scheduler interleaving the task may be aborted before the intended
   cancellation boundary is reached.

A nearby SSH cancellation test uses the same 1 ms abort heuristic and should be
included in the audit while this code is open.

The goal is to synchronize tests on observable state transitions instead of
wall-clock guesses or fixed scheduler-yield counts.

## Current-state evidence

### Coordinator publication cancellation

Current test:

`rust/tests/coordinator_publication.rs::cancelling_the_waiter_cannot_strand_a_claim_or_durable_rows`

The production path deliberately detaches publication work:

```rust
let (sender, receiver) = oneshot::channel();
tokio::spawn(async move {
    let outcome = service.publish_owned(claim, input).await;
    if let Err(outcome) = sender.send(outcome) {
        service.compensate_lost_delivery(outcome).await;
    }
});
```

The test blocks the SQLite transaction at `PublicationStage::BeforeCommit`,
aborts the receiver task, releases the barrier, then waits like this:

```rust
for _ in 0..100 {
    if fixture.router.active_request_count("account-a") == 0 {
        break;
    }
    tokio::task::yield_now().await;
}
assert_eq!(fixture.router.active_request_count("account-a"), 0);
```

One Plan 222 validation run observed the assertion before the detached worker
finished cleanup; the test then passed in isolation and on a full retry.

The production compensation ordering is useful here:

1. detached worker finishes/observes transaction outcome;
2. dropped oneshot receiver causes `sender.send(...)` failure;
3. `compensate_lost_delivery()` runs;
4. for a published result, `compensate_post_commit()` first converges durable
   rows;
5. only then does it release/rollback the local claim;
6. `active_request_count("account-a") == 0` therefore acts as an observable
   completion condition for the compensation path used by this test.

The defect in the test is the fixed number of yields, not the invariant being
asserted.

### Encrypted proxy cancellation

Current test:

`rust/tests/provider_transport.rs::extended_encrypted_proxy_cancellation_recovers_through_same_client`

It currently:

- starts a fixture with a 100 ms artificial handshake delay;
- starts the request;
- sleeps 1 ms;
- aborts the request task;
- sleeps another 50 ms;
- sends the recovery request;
- expects the recovery request to succeed;
- expects two completed proxy handshakes.

The 1 ms sleep is not proof that the first connection was accepted or that the
request reached the intended cancellation phase.

The fixture currently records completed handshakes only after
`shadowsocks_accept` / SSR accept succeeds. It does not expose a deterministic
"TCP connection accepted and handshake intentionally paused" boundary.

### SSH cancellation

`rust/tests/provider_transport.rs::ssh_cancellation_does_not_poison_the_provider_client`

also performs:

```rust
tokio::time::sleep(Duration::from_millis(1)).await;
pending.abort();
```

It has not been observed failing in the same way, but it relies on the same
scheduler assumption. Audit it in this pass so cancellation tests do not retain
two different standards.

## Required end state

After this pass:

1. the coordinator publication cancellation test waits on a bounded
   **condition**, not a fixed number of scheduler yields;
2. the encrypted proxy cancellation fixture exposes a deterministic
   cancellation boundary;
3. the encrypted proxy test aborts only after that boundary is observed;
4. fixed 1 ms / 50 ms sleeps are removed from that test;
5. assertions describe the product contract rather than incidental connection
   timing;
6. the SSH cancellation test is either converted to explicit synchronization
   or documented with evidence that its existing boundary is already
   deterministic;
7. no production retry/routing/cancellation semantics are weakened to make
   tests pass;
8. no arbitrary timeout inflation is used as the primary fix;
9. repeated targeted stress runs complete without intermittent failure.

## Workstream 1 — Make coordinator compensation waiting condition-based

### File

- `rust/tests/coordinator_publication.rs`

### Replace the 100-yield loop

Replace:

```rust
for _ in 0..100 {
    if fixture.router.active_request_count("account-a") == 0 {
        break;
    }
    tokio::task::yield_now().await;
}
```

with a bounded eventual-condition helper or equivalent.

Preferred semantics:

```text
timeout(reasonable_bound, async {
    while active_request_count != 0 {
        yield/sleep cooperatively
    }
})
```

The timeout is only the failure bound. Success must be driven by observing the
actual condition.

A small reusable test helper is acceptable if other coordinator cancellation
tests need the same pattern, for example:

```rust
async fn wait_until(timeout, predicate) -> Result<(), ...>
```

Do not add a general async-test framework.

### Bound selection

Use a bound large enough for SQLite worker scheduling under CI load but short
enough to expose a real stuck compensation promptly. A value on the order of
1–2 seconds is appropriate for this local in-memory/temp SQLite test.

Do not use hundreds/thousands of `yield_now()` calls as a disguised timeout.

### Preserve the important assertions

Keep:

- active claim count converges to zero;
- durable rows are either:
  - completely rolled back `(0, 0, 0, 0)`; or
  - completely published `(1, 1, 1, 1)` with reservation status
    `released`;
- no partial durable fanout is accepted.

If the condition times out, print useful state in the assertion:

- active request count;
- row counts;
- reservation status if present;
- whether the fault injector reached `BeforeCommit`.

This should make any future true failure diagnosable from CI output.

### Do not alter production ownership semantics merely for the test

The current detached worker exists specifically so cancellation of the caller
cannot cancel SQLite closure and strand ownership.

Do not:

- make `PublicationService::publish` cancellation-propagating;
- add Drop side effects to `SelectionClaim`;
- return the worker `JoinHandle` through production APIs only to satisfy this
  test;
- force compensation to happen synchronously in the caller task.

If deterministic condition waiting reveals a real non-converging production
path, stop and create a separate corrective plan describing that bug.

## Workstream 2 — Add an explicit encrypted-proxy cancellation gate

### File

Primary:

- `rust/tests/provider_transport.rs`

No production Eggfetch/Eggress source should need modification for the test
fixture cleanup.

### Fixture requirement

Extend `EncryptedProxyFixture` with test-only synchronization for the first
connection.

The fixture should be able to express:

```text
connection accepted
        |
        v
notify test that cancellation boundary is reached
        |
        v
wait on test-controlled release gate
        |
        v
continue encrypted protocol accept
```

Use Tokio synchronization already available in the test environment, such as:

- `tokio::sync::Notify`; or
- a small oneshot pair.

Prefer a one-shot explicit gate over atomics plus polling.

A possible fixture shape:

```rust
struct EncryptedProxyFixture {
    ...
    first_connection_accepted: Arc<Notify>,
    first_connection_release: Arc<Notify>,
    accepted_connections: Arc<AtomicUsize>,
}
```

Exact structure may differ.

### Required ordering in the test

The cancellation test should become logically:

1. start encrypted proxy fixture with first-handshake gate enabled;
2. start request on cloned provider client;
3. await "first proxy TCP connection accepted";
4. abort request task;
5. await/confirm task cancellation;
6. release the fixture gate so the first proxy connection can converge/close;
7. immediately issue the recovery request through the **same**
   `ProviderHttpClient`;
8. recovery request reaches the origin and returns 200.

No 1 ms pre-abort sleep and no 50 ms post-abort sleep should remain.

### Assert the contract, not incidental handshake count

The core contract is:

> cancelling an in-flight proxied request must not poison the reusable provider
> client; a subsequent request through the same configured proxy must succeed,
> and the cancelled request must not reach the origin.

Required assertions:

- first proxy connection was accepted before abort;
- recovery request succeeds through the same client;
- origin sees exactly one request;
- that request is `/after-encrypted-cancel`;
- proxy target/direct-bypass invariants remain intact.

Be careful with:

```rust
assert_eq!(proxy.handshake_count(), 2);
```

If cancellation occurs intentionally before protocol acceptance completes, the
first handshake is allowed to fail as part of cancellation. Exact completed
handshake count may therefore be an implementation detail rather than the
contract.

Prefer recording separate fixture counters:

- accepted TCP connections;
- completed encrypted handshakes;
- origin requests.

Then assert only what is semantically required. For example, two accepted
connections and at least the recovery handshake completing can be stronger and
less timing-sensitive than demanding two successful encrypted handshakes.

If repository/Eggress semantics explicitly require the first handshake to
complete despite caller cancellation, document that evidence before retaining
an exact two-handshake assertion.

## Workstream 3 — Remove the SSH 1 ms cancellation guess where practical

### File

- `rust/tests/provider_transport.rs`

Inspect:

`ssh_cancellation_does_not_poison_the_provider_client`

The current 1 ms sleep should not remain merely because the test has not yet
flaked.

Preferred approaches:

1. expose a deterministic observable boundary from the SSH fixture/process if
   it can be done narrowly; or
2. move cancellation to a deterministic provider-client state already exposed
   by an existing fixture; or
3. if SSHD is too opaque for a clean gate without adding brittle
   instrumentation, rewrite the assertion so it does not depend on the first
   request having advanced after exactly 1 ms.

Do not add invasive SSH daemon parsing/log scraping solely to eliminate one
sleep.

If no narrow deterministic gate is available, retain a bounded
`tokio::select!`/timeout-based readiness criterion rather than an unconditional
1 ms sleep, and document the limitation in the implementation record.

## Workstream 4 — Audit adjacent cancellation tests for the same anti-pattern

Keep this audit narrow.

Search Rust tests for combinations of:

- `tokio::spawn`;
- `.abort()`;
- very short sleeps;
- fixed `yield_now()` loops;
- assertions immediately after detached cleanup.

Candidate already visible in `provider_transport.rs`:

- pool-capacity cancellation waits 10 ms before abort and 200 ms before retry.

Do **not** mechanically rewrite every sleep.

Classify each one:

- deterministic fixture delay intentionally testing timeout duration;
- bounded teardown wait;
- scheduler guess used to reach a state;
- eventual-consistency wait after cancellation.

Only change the latter two when the intended state can be observed directly.

Record the audit result in Plan 223's implementation record so future work
does not repeat the same review.

## Workstream 5 — Stress qualification

A single passing run is insufficient for this cleanup.

### Targeted repetition

Run the previously flaky tests repeatedly after the changes.

At minimum:

```bash
for i in $(seq 1 100); do
  cargo test --manifest-path rust/Cargo.toml     --test coordinator_publication     cancelling_the_waiter_cannot_strand_a_claim_or_durable_rows     -- --exact --test-threads=1 || exit 1
done
```

and:

```bash
for i in $(seq 1 100); do
  cargo test --manifest-path rust/Cargo.toml     --test provider_transport     extended_encrypted_proxy_cancellation_recovers_through_same_client     -- --exact --test-threads=1 || exit 1
done
```

If `--exact` requires the module-qualified test name emitted by the test
binary, use that exact discovered name.

For the SSH cancellation test, run repeated qualification when the
`test-support` feature and host SSH fixture prerequisites are available.

### Mixed scheduler pressure

In addition to serial repetition, run the full relevant test binaries normally:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication
cargo test --manifest-path rust/Cargo.toml --test provider_transport
```

Then run the normal repository serial baseline because that is where the
coordinator flake was observed:

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Do not mark the plan complete if the same assertion flakes during the stress
loop.

## Full validation

From repository root:

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

If `Cargo.toml` or `Cargo.lock` changes, explain why. This cleanup should not
require a new dependency.

## Stop conditions

Do not paper over a real cancellation defect.

Stop this plan and write a follow-up corrective plan if deterministic
synchronization shows any of these:

- coordinator active claim never converges to zero within a reasonable bound;
- partial durable publication survives cancellation;
- released reservation is missing after committed publication cancellation;
- same provider client cannot recover after a deterministically in-flight
  encrypted proxy cancellation;
- cancellation causes direct-network fallback around a configured proxy;
- resource ownership leaks are visible outside test-only timing.

Those are production correctness bugs and require a different implementation
scope.

## Documentation / plan lifecycle

No user-facing docs should need changes.

At completion:

1. append an implementation record to this file;
2. include the exact root cause classification for each test;
3. list synchronization primitives introduced;
4. record the 100x repetition results;
5. record the adjacent cancellation-test audit;
6. record full validation results;
7. set `Status: complete`.

## Out of scope

Do not use this pass to:

- redesign `PublicationService`;
- add Drop-based claim cleanup;
- change provider retry policy;
- change routing/fairness/backoff behavior;
- change Eggfetch or Eggress APIs without evidence of a production bug;
- increase sleeps/timeouts as the primary fix;
- add a generic async test framework;
- refactor unrelated provider fixtures;
- revisit Plans 221/222 terminal/dashboard work.

## Acceptance criteria

Plan 223 is complete when:

1. coordinator publication cancellation no longer depends on 100 fixed
   `yield_now()` iterations;
2. coordinator cleanup is awaited through a bounded observable condition;
3. encrypted proxy cancellation no longer uses the 1 ms pre-abort sleep;
4. encrypted proxy recovery no longer uses the 50 ms post-abort sleep;
5. the first encrypted proxy request is proven to reach an explicit fixture
   boundary before abort;
6. the recovery request succeeds through the same provider client;
7. the cancelled request never reaches the origin;
8. assertions no longer depend on incidental completed-handshake count unless
   that count is proven to be contractual;
9. the SSH 1 ms cancellation heuristic is removed or explicitly justified with
   a bounded deterministic replacement;
10. adjacent cancellation tests are audited for the same scheduler-guess
    pattern;
11. each originally flaky test passes at least 100 consecutive targeted runs;
12. relevant test binaries and the full serial workspace test pass;
13. no new dependency or production behavior change is introduced unless a
    newly proven production bug requires a separately planned correction.

## Handoff note

Treat this as synchronization cleanup.

The current evidence points to tests racing the scheduler, not to Eggpool
losing ownership or poisoning clients in production. Replace "sleep/yield and
hope the state was reached" with "wait until the fixture or invariant proves
the state was reached."

If the deterministic version fails, that is valuable evidence: stop widening
the timeout and elevate the newly reproducible production bug into its own
corrective plan.
