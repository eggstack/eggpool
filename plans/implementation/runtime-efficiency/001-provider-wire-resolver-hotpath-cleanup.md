# Runtime Efficiency Milestone 001 — Provider/Wire-Resolver Hot-Path Cleanup

Status: implemented

Repository baseline: `b9ba100d8f4165e338c76a11359381137590b1d2` (reviewed current head; implementation started on branch after this commit)

Source roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-001--providerwire-resolver-hot-path-ownership-and-cache-cleanup`

Long-term requirements:

- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/000-long-term-specification.md#5-performance-posture`
- `plans/002-long-term-roadmap.md#cross-phase-execution-rules`

Applicable ADRs:

- None required. This plan preserves coordinator/wire/provider ownership,
  runtime topology, public APIs, and protocol behavior.

Primary class: polish

## 1. Objective

Remove source-proven per-attempt ownership churn and avoidable resolver
allocation/linear cache-maintenance work from finite and streaming inference,
without changing provider selection, wire candidate ordering, learned
preferences, fingerprint identity, retry semantics, or any public API/capability
surface.

## 2. Why this milestone is ready

No hard dependency is open. The relevant ownership contracts were stabilized by
the completed native-runtime campaigns:

- request admission parses once and native forwarding can reuse ingress
  `Bytes`;
- `AttemptPreparation` already borrows request/provider data synchronously;
- `PreparedUpstreamAttempt` is the fully-owned pre-await boundary;
- `ProviderClientPool` already publishes immutable lookup topology;
- provider profiles are generation-compiled by
  `compile_provider_profiles`;
- the current `WireResolver` is the sole process-owned negotiation/learning
  authority.

Persistence M007 is independent and remains blocked only on physical target
evidence. This milestone neither depends on nor modifies it.

## 3. Current implementation evidence

At baseline:

- `rust/src/coordinator/finite.rs` obtains the selected provider with
  `self.providers.get(&provider_id).cloned()` for every attempt and clones the
  provider profile vector before constructing resolver candidates.
- `rust/src/coordinator/streaming/coordinator.rs` repeats the same provider and
  profile ownership pattern.
- `rust/src/coordinator/attempt.rs::AttemptPreparation` already accepts
  `&ProviderConfig` and `&ConfiguredWireProfile`, so the full provider clone
  is not required by synchronous request preparation.
- `rust/src/coordinator/wire_resolver.rs::resolve` constructs a formatted
  candidate-structure `Vec<String>`, joins it into another String, formats
  preference state into the hash input, and creates owned
  `(provider_id.to_owned(), model_id.to_owned())` tuples for preference/hint
  lookups.
- `WireResolver::resolve` then creates/clones a `CacheKey`; ordinary
  `touch_lru` uses `VecDeque::retain`, making a cache touch O(current cache
  length) before pushing the key to the back. Default cache capacity is 2048.
- `WireCandidate` and `WireResolution` are public compatibility types and
  existing C011 tests exercise fixed preferences, learned success, deterministic
  rejection, TTL behavior, and leader/follower negotiation.

The existing physical SBC evidence does not justify changing Tokio runtime
flavor, the stream handoff, or routing selection lock; those boundaries are
outside this plan.

## 4. Invariants that must not regress

- Exact candidate order for every equivalent resolver state.
- Exact learned/fixed/metadata preference precedence.
- Exact deterministic rejection suppression and TTL expiry.
- Exact negotiation singleflight/permit/cancellation behavior.
- `WireResolution::fingerprint` remains byte-for-byte identical for the same
  candidate structure and preference state unless repository evidence proves it
  is strictly private and a migration of all live resolver state is impossible
  to observe. Default assumption: preserve it exactly.
- Resolver state remains bounded by configured capacities.
- Repeated hits must not create an unbounded stale-LRU queue.
- No provider/account credential data enters resolver keys, fingerprints, logs,
  or diagnostics.
- `AttemptPreparation` borrows only synchronously; no borrowed provider/profile
  reference crosses `submit_once().await`.
- `PreparedUpstreamAttempt` remains owned.
- Public `AttemptInput`, `PreparedUpstreamAttempt`, `WireCandidate`,
  `WireResolution`, and existing coordinator constructor surfaces remain
  source-compatible unless a crate-private-only helper can be changed without
  public impact.
- No retry/failure/health/quota/finalization semantics change.

## 5. Scope

### In scope

- Borrow selected `ProviderConfig` through finite/streaming synchronous
  preparation instead of deep-cloning it per attempt.
- Avoid unnecessary deep cloning/reconstruction of generation-static provider
  profile data on the common attempt path.
