# Runtime Efficiency Milestone 004 — Behavior-identical request-path cost removal

Status: implemented

Repository baseline: `563cf91792bc`

Source roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-004--behavior-identical-request-path-cost-removal`

Long-term requirements:

- `plans/000-long-term-specification.md#5-performance-posture`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/000-long-term-specification.md#4-protocol-and-compatibility`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#cross-phase-execution-rules`

Applicable ADRs:

- None required. This milestone preserves protocol, config, auth, generation,
  and storage ownership. Stop and write an ADR before changing any of those.

Primary class: polish

## 1. Objective

Remove four sources of repeated request-path work while keeping every
client-visible and durable result identical to the baseline:

- provider-model catalog lookup allocates a `(String, String)` key for every
  account visited during selection;
- publication and finalization compile the same SQL on every call;
- an attributed request reads `policy_state.revision` through the single
  SQLite gate even when the process cache is already current;
- admission parses a fully buffered body on the only Tokio runtime thread.

A package whose result cannot be shown identical to the baseline is declined.
It is not landed in a weaker form.

## 2. Why this milestone is ready

No hard dependencies. Runtime-efficiency M001–M003 are closed and stay closed.
Admission, catalog identity, publication, finalization, user budgets, instance
budgets, and model policy are implemented and covered by existing serial
suites. Persistence M003, M004, M007, M008, and M010 rejected storage-topology
changes; this milestone does not reopen them.

## 3. Current implementation evidence

Selection calls `ModelCatalogCache::get_provider_model` from
`rust/src/routing/eligibility.rs`. The cache in `rust/src/catalog/cache.rs`
stores `BTreeMap<(String, String), ProviderModelIdentity>` and looks up with
`model_id.into()` and `provider_id.into()`. The same allocating lookup is used
by the other provider-model getters in that file.

`rust/src/coordinator/publication.rs` and `rust/src/coordinator/finalization.rs`
run named `TransactionKind::{Publication, Finalization}` transactions on the
single gate in `rust/src/db/connection.rs`. Inside those transactions,
`rust/src/user_budget.rs` and `rust/src/instance_budget.rs` run further
statements with `execute` and `query_row`, which prepare a new statement per
call. The metrics flush in `rust/src/operations/metrics.rs` already prepares
its upsert once per flush transaction. No hot-path code calls `prepare_cached`.

`ModelPolicyService::snapshot` in `rust/src/model_policy.rs` always
`Database::call`s `SELECT revision FROM policy_state WHERE singleton = 1`
before it may return the cached `ModelPolicySnapshot`. The cache is bounded
to 256 users. SQLite triggers in `rust/assets/db/migrations/0058_model_policy.sql`,
`0059_user_budgets.sql`, `0062_instance_budgets.sql`,
`0063_default_member_profile.sql`, and `0065_tool_grants.sql` also increment
`policy_state.revision`. `architecture/deep-dive-on-prem-user-access.md`
records that a snapshot is immutable for the request that admitted it.

`rust/src/request/admission.rs` `parse_once` scans bytes for depth, decodes
JSON once, then walks the tree. `rust/src/main.rs` uses Tokio
`current_thread`. `rust/src/server/inference.rs` `finish_finite_execution`
awaits `FiniteExecution::complete` before `into_response`. Streaming returns
headers first and finalizes after the body. That difference is the fail-closed
finite contract: a finalization failure is a 503 and the client does not
receive the provider body.

## 4. Invariants that must not regress

- Chat Completions, Responses, Responses compact, Anthropic Messages, and
  embeddings keep their current status codes, error bodies, and success bodies
  for the same admitted input.
- The selected account, score order, exclusion reasons, fairness decision, and
  routing-decision row match the baseline.
- Publication still inserts the request, reservation, attempt, and
  routing-decision rows in one immediate transaction. Retry, conflict,
  principal-changed, budget-exceeded, and budget-not-enforceable outcomes
  stay the same.
- Finalization still settles the same request, attempt, reservation, and
  user/instance budget rows. A finite HTTP success is still returned only
  after that commit succeeds.
- Model allow/deny for a user matches a fresh SQLite evaluation after every
  revision bump, including trigger bumps. An admitted request keeps the
  snapshot it already holds.
- User and instance budget SQL still runs inside the publication and
  finalization transactions. An empty rule set is still queried there.
- Admission still rejects the same oversize, over-deep, and malformed bodies,
  with the same error classification. Each body is still parsed once.
