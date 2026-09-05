# C012 — Coordinator Core Contract Correction

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant/corrective

Hard dependencies: C001 and C002 accepted; historical C003-C006 implementation present.

## Objective

Correct the bounded post-C006 audit findings in the coordinator core before any finite-response handoff work proceeds. This plan does **not** redesign M7 or discard the useful C003-C006 implementation. It closes the specific gaps between the accepted C001 coordinator contract and the Rust wire-resolver, provider-attempt, failure/effects, and finalization boundaries implemented in `97a4846`.

C003-C006 closure records remain append-only historical evidence. C012 supersedes their aggregate “no unresolved mandatory findings” conclusion only for the findings named here.

## Accepted findings

The corrective implementation must address all of these findings together because they meet at one attempt lifecycle boundary:

1. `FailureObservation`/`FailureEffects` are narrower than the accepted C001 failure/effect contract, and current classification cannot distinguish ambiguous authentication responses from explicit invalid-credential evidence.
2. The process-lifetime attempt effect ledger is monotonically growing and has no lifecycle retirement/cap.
3. `WireResolver` omits accepted ordering/delay inputs and can create state through accept/reject paths without applying the same LRU bound used by normal resolution; provider gate/negotiation state also lacks explicit lifecycle bounds.
4. Provider-attempt construction loses M5 `upstream_model_id` and therefore can send/expand the canonical alias instead of the provider-native model ID.
5. Provider-attempt construction does not yet carry the complete header/forwarding/request-ID/evidence boundary required by C004.
6. Durable finalization can report attempt/reservation convergence after a zero-row conditional update without re-reading durable truth.
7. The retained finalization supervisor coalesces duplicate keys without proving terminal commands are compatible.
8. Finalization progress/runtime release and failure-effect convergence are too implicit for the C001/C006 ownership contract.

If implementation review shows one of these observations is inconsistent with the current Python oracle, stop and update the contract only through an explicit reviewed supported-difference decision. Do not silently narrow C001 to match the current Rust code.

## C003 correction — wire resolution and negotiation

Preserve the current small `WireResolver` design, but complete the accepted inputs and bounds.

Required behavior:

- represent operator-fixed wire preference separately from learned preference;
- preserve metadata/bundled wire hints when present in the C001/Python oracle;
- keep deterministic configured priority as the final stable ordering input;
- preserve candidate fingerprint invalidation when structural candidate/config/request constraints change;
- support provider negotiation delay after rate-limit evidence without making the resolver interpret HTTP responses itself; C005 supplies the authorized delay transition;
- apply the same capacity discipline to entries created by `resolve`, `accept`, and `reject`;
- bound or lifecycle-scope provider gates, last-negotiation timestamps, flights, learned entries, rejection entries, and any metric-label state;
- leader/follower/throttled semantics and cancellation ownership remain as already implemented;
- resolver performs no provider I/O, database writes, raw body inspection, or account-health mutation.

Prefer one bounded keyed state container rather than several independently unbounded maps. Do not add a generic cache crate unless the existing `BTreeMap`/`VecDeque` approach is demonstrably insufficient.

Tests must include fixed preference vs hint vs learned ordering, TTL expiry, rejection cooldown, structural fingerprint change, LRU eviction through every insertion path, rate-limit delay, concurrent providers, leader cancellation, follower cancellation, gate-cap recovery, and repeated accept/reject/finish.

## C004 correction — provider-native identity and request construction

Keep canonical/client model identity and provider-native model identity distinct from claim acquisition through upstream submission.

Required behavior:

- carry the selected `upstream_model_id` explicitly into `AttemptInput`/`PreparedUpstreamAttempt` (or an equivalent immutable attempt DTO);
- do not repurpose the durable canonical `model_id` field if doing so would change existing schema semantics;
- use provider-native model identity for M6 target-model adaptation and `{model}`/`{model_id}` provider path expansion where the Python oracle does;
- preserve the original admitted canonical request as the source for every retry; never transcode a previously translated provider body;
- match Python static-header, auth-header, surface-header, and forwarded incoming-header allow/deny precedence;
- preserve request/correlation-ID forwarding/generation semantics without exposing secrets;
- carry the structural timeout/evidence inputs needed by later C007/C008 policy without moving timeout scheduling into C004;
- extract bounded upstream request-ID and byte/timing evidence available at the C004 boundary;
- continue to use exactly one M4 provider/account client and exactly one `send` per invocation;
- no second HTTP stack, direct-proxy bypass, implicit retry, or provider SDK.

Add a mandatory alias/remap fixture where canonical model ID and `upstream_model_id` differ and the target rejects the canonical alias. The test must prove the provider-native ID is present in both the encoded request/path positions required by the selected wire profile.

Header tests must cover static/auth/surface precedence, allowed forwarded headers, denied hop-by-hop/auth/client-controlled headers, missing credential, request ID, direct and proxied account-client selection, and Debug/error redaction.

## C005 correction — complete failure/effect policy

Rebuild the policy DTOs around the accepted C001 failure contract rather than adding provider-specific `if` statements to the current reduced classifier.

`FailureObservation` (or equivalent) must carry the policy-bearing structural facts required to disambiguate outcomes, including as applicable:

- attempt identity/number;
- source and typed transport phase/error class;
- HTTP status;
- provider/account/model/upstream-model identity;
- client/upstream protocol and selected wire surface/candidate;
- bounded normalized provider signal/evidence class;
- parsed bounded Retry-After;
- model-presence/absence evidence and alternate availability when required by the frozen classifier;
- downstream response-start fact;
- stream terminal evidence for post-handoff terminal classification.

Do not retain raw provider bodies or arbitrary provider prose.

