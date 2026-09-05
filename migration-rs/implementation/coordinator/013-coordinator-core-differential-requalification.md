# C013 — Coordinator Core Differential Requalification

Status: queued behind C012

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant/corrective

Hard dependency: accepted C012 closure.

## Objective

Independently requalify the corrected C003-C006 coordinator core against the accepted C001 Python contract before C007 finite-response handoff work resumes. C013 exists because the original C003-C006 closures used a very small focused Rust matrix and asserted broader parity than the evidence demonstrated.

C013 is evidence work plus only narrowly-scoped fixes required to make the evidence pass. If the requalification exposes a new architectural defect, stop and add a new corrective plan rather than hiding it inside the closure record.

## Requalification principle

Do not prove only that helper functions can be constructed. Exercise the real Rust ownership path for each qualified behavior:

`M5 selected claim -> C002 durable publication -> corrected C003 resolution -> corrected C004 preparation/submission fixture -> corrected C005 classification/effects -> corrected C006 attempt/request finalization/runtime release`.

Where provider I/O is unnecessary to a case, inject typed transport/response evidence at the C004/C005 boundary. Where upstream request shape matters, use deterministic local HTTP/HTTPS/proxy fixtures through M4.

## 1. C001 failure/effect differential matrix

Consume the committed C001 Python observation corpus and compare every policy-bearing case against Rust. At minimum cover the existing 23 failure/effect observations plus any C012 fixture additions.

Assertions must compare semantic fields, not merely category names:

- retry legality and next action;
- account vs wire vs wait vs terminal scope;
- client outcome;
- account credential/health effect;
- model quarantine/withdrawal effect;
- circuit/probe effect;
- durable backoff decision and bounded Retry-After;
- wire rejection/learning/negotiation-delay effect;
- downstream-start no-replay fact.

Mandatory distinct pairs include:

- ambiguous 401 vs explicit invalid credential;
- generic 404/path mismatch vs strong model absence;
- deterministic wire rejection vs ordinary bad request;
- 429 with valid/invalid/missing Retry-After;
- transport connect/proxy/TLS/write/read/pool phases;
- pre-handoff 408/5xx vs identical evidence after downstream start;
- local/adaptation error vs provider-originated error.

No normalization may erase account/model/wire/effect meaning.

## 2. Wire resolver state and concurrency matrix

Exercise the actual corrected resolver under fake/injected time.

Required cases:

- configured priority only;
- operator-fixed preference;
- metadata/bundled hint;
- learned success preference;
- precedence interactions among fixed/hint/learned/configured order as frozen by Python;
- learned TTL expiry;
- rejection cooldown expiry;
- structural fingerprint change;
- rate-limit negotiation delay and expiration;
- LRU/capacity eviction for entries first created by `resolve`, `accept`, and `reject`;
- bounded provider-gate/last-negotiation state;
- leader/follower shared result;
- leader cancellation;
- follower cancellation;
- gate saturation and recovery;
- concurrent independent providers;
- repeated finish/accept/reject without leaked flight or permit state.

After each concurrency case, assert flights/permits and bounded state return to the expected baseline.

## 3. Provider-attempt identity/header/submission matrix

Use deterministic M4-backed targets to verify the corrected C004 boundary.

Required cases:

- canonical model equals upstream model;
- canonical alias differs from provider-native `upstream_model_id` and the server rejects the canonical alias;
- finite and stream path selection;
- every M6-supported selected wire profile needed by M7;
- static provider headers;
- wire-surface headers;
- auth header/scheme precedence;
- allowed forwarded incoming headers;
- denied authorization/hop-by-hop/client-controlled headers;
- request/correlation ID behavior;
- missing credential failure before send;
- direct account and proxied account client selection;
- exactly one upstream request per C004 invocation;
- transport failure remains typed evidence with no local replay;
- cancellation at deterministic connect/write/header boundaries where the M4 fixture supports it;
- bounded request-ID/byte/timing evidence;
- Debug/error output does not contain synthetic credentials, authorization values, provider body text, or proxy secrets.

The alias test must assert the actual target-observed path/body uses the provider-native ID required by the selected profile.

## 4. Effect lifecycle and boundedness matrix

Exercise enough completed attempts to exceed any internal effect registry/cache capacity several times.

Prove:

- one attempt's effect token cannot mutate another attempt;
- compatible duplicate effect application is idempotent;
- completion/finalization retires process-local effect state;
- process-local effect bookkeeping remains at or below its documented bound;
- capacity exhaustion fails before accepting ownership that cannot later converge;
- retry/finalization does not double-apply quota, health, circuit, model, or credential effects;
- no request/provider bodies are retained in effect state.