- `PreparedUpstreamAttempt` is still fully owned before provider I/O.
- One SQLite connection, one gate, one worker, WAL, and `synchronous=NORMAL`
  remain the production topology. Tokio stays `current_thread`. The routing
  selection lock and the streaming mpsc handoff stay.
- No credential, prompt, raw body, cache key, or provider secret is added to
  logs, diagnostics, or plan evidence.
- Default and `--no-default-features` behavior stay on the current
  compatibility contract. No new crate and no Cargo feature change.

## 5. Scope

### In scope

- A borrowed provider-model lookup with the same hit, miss, and iteration
  order as the current `(model_id, provider_id)` map.
- `prepare_cached` (or one equivalent prepare-once cache on the worker
  connection) for statements already executed by publication, finalization,
  and the budget helpers those transactions call.
- Replacing the per-request policy revision `SELECT` with a process-local
  revision, only after the writer census in work package C passes.
- Moving the already-buffered admission parse off the runtime thread, only
  after the admission parity tests pass.

### Explicitly out of scope

- Returning a finite provider body before durable finalization commits.
- Skipping publication, finalization, user-budget, or instance-budget
  statements, including when an in-memory flag says no rules exist.
- Narrowing selection to `supporting_accounts`. That would drop exclusion
  rows the baseline records for accounts that do not serve the model.
- Batching health locks or changing lazy health expiry inside selection.
- Removing either admission depth walk.
- A second SQLite connection, a read replica, `PERSIST`, a checkpoint-policy
  change, or an event-driven checkpoint wake.
- Multi-thread Tokio, removal of the selection lock, or removal of the
  streaming bridge.
- Schema, index, config, CLI, HTTP, or public Rust API changes.
- A new benchmark harness, a performance threshold, or a claim that the
  Pi 5 / microSD publication tail is gone.

## 6. Required production changes

No storage migration. No operator setting. No protocol field.

Catalog lookup stops allocating a key on the hit path. The stored identity
type and the selected model fields stay the same.

The single worker connection caches the hot statements. SQL text and bound
parameters stay the same, including `datetime('now', ?)` modifiers. Cache
capacity is an explicit bounded constant on that connection, large enough for
the distinct publication and finalization statements, and is not grown per
request.

The policy fast path, if landed, is a process-local revision updated only
after a successful commit by a writer in this process. Startup hydrates it
from SQLite after migrations and before inference is served. Uncertainty
falls back to the existing `SELECT` and does not serve the user cache.

Admission parsing, if moved, runs only on bytes that have already passed the
body-size limit. It holds no SQLite permit and no selection lock across that
work. The runtime flavor stays `current_thread`.

## 7. Ordered work packages

### Work package A — Lock the baseline outcomes

Intent:

Add the parity tests while production code is unchanged, so later packages
fail on a behavioral difference instead of discovering one at closure.

Required changes:

- Catalog: for a fixed cache, borrowed lookup and the current owned-key
  lookup return the same entry or the same miss for present ids, absent ids,
  and ids that contain `/`.
- Publication and finalization: the existing
  `coordinator_publication` and `coordinator_finalization` suites remain the
  outcome oracle. Add one file-backed case that compares the request,
  attempt, reservation, routing-decision, and budget rows of a cached-statement
  execution with a direct `execute` of the same SQL if both paths exist
  during the change. Before package B, this case may call the current path
  twice and require equal rows.
- Policy: after each mutation class below, the next `snapshot` for an
  affected user equals a snapshot loaded from SQLite with an empty cache.
  Classes: profile rule, user override, instance denial, user disable, role
  change, auth-epoch change, a user-budget rule write, an instance-budget
  rule write, a tool-grant write, and a default-member-profile change.
- Admission: the same bytes produce the same admitted model, stream flag,
  token bounds, and error variant for a normal body, a malformed body, a
  body over the depth limit, and a body over the size limit.

Acceptance evidence:

The new tests pass on unmodified production code. A deliberately stale
policy fixture fails the policy comparison.

### Work package B — Borrowed catalog lookup

Intent:

Stop allocating two `String`s per account on the selection lookup.

Required changes:

- Replace the allocating `(String, String)` get in `rust/src/catalog/cache.rs`
  with a lookup that accepts `&str` keys. A nested
  `BTreeMap<String, BTreeMap<String, ProviderModelIdentity>>` is acceptable.
  A private key type is acceptable. Concatenating the two ids with a
  separator is not.
- Preserve `(model_id, provider_id)` order for any iteration the routing or
  refresh path observes.
