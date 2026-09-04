# D008 — Differential Qualification and M5 Closure

Status: closed; D001-D008 closed

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d008--m5-differential-qualification-and-closure`

Primary class: invariant

## 1. Objective

Qualify the complete M5 state/policy layer as an integrated Rust dependency for M6/M7. D008 must prove that the same deterministic account/config/database/request-facts scenarios produce parity-equivalent account/catalog snapshots, quota pressure, health/backoff/quarantine state, routing candidates/exclusions/ranking/fairness, local claim ownership, and compiled model-router/affinity behavior.

D008 is not a place to finish missing predecessor implementation. Material gaps receive bounded corrective plans.

## 2. Preconditions

Do not start closure qualification until D001-D007 each has an accepted closure record and the registry marks all hard predecessors complete.

Re-read the D001 parity classifications and all supported-difference ADRs. A behavior cannot be normalized away merely because Rust uses different containers, locks, float formatting, or time APIs.

## 3. Integrated fixture generations

Build a small set of generation-style test scenarios that instantiate together:

- validated Rust config;
- schema-54 database copy;
- D002 account registry/catalog cache;
- D003 catalog refresh fixture/M4 provider clients when the scenario needs refresh;
- D004 quota/usage/claim state;
- D005 health/backoff/circuit/quarantine;
- D006 router/fairness/local claim coordinator;
- D007 model-router registry/affinity.

Do not need M6 canonical requests. Feed the frozen D001 `RoutingRequestFacts`/affinity identity DTOs directly.

## 4. Required integrated scenarios

At minimum qualify:

### Healthy multi-account routing

Two or more same-priority accounts support one model. Vary weight, request/token persisted usage, pending load, active count, and native/transcode state. Compare ranked candidates, fairness band, selection, pending-claim publication, rollback, and next selection.

### Strict priority fallback

Higher-priority account is healthy, then cooldown/quarantined/stale/unsupported. Prove lower tier is ignored while higher tier is claimable and becomes eligible only when all higher-tier candidates are excluded.

### Catalog uncertainty

Start with durable working support. Apply failed, malformed, partial, and empty non-authoritative refreshes and prove routing support remains. Apply confirmed authoritative withdrawal and prove the exact support disappears; then reappearance restores it and clears bounded quarantine where appropriate.

### Quota authority split

Place an account above local estimated capacity in default score-only mode and prove it remains routable but loses score. Repeat with hard-cap mode. Separately apply provider-observed quota exhaustion and prove authoritative health exclusion regardless of local mode.

### Backoff/restart

Persist active quota/rate/generic/model backoffs in Python-compatible SQLite, hydrate Rust at controlled wall/monotonic time, prove remaining duration/category/scope, advance to expiry, and compare eligibility. Include the 1,800-second clamp and terminal auth reset path.

### Circuit half-open concurrency

Open one account's circuit, advance into half-open eligibility, launch competing local claims, and prove exactly one probe is acquired. Cancel/rollback it and prove another claim can acquire without leaked pending/active ownership.

### Model quarantine

Promote a precise provider/account/model/protocol key from suspected to quarantined, verify exclusion, expire/recover, clear on exact success, and clear/terminally withdraw under authoritative catalog evidence. Sibling provider/account/model keys remain unaffected.

### Fairness boundedness

Exercise round-robin across near ties, random with seeded RNG, scope-key changes, native/transcode separation, strict tiers, and more than 4,096 fairness keys. Prove bounded map convergence and stable post-eviction behavior.

### Selection claim contention

Use deterministic synchronization to start multiple selectors simultaneously. Prove each accepted selection observes previously published pending load, no claim owns partial active/pending/probe state, and rollback/conversion are exact.

### Model-router affinity

Resolve a virtual router through a fake selector closure, cache the concrete model, route that concrete model through D006, repeat as a hit, expire/invalidate config, and prove single-flight/TTL/LRU behavior without raw session retention. Do not call a live selector model.

## 5. Contract-to-evidence matrix

Create `migration-rs/closure/routing-domain/008-status.md` with a matrix mapping every M5 roadmap invariant to concrete Rust tests and Python oracle fixtures. Include at least:

- secret-free account identity;
- catalog non-destructive uncertainty;
- per-account freshness;
- provider/model protocol/capability identity;
- request/token score formula and local hard-cap policy;
- pending/reserved claim ownership;
- learned-state caps;
- 30-minute backoff cap and Retry-After policy;
- auth/operator terminal behavior;
- read-only circuit checks and one half-open probe;
- exact-key quarantine lifecycle;
- stable routing exclusions;
- strict priority tiers;
- fairness-band/cap behavior;
- local claim atomicity;
- bounded model-router fingerprint/affinity/single-flight;
- M6/M7 boundaries.

Any mandatory row with only construction/static evidence must be called out rather than marked pass.

## 6. Restart/database compatibility

Run sequential compatibility tests using copies of the same DB:

1. Python creates/hydrates representative M5 state -> Rust reads equivalent state;
2. Rust performs M5-owned durable mutations (catalog freshness/model metadata/backoff/quarantine/repository operations) -> Python reads equivalent semantics;
3. Rust restarts and reconstructs the same effective routing state given controlled time;
4. expired nonterminal state is not resurrected;
5. corrupt mandatory state fails closed without modifying the source DB.

Do not run Python and Rust simultaneously against one writable fixture database.

## 7. Concurrency qualification

Use multithread-capable Tokio tests where practical even if the current binary still uses a current-thread runtime. M8 may change runtime scheduling, so M5 ownership must not depend on one-thread accident.

Test:

- claim serialization;
- quota snapshot mutation;
- circuit probe races;
- catalog refresh serialization;
- model-router single-flight;
- cancellation at each local async lock wait.

No lock may be held across M4 network I/O or SQLite I/O in the local claim transaction. Use instrumentation/assertions where possible rather than code-review-only claims.

## 8. Failure isolation corpus

Feed invalid/unexpected state at each boundary:

- malformed catalog JSON/metadata;
- unknown account/provider/model identity;
- non-finite quota numbers;
- durable expiry far in the future/past;
- corrupt backoff/quarantine enum/identity;
- duplicate claim release/conversion;
- fairness/affinity cap pressure;
- selector closure error/cancel;
- one catalog account network failure.

Expected result is a typed local failure, skipped advisory metadata, or stable exclusion according to the frozen contract. No input should leave the router permanently wedged or require restart/database refresh to recover unless the database integrity is genuinely indeterminate under the existing fatal DB policy.

## 9. Resource/dependency review

Review `Cargo.toml`/`cargo tree` after M5:

- no Reqwest/second TLS stack;
- no ORM;
- no actor framework/DI framework;
- no duplicate proxy implementation;
- no scheduler/background framework added to M5;
- bounded maps retain documented caps;
- selection has no per-candidate SQLite query;
- no per-entry timer/task for backoff/quarantine/fairness/affinity.

Characterize representative local cost for an SBC-like state size (for example tens of accounts and hundreds/thousands of model/provider rows). Record RSS and routing-plan/claim latency as observations, not unsupported pass/fail thresholds. Investigate obvious allocation/task/thread explosions before closure.

## 10. Security review

Scan test markers through:

- account/API key config;
- proxy credentials inherited from M4 state;
- catalog auth headers;
- failure/backoff details;
- routing traces;
- affinity/session input.

Assert secrets/raw session content are absent from Rust `Debug`/`Display`, structured snapshots, errors, routing diagnostics, and closure fixtures.

A routing trace may contain account/provider/model identifiers, score components, reason codes, and hash/fingerprint identifiers only as allowed by current product behavior.

## 11. Public/read-plane integration

Where F005 already exposes health/readiness/model/stats/dashboard slices whose backing state is now available, add only the minimum wiring needed to prove M5 state can serve those existing read surfaces without redesign. Do not expand the dashboard or inference placeholder endpoints as part of D008 unless a predecessor plan explicitly owns that parity surface.

`/v1/chat/completions`, `/v1/responses`, and `/v1/messages` remain non-dispatch placeholders until M6/M7.

## 12. Verification commands

Record successful execution of at least:

- `cargo fmt --manifest-path rust/Cargo.toml -- --check`;
- `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`;
- `cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1`;
- any targeted multithread/concurrency suite separately if serialized global fixtures require the primary suite to use one thread;
- complete `tests/migration_rs` suite;
- targeted Python account/catalog/quota/health/failure/routing/model-router tests;
- Python smoke suite for unaffected production behavior;
- schema/checksum compatibility tests;
- `cargo tree --manifest-path rust/Cargo.toml -e features` dependency review;
- declared Rust 1.85 check;
- `git diff --check`.

No broad cloud CI matrix, live paid provider, or load farm is required.

## 13. Closure outputs

On success:

- write the D008 closure record and matrix;
- mark D001-D008 completed in `migration-rs/registry.md` with implementation/closure commits;
- mark the routing-domain roadmap `closed after D008 qualification`;
- update the implementation handoff README/sequence;
- explicitly mark M6 planning/implementation handoff work unblocked;
- do not mark M7 implementation dependency-ready solely because M5 closed: M7 still depends on M6 canonical request/codec work.

If qualification finds a bounded defect, write D009 or another clearly named corrective plan and keep aggregate M5 open. Do not rewrite historical closure records to hide the finding.

## 14. Acceptance criteria

D008/M5 closes only if:

- every M5 invariant has runtime/state evidence;
- integrated Python/Rust scenarios match candidate membership, exclusions, ranking, fairness, and local claim ownership;
- restart hydration preserves effective durable state;
- concurrency cannot publish partial claims or consume multiple half-open probes;
- uncertain catalog/optional enrichment failure remains non-destructive;
- nonterminal suppression is bounded and recoverable;
- fairness/affinity/EWMA/recovery state is bounded;
- mandatory corrupt state fails closed without poisoning future valid operations;
- dependency/resource review finds no unjustified architecture expansion;
- no inference dispatch/retry/finalization or semantic selector call has leaked into M5.

## 15. Stop conditions

Do not close M5 for a high/medium correctness gap, a construction-only mandatory state transition, an unbounded learned-state map, a secret/raw-session leak, a claim/circuit ownership race, destructive catalog uncertainty, or an implementation that requires M6/M7 behavior merely to demonstrate its own stated M5 contract.
