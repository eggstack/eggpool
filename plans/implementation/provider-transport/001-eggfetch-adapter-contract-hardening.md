# Provider Transport Milestone 001 — Eggfetch adapter contract hardening

Status: closed

Repository baseline: `b1896813caa1ebb5382939d9a29788d987c01843`

Source roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-001--eggfetch-adapter-contract-hardening`

Long-term requirements:

- `plans/000-long-term-specification.md` — preserve the single provider HTTP abstraction/retry owner and secret-free diagnostics.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — provider transport hardening is sustaining Phase 1 work.
- `plans/003-planning-process.md` — closure requires focused and broad evidence, not compilation alone.

Applicable ADRs:

- None required. This milestone preserves the already-selected Eggfetch/Eggress transport architecture and HTTP/1.1 protocol boundary.

Primary class: invariant

## 1. Objective

Harden EggPool's existing Eggfetch provider adapter without reopening the
completed migration.

The milestone has two bounded outcomes:

1. make HTTP response-trailer behavior explicit by retaining trailer frames at
   the provider-body boundary while preserving the existing DATA-only
   `ProviderBody::next()` contract and without forwarding trailers into
   EggPool's public JSON/SSE wire semantics; and
2. remove the remaining upstream error-message text match from residual
   Eggfetch pool classification, using only typed facts plus EggPool's validated
   client-construction invariant.

The milestone must not change coordinator attempt counts, provider routing,
timeouts, connection limits, proxy behavior, TLS ownership, or public
downstream protocol behavior.

## 2. Why this milestone is ready

There are no hard external dependencies.

Legacy Plans 215–220 and 241 closed the Eggfetch transport migration and 0.2.0
requalification. The current adapter already uses a frame-preserving Eggfetch
native body, so retaining trailers is a local consumer-side concern.
EggPool already rejects zero/invalid physical connection limits before client
construction, and Eggfetch exposes a typed physical-admission timeout helper;
therefore the local residual `Pool` branch can be reviewed without requiring
an upstream release.

The broader cleanup of Hyper/Rustls source-chain inspection is deliberately not
part of M001. It remains provider-transport M002 and is blocked on an upstream
general-purpose Eggfetch classification API.

## 3. Current implementation evidence

At the baseline:

- `rust/Cargo.toml` exact-pins `eggfetch-core =0.2.0` with
  `default-features = false`, `native-http1`, and `tls-rustls`.
- `rust/src/providers/transport.rs::ProviderHttpClient::send`:
  - enforces the finite request-body bound before connection;
  - builds one HTTP/1.1 `Request<Full<Bytes>>`;
  - calls `Client::execute_http_body` once;
  - maps dispatch errors into stable `TransportError`.
- `build_eggfetch_client`:
  - forces `HttpVersionPolicy::Http1Only`;
  - disables canceled-request retry;
  - uses `PhysicalConnectionPolicy` for live-connection admission;
  - uses `TransportIoTimeout` for established read/write inactivity;
  - configures only connect timeout in Eggfetch's `Timeout`;
  - installs the Eggress dialer as the only physical route for proxied clients.
- `ProviderBody::next` currently loops over Eggfetch response frames,
  returning DATA and skipping every non-DATA frame. Valid HTTP trailers are
  therefore discarded.
- `ProviderBody::read_to_bytes` is built on `next()` and must remain a
  bounded DATA collector.
- `map_eggfetch_error` already checks
  `is_physical_connection_admission_timeout()` before the broad
  `EggfetchError::Pool` arm.
- That residual `Pool` arm currently checks whether the error message contains
  `"max_live"` to distinguish configuration from a pool timeout.
- `validate_config` rejects `max_connections == 0`,
  `max_keepalive == 0`, `max_keepalive > max_connections`, invalid body
  limits, and invalid/zero timeouts before the client is built.
- Cancellation, protocol, TLS, custom-dialer, timeout, and direct-connect
  mapping otherwise use typed variants/source types; M001 must not broaden
  message parsing.
- `rust/tests/provider_transport.rs` already supplies the real-socket fixture
  and qualification corpus for direct/proxied transport, pooling, cancellation,
  TLS, body bounds, premature close, and route failure categories.

## 4. Invariants that must not regress

- One coordinator attempt remains at most one transport submission.
- `retry_canceled_requests(false)` remains explicit.
- HTTP/1.1 remains the only provider protocol.
- Direct and proxied routes retain separate Eggfetch client/pool identity.
- A configured proxy route has no direct fallback.
- Origin TLS remains Eggfetch-owned; route TLS remains Eggress-owned.
- Physical live-connection admission remains separate from logical request
  concurrency.
- `ProviderBody::next()` continues to yield only DATA `Bytes` in the same
  order and chunking Eggfetch supplies.
- `read_to_bytes(max)` remains bounded by DATA bytes only and never treats
  trailer bytes/values as response content.
- HTTP trailers never become SSE/JSON terminal evidence, usage facts, retry
  signals, or coordinator success/failure input in this milestone.
- Existing `TransportError` variants and mapping precedence remain stable
  except for the explicitly reviewed residual `Pool` branch.
- No new raw header/trailer/body value is logged or surfaced through
  `Debug` as a consequence of the change.
- Cancellation/drop before EOF continues to release the Eggfetch response body
  and physical lease without waiting for trailers.
- No new task, lock, channel, or background owner is introduced.
- `--no-default-features` remains valid.

## 5. Scope

### In scope

- `rust/src/providers/transport.rs` response-frame handling.
- Additive trailer observation on `ProviderBody`.
- Focused manual/redacted `Debug` behavior if storing trailers would otherwise
  expose trailer values.
- Removal of the `Pool(message).contains(...)` classification dependency.
- Unit tests for the residual pool-error decision.
- Real-socket provider transport regression coverage for HTTP/1.1 trailers.
- Documentation of the DATA/trailer/error-classification contract.
- Default, `test-support`, no-default, coordinator, wire, dependency, and
  serial workspace qualification required by the repository.

### Explicitly out of scope

- No change to `eggfetch-core =0.2.0`.
- No upstream Eggfetch source modification in this EggPool milestone.
- No replacement of Hyper/Rustls source-chain inspection; that is M002.
- No updater HTTP migration.
- No redirect, logical retry, compression, JSON, cookie, auth, built-in proxy,
  HTTP/2, or HTTP/3 Eggfetch features.
- No changes to provider routing/account health/failover.
- No public downstream HTTP trailer forwarding.
- No provider-specific interpretation of trailer names or values.
- No new config/schema/environment setting.
- No SQLite/runtime/server lifecycle work.
- No physical SBC performance campaign absent evidence of a resource
  regression.

## 6. Required production changes

### Response-trailer retention

Keep `ProviderBody::next()` source-compatible and DATA-only.

Add body-local storage for trailers observed while polling Eggfetch
`NativeResponseBody`. The implementation should use the `http_body::Frame`
facts already returned by Eggfetch rather than parsing bytes.

Preferred public shape is an additive accessor such as:

```rust
pub fn take_trailers(&mut self) -> Option<HeaderMap>
```

The exact name may follow existing style, but the semantics must be:

- returns only trailers already observed while consuming frames;
- ordinarily becomes useful after `next()` returns `None`;
- does not force/drain the body on its own;
- moves or otherwise returns the retained `HeaderMap` without cloning large
  metadata unnecessarily;
- repeated call after take returns `None`;
- ordinary responses with no trailer frame return `None`.

If Hyper/Eggfetch can legally surface more than one trailer frame, merge them
without replacing duplicate field values. Preserve header multiplicity by
appending values rather than using a replace-only merge.

Do not add a downstream consumer in this milestone.

### Debug/security behavior

If `ProviderBody` can no longer safely derive `Debug` after storing a raw
`HeaderMap`, replace the derived implementation with a manual implementation
that reports only non-sensitive structural facts (for example
`has_trailers: bool`) and never trailer names/values.

Do not add trailer values to tracing, errors, test failure messages, closure
records, or diagnostics.

Do not opportunistically redesign `ProviderResponse` header debug behavior;
that is separate scope unless required to prevent a new M001 leak.

### Residual pool-error classification

Preserve the current mapping order.

First, `is_physical_connection_admission_timeout()` remains authoritative for
real physical admission timeout and maps to `TransportError::PoolTimeout`.

Then audit Eggfetch 0.2.0's residual `Error::Pool` construction sites against
EggPool's client builder invariants.

The desired local end state is no message-text inspection. If the audit
confirms that valid runtime admission timeout is already fully identified by
the typed helper and the remaining `Pool` cases represent invalid
construction/internal pool setup, map the residual `Pool(_)` case
conservatively to `TransportError::Configuration`.

Add a unit regression proving:

- typed physical admission timeout still maps to `PoolTimeout`;
- residual synthetic `Pool` error maps to the chosen fail-closed category;
- no `Display` string content changes that classification.

If upstream source evidence shows a legitimate transient runtime `Pool` case
that is not covered by the typed admission helper, stop this part of M001
rather than guessing or broadening message parsing. Record that finding and
move the classification change into M002.

### Existing source-chain classification

Leave the current typed source-chain checks for:

- `hyper::Error::is_canceled()`;
- Hyper parse/incomplete-message facts;
- nested `rustls::Error`;
- `UnexpectedEof` protocol evidence.

Do not add new downcast families in M001 unless a regression test demonstrates
a current misclassification. Their replacement requires the upstream M002
interface.

## 7. Ordered work packages

### Work package A — Freeze exact baseline and re-audit Eggfetch pool semantics

Intent:

Confirm the M001 assumptions against the exact locked Eggfetch source before
editing classification.

Required changes:

- none initially.

Acceptance evidence:

- current baseline SHA and locked `eggfetch-core 0.2.0`;
- source locations or tests demonstrating what
  `is_physical_connection_admission_timeout()` covers;
- inventory of `Error::Pool` construction paths reachable under
  `Client::execute_http_body`;
- explicit verdict whether the residual branch can map to
  `Configuration` without string inspection.

Stop if that verdict is not supportable.

### Work package B — Preserve trailer frames without changing DATA callers

Intent:

Make the native response-frame contract faithful and explicit.

Required changes:

- add bounded body-local trailer storage;
- capture trailer frames while `next()` continues to return only DATA;
- add an additive non-draining trailer accessor;
- use a safe/manual `Debug` implementation if needed.

Acceptance evidence:

- ordinary body behavior unchanged;
- declared HTTP/1.1 trailer is retained;
- DATA before the trailer is unchanged;
- duplicate trailer values remain distinguishable if the fixture supplies them;
- accessor before trailer observation does not synthesize data;
- no raw trailer values in `Debug`.

### Work package C — Remove residual pool message parsing

Intent:

Make the local classification boundary typed/fail-closed.

Required changes:

- remove `message.contains("max_live")` or equivalent display-text matching;
- retain typed admission helper precedence;
- map only according to evidence from work package A.

Acceptance evidence:

- focused synthetic mapping tests;
- physical pool-pressure real-socket test still produces `PoolTimeout`;
- invalid construction remains rejected before client build;
- grep/static review finds no new Eggfetch error-display parsing.

### Work package D — Real-socket trailer qualification

Intent:

Prove M001 against actual HTTP/1.1 framing rather than synthetic frames alone.

Required changes:

Extend the existing fixture in `rust/tests/provider_transport.rs` with a
response mode that emits a standards-valid chunked response with a declared
trailer, for example structurally:

```text
HTTP/1.1 200 OK
Transfer-Encoding: chunked
Trailer: X-Eggpool-Test-Trailer