- Update every allocating get of `provider_models` in that file. Do not
  change refresh persistence or the routing score formula.

Acceptance evidence:

Package A catalog tests pass. `routing_domain`, `routing_claims`, and
`catalog_refresh` pass. A test shows the lookup does not allocate a key
`String` on a hit; a counter on a test-only path or a reviewed code guard is
enough. Do not add a benchmark dependency.

### Work package C — Cache statements on the existing worker

Intent:

Compile each hot statement once per connection without changing the
transaction's statements, parameters, or results.

Required changes:

- Use the connection's statement cache for every statement executed inside
  `TransactionKind::Publication` and `TransactionKind::Finalization`,
  including the `user_budget` and `instance_budget` helpers called from
  those transactions.
- Set an explicit cache capacity constant no smaller than the number of
  distinct statements those paths prepare. A test lists that set and fails
  if a newly added hot statement would exceed the constant.
- Keep fault-injection stages in `rust/src/coordinator/publication.rs` firing
  at the same points.
- Do not open a connection, change WAL, or change `synchronous`.

Acceptance evidence:

`coordinator_publication`, `coordinator_finalization`, and
`database_compatibility` pass, including conflict, rollback, budget exceeded,
and budget not enforceable. Row-for-row comparison from package A matches.
`--no-default-features` still compiles and passes the same targets.

### Work package D — Policy revision without a per-request gate read

Intent:

Serve a cached policy snapshot without taking the SQLite gate when this
process already knows the committed revision.

Required changes:

- Census every `UPDATE policy_state SET revision` and every trigger that
  performs that update under `rust/assets/db/migrations/`. Map each one to
  the Rust function that commits the enclosing transaction. Include
  `rust/src/model_policy.rs`, `rust/src/identity.rs`, `rust/src/user_budget.rs`,
  `rust/src/instance_budget.rs`, and `rust/src/tool_policy.rs`.
- If any live writer can commit a revision bump without a Rust hook in this
  process, or if `serve` can observe a revision committed by another process,
  decline this package. Leave `ModelPolicyService::snapshot`'s `SELECT` in
  place. Name the writer in the closure record. Packages B, C, and E may
  still land.
- If the census is complete: after migrations and before inference, load the
  revision once. After each successful committing writer returns, store that
  committed revision in one process-local atomic. A failed commit stores
  nothing. `snapshot` uses the atomic on the cache-hit path. If the atomic
  is unset or the cache entry's revision differs, load from SQLite as today.
- Do not cache across a generation in a way that outlives a user disable.
  The next request after the disable must deny.

Acceptance evidence:

Package A policy tests pass against the fast path, not only against a forced
`SELECT`. One test disables a user and shows the following request receives
the baseline denial. One test proves an in-flight admitted snapshot is
unchanged by a later revision bump. The declined-path closure names the
blocking writer and shows the `SELECT` still precedes the cache hit.

### Work package E — Parse the buffered body off the runtime thread

Intent:

Keep `current_thread` responsive to other tasks while a large, already
buffered body is decoded.

Required changes:

- Move `parse_once` work onto Tokio's blocking pool and await it from the
  existing admission boundary in `rust/src/coordinator/endpoints.rs` and any
  other production caller of `parse_request_body` on the request path.
- Hold no database permit, selection lock, or catalog lock across that wait.
- Preserve one parse, both depth checks, and the current `AdmissionError`
  mapping.
- A cancelled handler may let an already-started parse of a buffered body
  finish. It must not parse bytes that failed the body-size limit, and it
  must not change the error the client would have received had the handler
  run to completion.
- Do not change `rust/src/main.rs` runtime flavor.

Acceptance evidence:

Package A admission tests pass. `canonical_request` and
`coordinator_boundaries` pass. A review note confirms the blocking parse
holds none of the locks listed above. Stop if the client-visible error
variant for any package A fixture changes.

## 8. Failure, cancellation, restart, contention semantics

Statement-cache lookup failure is a database error of the same class as a
prepare failure today. It rolls the surrounding transaction back through the
existing `Database` contract. It does not commit a partial publication.

A policy writer that fails before commit leaves the process-local revision
unchanged. Restart loads the revision from SQLite before serving. Two
overlapping writers publish only revisions they have committed; a snapshot
that observes a newer revision and a stale cache entry reloads from SQLite.

Cancelling a request during parse does not cancel bytes already buffered and
does not invent a new error variant. Cancelling during publication or
finalization keeps the current rollback and reconciliation behavior.