`FailureEffects` must express the authoritative result needed by later application/finalization, not only three booleans. Preserve at least the C001 distinctions for retry action/scope, client outcome, account effect, model effect, circuit/probe effect, durable backoff, wire learning/rejection effect, and bounded Retry-After value.

Mandatory semantic cases include:

- client/local preparation error: no provider penalty;
- transport connect/proxy/TLS/write/read/pool failure before handoff: account-scoped failure/backoff/circuit effect as defined by Python;
- bare/ambiguous 401: **no credential disable and no health advancement**;
- explicit invalid/expired/revoked credential signal: selected-account credential effect only, with retry scope per oracle;
- deterministic wire/surface/schema rejection: alternate wire on the same account when legal, without account penalty;
- strong model absence: model-scoped effect, not wire enumeration;
- 429/rate pressure: bounded Retry-After/backoff and negotiation-delay effect without deterministic candidate rejection unless separately authorized;
- 408/5xx/transient provider failure: exact Python account/model effects;
- any failure after downstream start: terminal/no transparent replay regardless of otherwise retryable category.

### Exactly-once effects without an unbounded ledger

The current process-lifetime `BTreeSet<attempt_id>` must not grow forever. Use one of these bounded patterns, in preference order:

1. carry effect-application progress in the retained attempt/finalization command and retire the process-local entry once the attempt is durably terminal and runtime effects converge;
2. use a bounded attempt-effect registry with explicit completion retirement and capacity failure before effect ownership is transferred;
3. another design only if it proves the same lifecycle and restart behavior without a schema fork.

Do not add a Rust-only database column/table solely for effect idempotency. C010 later owns restart reconciliation; existing M5 effect APIs should remain idempotent/conditional where durable state already provides the natural boundary.

A stress test must process well beyond the configured registry capacity while retiring completed attempts and prove state returns to a bounded baseline.

## C006 correction — durable truth, supervisor compatibility, and explicit progress

Conditional SQL updates are not sufficient evidence of convergence when they affect zero rows.

Required behavior:

- after a request, attempt, or reservation conditional transition affects zero rows, re-read the row and verify compatible terminal truth;
- missing identity, wrong parent/account/model relationship, incompatible terminal state, or an active reservation after a claimed release is a typed invariant/conflict result—not `converged=true`;
- preserve retryable failed-attempt behavior: attempt/reservation terminal, parent request pending;
- runtime ownership components (quota reservation, active count, health probe, pending state where applicable) release independently and idempotently;
- a partial runtime release failure remains resumable and cannot mark the command complete;
- carry explicit finalization progress sufficient to distinguish durable request transition, attempt transition/observation, reservation convergence, effect convergence, and runtime component release;
- compatible duplicate commands share retained work; incompatible commands for the same request/attempt key fail closed **at registration or before observing another command's success**;
- command compatibility must include terminal outcome and any other immutable facts that would make sharing unsafe; do not compare secret/body content;
- bounded retry delay must be injectable for tests; no perpetual scheduler is introduced;
- normalized M6 usage/cost/effect inputs needed by later C007/C008 may be represented now, but C012 must not invent response parsing or endpoint behavior.

Review existing schema-qualified token/cache/cost fields before changing finalization persistence. If the accepted Python contract requires a field already present in schema 54, populate it. If the schema truly lacks a mandatory field, stop for review rather than adding a convenience migration in this corrective pass.

## Cross-cutting fault and cancellation cases

At minimum, add deterministic tests for:

- alias/canonical model divergence;
- ambiguous vs explicit credential failure;
- model absence vs wire mismatch;
- rate-limit negotiation delay;
- response-start no-replay;
- effect-registry capacity/retirement;
- wire-state insertion/eviction through resolve/accept/reject;
- cancellation of negotiation leader/follower;
- missing request/attempt/reservation rows during finalization;
- already-terminal compatible and incompatible rows;
- runtime release failure after durable commit and successful resume;
- concurrent compatible and incompatible supervisor registration;
- cancellation of all external finalization waiters while retained work converges.

## Scope boundaries

C012 must not implement:

- C007 finite response handoff;
- C008 streaming timeout/downstream iterator policy;
- C009 public inference endpoint wiring;
- C010 restart scanning/scheduling;
- M8 generation publication, rehash, signal handling, shutdown orchestration, or recurring background loops.

No new database schema, HTTP stack, actor/workflow framework, ORM, async runtime, or broad CI matrix is expected.

## Verification

Required before closure:

```text
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted Python failure/wire/finalization oracle tests> -q --tb=short --maxfail=1
uv run ruff check tests/migration_rs
uv run pyright <touched Python fixture/test paths if any>
git diff --check
```

Use existing local deterministic HTTP/proxy fixtures; no paid/live provider is a normal prerequisite.

## Acceptance criteria

C012 closes only when:

- C003 ordering/delay/state bounds satisfy C001;
- provider-native model identity survives to the actual M4 submission;
- C004 header/identity/evidence construction matches the Python boundary;
- all C001 policy-bearing failure dimensions are represented and ambiguous 401 behavior is correct;
- effect idempotency state is bounded and retires;
- zero-row finalization transitions are re-read and validated;
- incompatible retained terminal commands cannot silently share success;
- partial runtime/effect release remains resumable;
- no retry is introduced below the coordinator and no replay is legal after downstream start;
- no schema/dependency/M8 scope expansion is required;
- focused and full regression suites are green.

C012 closure does **not** restore C007 readiness by itself. C013 must independently requalify the corrected C003-C006 boundary against the C001 oracle.

## Closure

On completion write `migration-rs/closure/coordinator/012-status.md`, record implementation commits and requirement-to-evidence mapping, then promote C013 as the sole dependency-ready coordinator plan. Historical C003-C006 closure records remain unchanged.