<chunked DATA>
0
X-Eggpool-Test-Trailer: <bounded fixture value>
```

Use a non-secret deterministic fixture value.

Acceptance evidence:

- DATA bytes match the equivalent non-trailer response;
- `next()` reaches EOF normally;
- retained trailer exists after its frame is consumed;
- no trailer is exposed as DATA;
- connection reuse/release remains correct after trailer completion.

### Work package E — Wire/coordinator non-regression

Intent:

Prove that internal trailer observability did not become public wire behavior or
change attempt/finalization semantics.

Acceptance evidence:

Run the current focused coordinator/wire targets. Existing JSON/SSE responses
must remain byte/semantic compatible with their tests. No new downstream
`Trailer` header or trailer section should appear unless a pre-existing test
already intentionally emits one.

### Work package F — Broad repository qualification and documentation

Intent:

Close on repository authority.

Required changes:

- update current provider deep-dive/development guidance where the current
  trailer/error behavior is described;
- create
  `plans/closure/provider-transport/001-status.md` during closure, not during
  implementation;
- update roadmap/registry status only with actual evidence.

Acceptance evidence:

- §11 command set green;
- documentation matches implementation;
- closure record contains the requirement-to-evidence matrix and residual M002
  blocker.

## 8. Failure, cancellation, restart, contention semantics

Trailer state is owned exclusively by one `ProviderBody`. No shared mutable
state is introduced.

Cancellation/drop semantics:

- dropping the body before EOF may discard unobserved trailers; this is correct
  because the response was not fully consumed;
- the drop must still release Eggfetch's body/pool lease exactly as today;
- the trailer accessor must never drive I/O or keep the connection alive after
  normal body ownership would end.

Read failure semantics:

- a body error before trailers are received returns the same
  `TransportError` as today;
- do not synthesize an empty/success trailer state as evidence of complete
  application response;
- previously observed trailer metadata, if any, does not override a later body
  error.

Restart/reload:

- no state persists across requests or generations;
- no config/reload classification changes.

Contention:

- no mutex/channel/global cache is authorized;
- per-body `HeaderMap` storage must remain request-local.

## 9. Compatibility and migration

No data/config migration.

Rust API compatibility should be additive:

- existing `ProviderBody::next()` callers compile unchanged;
- `read_to_bytes` behavior remains DATA-only and bounded;
- any new trailer accessor is optional;
- no existing `TransportError` variant is removed or renamed.

Public HTTP compatibility must remain unchanged. EggPool does not begin
forwarding provider trailers to clients in M001.

If repository evidence shows that adding an accessor changes a documented
semver/public crate contract in a way that requires a larger compatibility
decision, stop and record the evidence rather than silently widening scope.

## 10. Required tests

Add/adjust focused tests in `rust/src/providers/transport.rs` and
`rust/tests/provider_transport.rs` for:

- DATA + declared trailer + EOF;
- ordinary body with no trailers;
- trailer not returned as DATA;
- trailer accessor move/take semantics;
- duplicate trailer-value preservation if representable by the fixture;
- cancellation/drop before trailer observation;
- redacted/manual `Debug`;
- typed admission timeout remains `PoolTimeout`;
- residual `Pool` mapping is independent of message text;
- existing invalid `max_connections == 0` configuration rejection.

Preserve existing tests for:

- direct/proxied request shape;
- keepalive/idle expiry;
- read timeout/premature close;
- pool pressure/cancellation recovery;
- direct TLS and hostname verification;
- account pool isolation;
- Eggress route corpus and fail-closed behavior.

Run all Rust tests serially where the repository requires it.

## 11. Required verification commands

Focused provider transport:

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support \
  --test provider_transport -- --test-threads=1
```