The selection lock is not held during parse, publication, or the policy
revision read. No package adds an `.await` under that lock.

## 9. Compatibility and migration

No schema change, no config key, no default change, and no client negotiation.
Existing databases, including those upgraded through the current migration
chain, behave as before. The statement cache is memory on the worker
connection and is empty after every process start.

If package D is declined, operators see the current policy-probe read. That
is compatibility, not a fallback mode to configure.

## 10. Required tests

Add the package A parity tests beside the owners they lock:

- catalog lookup parity in the `rust/src/catalog/cache.rs` tests or
  `rust/tests/catalog_refresh.rs`;
- publication/finalization row parity in `rust/tests/coordinator_publication.rs`
  and `rust/tests/coordinator_finalization.rs`;
- policy coherence in the `rust/src/model_policy.rs` tests, covering each
  mutation class in package A;
- admission parity in `rust/tests/canonical_request.rs` or the admission
  tests in `rust/src/request/admission.rs`.

Existing targets that must stay green:

- `routing_domain`
- `routing_claims`
- `catalog_refresh`
- `coordinator_publication`
- `coordinator_finalization`
- `coordinator_boundaries`
- `database_compatibility`
- `canonical_request`

Run Rust tests serially (`--test-threads=1`).

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
```

No `cargo deny` run is required unless a manifest changes. This plan forbids
that change. Do not claim a command in the closure record unless it was run.

## 12. Documentation updates

- If package D lands, update the model-policy paragraph in
  `architecture/deep-dive-on-prem-user-access.md` so it describes the
  in-process revision and the SQLite fallback. Keep the immutable-snapshot
  sentence.
- If package D is declined, leave that paragraph as it is.
- If package C lands, add one sentence to the publication section of
  `architecture/deep-dive-database.md`: hot statements are prepared through
  the worker connection's bounded statement cache; the gate, WAL, and
  `NORMAL` synchronous level are unchanged.
- Do not edit `plans/000-long-term-specification.md`,
  `plans/001-terminology-and-domain-model.md`,
  `plans/002-long-term-roadmap.md`, or `plans/003-planning-process.md`.
- Do not document a latency improvement, a tail reduction, or a capacity
  number. This milestone has no measurement campaign.

## 13. Acceptance criteria

- For the package A fixtures, selected route, exclusion set, publication rows,
  finalization rows, budget holds, policy decisions, admission errors, and
  HTTP statuses match the baseline.
- A finite request whose finalization fails still returns the baseline error
  and does not return the provider body as success.
- Production still has one SQLite connection and one gate. `rust/src/main.rs`
  still selects `current_thread`.
- No new dependency, config key, schema version, or public type change.
- Package D is either landed with the coherence tests or declined in the
  closure record with the unhooked writer named and the revision `SELECT`
  still on the cache-hit path.
- The closure record lists every command in section 11 that was actually run,
  separately for default and `--no-default-features`.

## 14. Stop conditions

Stop and report rather than improvise when:

- a borrowed lookup cannot preserve hit, miss, and iteration order without a
  new crate or a concatenated key;
- `prepare_cached` changes a rollback, busy, or fault-injection outcome;
- the policy census finds a writer that cannot publish the committed
  revision — decline package D, do not invent a time-based refresh;
- moving parse changes an admission error variant or requires holding the
  database gate;
- a package needs a schema, config, or HTTP change to stay equivalent;
- the change starts altering WAL, the worker count, the runtime flavor, the
  selection lock, or the streaming handoff.

## 15. Closure evidence required

- Implementation commits.
- Requirement-to-evidence matrix mapping each invariant in section 4 to a
  test or a code citation.
- Package D disposition: landed or declined, with the census result.
- Commands run and their results, labeled local, for default and
  `--no-default-features`.
- Confirmation that no manifest, migration, or public API file changed.
- Unresolved findings, or an explicit statement that none remain.
- Registry update moving M004 out of the ready table only when this closure
  record exists. This plan does not close itself.

## 16. Handoff notes

M001–M003 plans and closures are immutable. Do not reopen their code reviews
as part of this work.

The finite finalization wait in `rust/src/server/inference.rs` is intentional.
Speeding it up by responding earlier is a client-visible contract change and
is out of scope even if a measurement shows it is the largest latency.

The publication `COMMIT` on slow storage remains the measured tail from
persistence M004 and M007. Statement caching does not claim to move it.

Rust tests share process state in this repository and must run with
`--test-threads=1`. Preserve any user changes already in the worktree. Do not
commit unless the user asks.