- Add crate-private borrowed/precompiled candidate helpers when useful while
  preserving public resolver/candidate APIs.
- Remove throwaway provider/model String tuple allocations from resolver
  preference/hint lookup through an internal representation that supports
  borrowed lookup.
- Compute the existing fingerprint without materializing the intermediate
  formatted vector/join String when equivalence can be proven.
- Replace linear full-LRU scanning on every touch with a strictly bounded
  internal cache-order representation whose ordinary touch is O(1) amortized
  or O(log N).
- Focused semantic-parity tests and structural performance evidence.
- Architecture documentation updates describing the retained ownership
  boundary.

### Explicitly out of scope

- Routing `selection_lock`, claim-book ownership, or `active_snapshot`.
- Tokio runtime flavor/worker count.
- Streaming mpsc/task handoff.
- SQLite/checkpoint/dashboard persistence topology.
- Wire negotiation policy, TTL values, cache capacity defaults, retry budgets,
  or provider capability changes.
- New dependencies solely to obtain an LRU implementation.
- Public API cleanup unrelated to the optimization.

## 6. Required production changes

### Coordinator/provider ownership

In finite and streaming coordinators, keep the selected provider as a borrow
from the immutable generation-owned provider map for all synchronous work. Only
materialize owned identity/config scalars that must survive into the owned
attempt or later async response processing.

Do not solve borrow-checker pressure by moving the provider map behind a new
mutex or by extending borrowed references across provider I/O. If response
decoding needs a small subset after the send, prefer a compact owned
dispatch/context fact already required by `PreparedUpstreamAttempt` or
`WireRuntimeContext` rather than cloning the entire `ProviderConfig`.

### Provider profile/candidate ownership

Provider profiles are generation-static. Add an internal path that avoids
deep-cloning the complete profile vector merely to feed
`prepare_candidates`/resolver selection. The implementation may:

- borrow static profiles while constructing a bounded owned candidate result;
- precompile static candidate facts per provider; or
- use another internal representation with equivalent semantics.

Public `WireCandidate` behavior must remain available for tests/callers.
Compact-operation filtering must still be able to remove unsupported profiles
without mutating generation-static state.

### Resolver preference lookup

Replace repeated temporary `(String, String)` lookup keys with an internal
borrow-friendly representation, for example nested provider/model maps or
another standard-library structure. Preserve operator > configured > metadata
precedence, state trimming, and public getter/setter behavior.

### Fingerprint construction

Retain SHA-256 and the exact current logical input. Feed equivalent bytes
directly to the hasher instead of constructing a vector of formatted strings
and joining them. Add a test-only/reference implementation representing the
pre-change formatter and prove equality across candidate/preference matrices.

Do not silently change delimiters, debug formatting, surface spelling,
candidate order, or preference serialization.

### Bounded LRU

Replace `VecDeque::retain` per touch with a bounded representation that does
not scan every cached key on an ordinary hit. Acceptable designs must:

- update recency deterministically;
- evict the same logical least-recently-used entry;
- keep storage O(configured capacity);
- not accumulate stale touch records without a strict compaction bound;
- preserve capacity shrink/trim semantics.

Use standard-library structures already in the dependency graph unless current
repository dependencies already provide an appropriate primitive.

## 7. Ordered work packages

### Work package A — Lock semantic reference tests

Intent:

Make optimization equivalence executable before changing internals.

Required changes:

- extend resolver tests around candidate ordering/fingerprint generation;
- add a pre-change/reference fingerprint helper under tests or a fixed corpus
  of known inputs/outputs;
- cover no preference, metadata hint, configured preference, fixed operator
  preference, learned preference, rejection, TTL expiry, capacity eviction,
  repeated-hit recency, and capacity trim;
- cover one/multiple profiles including compact-capable Responses profiles.

Acceptance evidence:

- current baseline behavior is represented without relying on timing sleeps.

### Work package B — Remove per-attempt provider/profile deep ownership

Intent:

Keep generation-static data generation-owned.

Required changes:

- refactor finite and streaming attempt loops to borrow provider configuration;
- introduce only the minimal owned facts needed after provider send;
- remove unnecessary provider-profile vector deep clone/reconstruction;
- keep compact filtering local and non-mutating.

Acceptance evidence:

- finite and streaming request/response behavior remains identical across
  native, translated, alternate-wire retry, compact, and provider-error paths.

### Work package C — Resolver transient allocation cleanup

Intent:

Remove allocation that contributes no retained resolver state.

Required changes:

- borrowed preference/hint lookup;
- allocation-light equivalent fingerprint hashing;
- avoid redundant key/profile/fingerprint clones where ownership can move.

Acceptance evidence:

- exact fingerprint parity corpus passes;
- no new unsafe code or lifetime crossing of async boundaries.

### Work package D — Bounded non-linear LRU touch

Intent:

Remove O(cache-size) work from every resolver touch.

Required changes:

- replace full `VecDeque::retain` touch;
- maintain strict capacity bounds;
- preserve logical LRU eviction under repeated access;
- add a deterministic large-capacity test proving storage remains bounded and
  the touched hot key survives the expected eviction sequence.

Acceptance evidence:

- resolver semantics and cache capacity tests pass at capacities 1, small
  multi-entry values, and a representative large value.

### Work package E — Qualification and docs

Intent:

Close without architectural overclaim.

Required changes:

- run focused and broad suites;
- record structural before/after facts: eliminated ProviderConfig/profile
  clones, removed transient resolver formatting/key allocations, and changed
  LRU complexity;
- optionally run the existing deterministic loopback fixture for descriptive
  latency/CPU evidence, but do not invent an SLA;
- update coordinator/wire runtime architecture documentation where ownership
  wording changes.

## 8. Failure, cancellation, restart, contention semantics

Provider send failure/cancellation must observe the same fully-owned attempt and
same resolver accept/reject behavior as before.

Resolver mutex ownership remains process-local and synchronous. This milestone
may shorten work under/around it but must not split policy state across a second
authority.

Reload publishes a new immutable generation while the process-owned resolver
retains existing staged/configured preference semantics. Borrowed provider/profile
data must remain tied to the request's generation lease; never reach into a new
generation after an await.

Shutdown/retirement must not gain any new task or resource owner.

## 9. Compatibility and migration

No database migration, config change, HTTP change, CLI change, provider
protocol change, or persistent-state migration.

Public Rust compatibility surfaces remain available. Crate-private
implementation types may change.

Fingerprint output is treated as compatibility-sensitive internal identity and
must remain exact.

## 10. Required tests

Focused:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
```

Add unit/integration coverage for:

- exact old/new fingerprint equivalence;
- preference lookup precedence;
- learned/rejected TTL behavior;
- repeated LRU hit + eviction order;
- strict LRU storage bound;
- finite/streaming provider borrow behavior through retries;
- compact filtering parity.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
```

If Cargo state unexpectedly changes, also run the repository dependency/security
policy gates and explain why a dependency change was necessary. Expected result
is no Cargo change.

## 12. Documentation updates

Update the narrow current-authority descriptions in:

- `architecture/deep-dive-request-lifecycle.md`
- the wire/runtime or provider deep dive that documents resolver ownership, if
  current wording becomes inaccurate;
- `plans/subsystems/runtime-efficiency-roadmap.md` only for status/closure
  reconciliation after implementation.

Do not rewrite completed legacy performance plans.

## 13. Acceptance criteria

- No full `ProviderConfig` deep clone solely for ordinary finite/streaming
  synchronous attempt preparation.
- No avoidable full provider-profile vector deep clone solely to enter resolver
  selection on the common path.
- Resolver preference/hint reads allocate no temporary provider/model key
  Strings.
- Fingerprints are exactly identical to the locked reference corpus.
- Ordinary LRU touch is no longer O(cache length), and cache-order storage is
  strictly bounded.
- Candidate order, accept/reject/learning, retry, health, quota, finalization,
  and compact behavior remain unchanged.
- Default/no-default full qualification is green.
- No public API/capability regression and no new dependency.

## 14. Stop conditions

Stop and report rather than improvise if:

- removing the provider clone requires a reference to survive provider
  `.await`;
- exact fingerprint parity cannot be retained without changing live resolver
  semantics;
- bounded LRU replacement requires an unbounded stale queue;
- public candidate/resolution APIs must be broken;
- optimization requires new synchronization authority or a dependency;
- tests expose a current semantic ambiguity rather than an implementation-only
  inefficiency.

## 15. Closure evidence required

The closure record must contain:

- implementation commit(s);
- before/after ownership/complexity table;
- exact fingerprint parity evidence;
- LRU capacity/eviction evidence;
- finite/streaming/compact/retry regression results;
- default + no-default serial workspace results;
- strict Clippy/fmt + locked release build;
- Cargo/dependency delta (expected zero);
- limitations and any deferred active-request/selection optimization;
- explicit statement that persistence M007 and runtime-threading/stream/routing
  boundaries were not changed.

## 16. Handoff notes

Keep borrowing local and synchronous. The desired boundary remains:

```text
immutable generation state
    -> borrowed synchronous attempt/resolver preparation
    -> fully owned attempt/context
    -> provider await
```

Do not optimize by weakening ownership clarity or failure semantics.