Use structural snapshots/counters rather than RSS as the primary correctness assertion. Resource characterization belongs to M10.

## 5. Durable finalization fault matrix

Fault or synthesize each meaningful durable/runtime boundary and verify actual re-read/convergence behavior.

Required cases:

- normal request completion;
- retryable failed attempt leaves parent pending;
- duplicate compatible finalization;
- request terminal conflict;
- missing request row;
- missing attempt row;
- missing reservation row;
- attempt already terminal with compatible facts;
- attempt already terminal with incompatible identity/facts;
- reservation already released compatibly;
- reservation unexpectedly active after an alleged completed release;
- database failure before transaction, inside transition, and on re-read;
- quota release failure after durable commit then successful resume;
- active-count release failure then successful resume where injectable;
- health-probe release failure/ownership mismatch then successful resume where injectable;
- cancellation of all external waiters after terminal command registration;
- compatible concurrent supervisor registration shares one job;
- incompatible concurrent supervisor registration fails closed;
- supervisor capacity exhaustion before ownership transfer;
- bounded retry exhaustion remains observable and does not report completion.

Every case must assert durable rows plus M5 active/quota/probe state rather than relying only on a `FinalizationResult` boolean.

## 6. Retry replacement ownership invariant

Build an integrated two-attempt fixture proving:

1. attempt 1 is durably published;
2. attempt 1 fails pre-handoff with retryable evidence;
3. C005 authorizes replacement only after attempt 1 finalization/reservation convergence is complete or retained ownership is explicitly accepted by the contract;
4. attempt 2 then acquires a fresh M5 claim and durable identity;
5. no reservation, active count, quota estimate, health probe, wire flight, or effect token from attempt 1 remains incorrectly owned;
6. successful attempt 2 finalization leaves the whole request in one compatible terminal state.

Add a negative race fixture that tries to acquire/publish replacement ownership before attempt-1 cleanup reaches the required boundary and prove it cannot bypass the invariant.

## 7. Python/Rust oracle strategy

Prefer extending `tests/migration_rs/coordinator_fixtures.py` and the committed scalar observation format rather than creating a second oracle system. Add fields only when needed to represent accepted C001 semantics.

Exact comparisons should include state/action/effect vocabulary, identity relationships, bounds/counters, response-start fact, and durable terminal/release state. Normalize only injected time values, synthetic row IDs, and non-semantic exception wording.

All committed observations remain secret-safe: no API keys, auth values, proxy credentials, prompts, responses, raw provider bodies, session IDs, host paths, process IDs, or arbitrary exception prose.

## 8. Regression and dependency review

C013 must rerun the existing closed-boundary suites so the corrective pass does not regress M4-M6 or C001-C002.

Required verification:

```text
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted Python coordinator/failure/wire/finalization suites> -q --tb=short --maxfail=1
uv run ruff format --check tests/migration_rs
uv run ruff check tests/migration_rs
uv run pyright <touched Python fixture/test paths>
git diff --check
```

Review `cargo tree` if C012 changed dependencies. No new database schema, second HTTP/TLS stack, workflow/actor framework, live provider prerequisite, Docker prerequisite, or broad CI matrix is expected.

## Acceptance criteria

C013 closes only when:

- all C001 policy-bearing failure/effect cases are semantically differential;
- ambiguous auth, model absence, wire mismatch, 429, transport phases, and response-start boundaries match Python;
- corrected wire ordering, delay, cancellation, and bounds are runtime-qualified;
- provider-native model aliasing and full C004 header/request evidence are target-observed through M4 fixtures;
- effect bookkeeping is demonstrably bounded/retired;
- finalization never reports convergence without compatible durable truth;
- compatible/incompatible supervisor sharing is correctly distinguished;
- replacement attempt ownership cannot race ahead of required prior-attempt cleanup;
- full Rust and migration regression suites are green;
- no unresolved high/medium correctness/security finding remains in C003-C006;
- C007 can consume the corrected APIs without a known ownership/policy defect.

## Registry transition

On accepted closure:

1. write `migration-rs/closure/coordinator/013-status.md` with the complete evidence matrix;
2. retain C003-C006 closure records as historical evidence for the original implementation;
3. mark C012/C013 complete;
4. mark the M7 core correction closed;
5. promote C007 back to the sole dependency-ready plan;
6. keep C008-C011 and M8 behind their existing serial gates.

C013 does not close M7; C011 remains the aggregate M7 closure plan after C007-C010.