Focused coordinator/wire non-regression:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
```

Reduced/default/full repository gates:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets \
  --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1

cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Static review:

```bash
rg -n 'contains\("max_live"\)|EggfetchError::Pool|ProviderBody|into_trailers|take_trailers' \
  rust/src rust/tests architecture .opencode
```

Use exact current test target names if repository reality changes before
execution. Closure must record commands actually run rather than copying this
list as presumed evidence.

## 12. Documentation updates

Inspect and update current-authority text where applicable:

- `architecture/deep-dive-providers.md`;
- `.opencode/skills/architecture/SKILL.md`;
- `.opencode/skills/development/SKILL.md`;
- `rust/src/providers/transport.rs` module/API comments.

Documentation must state:

- `next()` is DATA-only compatibility behavior;
- provider trailers are retained as transport metadata only;
- EggPool does not currently forward or interpret provider trailers;
- physical admission timeout remains typed and distinct from residual pool
  construction/internal errors;
- broader upstream error-taxonomy cleanup is deferred to provider-transport
  M002.

Do not rewrite legacy Plans 215–220 or 241.

## 13. Acceptance criteria

M001 is acceptable only when all are true:

1. Existing `ProviderBody::next()` callers require no change.
2. A valid HTTP/1.1 trailer is retained and retrievable after observation.
3. Trailer metadata is never returned as DATA.
4. Trailer presence does not alter `read_to_bytes` size accounting.
5. No downstream JSON/SSE response begins forwarding provider trailers.
6. Trailer values are absent from newly affected `Debug`/diagnostics.
7. The residual Eggfetch `Pool` mapping contains no message-text inspection.
8. Real physical admission pressure still classifies as `PoolTimeout`.
9. Existing direct/proxy/cancellation/TLS/protocol qualification remains green.
10. No new Eggfetch feature family or dependency is introduced.
11. Default and no-default repository gates pass.
12. Closure records M002 as a separate blocked upstream-interface follow-up,
    not as incomplete M001 scope.

## 14. Stop conditions

Stop and report rather than improvise if:

- Eggfetch 0.2.0 does not expose trailer frames through the current native body
  path as expected;
- trailer preservation requires changing Eggfetch or Hyper internals;
- residual `Error::Pool` has a legitimate transient runtime meaning not
  distinguishable without text parsing;
- preserving trailers would require downstream wire/API forwarding;
- the work requires a new protocol, retry owner, proxy owner, or durable
  dependency decision;
- tests show coordinator attempt counts, pool ownership, cancellation recovery,
  or TLS/proxy classification changed;
- scope expands into updater, server, runtime lifecycle, or persistence work.

A stop condition produces a new plan/upstream prerequisite; it does not justify
weakening current invariants.

## 15. Closure evidence required

Create `plans/closure/provider-transport/001-status.md` with:

- implementation commit SHA(s);
- exact locked Eggfetch/Eggress versions;
- requirement-to-evidence matrix for all §13 criteria;
- focused trailer tests and their outcomes;
- pool-error audit evidence and final mapping rationale;
- provider transport default + `test-support` results;
- coordinator/wire focused results;
- no-default and serial workspace results;
- `cargo deny` and feature/dependency graph results;
- security review confirming no new trailer/credential/body diagnostic leak;
- compatibility statement that existing DATA callers/wire surfaces did not
  change;
- severity-tagged unresolved findings;
- explicit disposition;
- M002 blocker status and whether upstream work has been planned separately;
- registry/roadmap updates.

Do not claim closure from unit tests alone.

## 16. Handoff notes

- Preserve unrelated user changes.
- Rust integration tests are run serially (`--test-threads=1`) per repository
  policy.
- Reuse `rust/tests/provider_transport.rs`; do not build a second HTTP fixture
  framework for one trailer case.
- Keep the fixture trailer value deterministic and non-sensitive.
- Do not add logging to prove trailer capture; inspect it through the API/test.
- Do not touch the completed legacy Eggfetch migration plans.
- Do not implement provider-transport M002 locally unless Eggfetch first
  publishes the required general-purpose typed classification surface.
