# D007 Closure — Model-Router Registry and Affinity State

Status: closed

Recommendation: closed; D008 is now dependency-ready. M6 implementation
handoff remains blocked on the integrated D008/M5 closure, and M7 remains
additionally blocked on M6 canonical request/codec work.

Implementation commit: [`43ce484`](https://github.com/eggstack/eggpool/commit/43ce484)

Plan: [D007 — model-router registry and affinity state](../../implementation/routing-domain/007-model-router-registry-and-affinity.md)

Contract and oracle: [M5 routing-domain contract](../../routing-domain-contract.md),
D001's [Python observations](../../fixtures/routing-domain/d001-python-observations.json),
and Python's `model_router.config`, `model_router.registry`, and
`model_router.affinity` modules/tests.

## Outcome

D007 adds the Rust `model_router` boundary. Validated configuration compiles
into an immutable registry with exact virtual-alias lookup, sorted stable route
IDs, normalized static selector policy bytes, and the Python-compatible
length-delimited SHA-256 semantic fingerprint. The empty registry shares one
process-wide immutable backing value.

`ModelRouterAffinity` is a process-local TTL/LRU cache keyed by virtual model,
router fingerprint, and a SHA-256 session digest. It stores only the derived
concrete route decision and expiry metadata. Explicit identities and bounded
automatic Chat/Messages identities are hashed before storage; Responses
automatic identity is disabled. Keyed single-flight uses Tokio watch channels,
is capped at the cache capacity, does not cancel a leader when a follower is
dropped, and releases coordination state on success, error, or leader
cancellation. Sticky-disabled routers call the supplied selector directly.

No M6 canonical request type, provider client, database row, background task,
or semantic selector inference path was added. `Config::compile_model_router_registry`
is the structural candidate-compilation boundary for a later generation
publisher.

## Requirement-to-evidence matrix

| D007 requirement | Evidence | Result |
|---|---|---|
| Immutable compiled structures | `rust/src/model_router.rs::CompiledModelRoute`, `CompiledModelRouter`, and `ModelRouterRegistry` use owned/Arc-backed published values; registry lookup has no mutation API. | Pass |
| Exact alias lookup and collision precedence | `ModelRouterRegistry::get`/`is_virtual` perform exact lookup; `model_router` test covers `gpt-4` versus `gpt-4/provider-a`. | Pass |
| Sorted routes and stable IDs | `compile_model_router` sorts labels from the `BTreeMap` and assigns decimal IDs; golden test covers source-order independence. | Pass |
| Static selector policy | Golden test asserts the exact D001 bytes `model-router/v1|choose id;reply id only|0=Default path|1=Fast path`. | Pass |
| Fingerprint parity and invalidation | `length_delimited_hash` frames UTF-8 fields with big-endian u64 lengths and includes all decision-sensitive fields; golden fixture matches `70c264...c0f8a`. | Pass |
| Aggregate policy bounds | Structural validation enforces UTF-8 byte limits, controls, TTL, timeout, input, repair, route, and policy-size bounds without catalog access. | Pass |
| Virtual-to-virtual rejection | Global validation rejects selector and route references to any configured virtual alias. | Pass |
| Shared feature-off registry | `OnceLock` supplies one empty `RegistryInner`; `from_config({})` returns that shared empty backing state. | Pass |
| Explicit session identity | Header values are bounded to 512 UTF-8 bytes, reject empty/control input, and enter affinity only as SHA-256 bytes. D001 digest golden is asserted. | Pass |
| Automatic identity DTO boundary | `ConversationPrefix` and `AffinityIdentityInput` avoid M6 coupling; system/developer fields and first-user text are bounded/framed, while Responses returns no automatic identity. | Pass |
| UTF-8 and entropy bounds | Boundary-safe head/tail truncation preserves the 4,096-byte prefix budget and 1,536-byte first-user reserve; tests cover large system input and non-ASCII content. | Pass |
| TTL/LRU cache | Lookup lazily expires entries, store performs bounded expiry cleanup, and insertion evicts the oldest entry at capacity. Tests cover hit, expiry, touch, and eviction. | Pass |
| Fingerprint-partitioned affinity | Cache keys include the compiled fingerprint, so changed policy creates a new key space; dedicated test resolves two fingerprints independently. | Pass |
| Obsolete-target validation | Hits revalidate route ID, label, model, and virtual alias against the current compiled route map before returning. | Pass |
| Sticky false bypass | `resolve` invokes the supplied selector without cache or flight coordination when `sticky` is false; test proves two calls. | Pass |
| Keyed single-flight | Equal concurrent misses share one leader and one stored decision; stats expose leaders, joins, entries, and inflight keys. | Pass |
| Error/cancellation recovery | Leader errors publish a typed result without caching; leader cancellation removes the key and wakes followers, which can become the next leader. Tests cover both error recovery and cancellation recovery. | Pass |
| Selected-model validation | `decision_from_selection` requires exact route ID/label/model/virtual identity and returns `InvalidSelection` without storing unknown results. | Pass |
| Diagnostics/privacy | Stats contain aggregate counts only; `SessionIdentity` debug output contains the digest, while cache state never stores raw header/prefix content. | Pass |
| Account/provider separation | Affinity decisions contain only route/model data; no account, provider, health, quota, or provider-client dependency exists in the module. | Pass |
| M5/M6/M7 boundary | No selector inference, `ProviderHttpClient`, canonical request, durable persistence, retry, or finalization code was added. | Pass |

## Differential and concurrency evidence

The Rust golden test matches the D001 route order, static policy bytes,
affinity digest, and config fingerprint. The Python D001 snapshot and adapter
remain unchanged and passed. Focused Rust tests exercise concurrent equal-key
misses, a cancelled leader with a recovering follower, selector error and
subsequent recovery, bounded TTL/LRU behavior, and sticky bypass.

No credentials, authorization values, raw session headers, conversation text,
provider/account identity, request body, or response object is retained by the
affinity cache or emitted by its aggregate diagnostics.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml
rtk cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1  # 12 passed
rtk cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1  # 8 passed
rtk cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1  # 6 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 53 passed, 3 skipped
rtk uv run pytest tests/unit/test_model_router_config.py tests/unit/test_model_router_registry.py tests/unit/test_model_router_affinity.py -q --tb=short --maxfail=1  # 38 passed
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk cargo tree --manifest-path rust/Cargo.toml -e features  # no new M5 stack/dependency
rtk rustc --version  # rustc 1.98.0
rtk git diff --check
```

The repository-wide `cargo test --all-targets -- --test-threads=1` command was
attempted and stopped after repeated no-output waits because it reaches the
pre-existing catalog-refresh fixture that does not terminate in this
workspace, as documented by the D004/D005/D006 closure records. All affected
predecessor suites and the new D007 suite passed independently. No live
provider or selector inference request is required for D007.

## Acceptance and stop-condition review

The implementation stays outside M6's canonical request model and M7's
selector/coordinator path. It does not call provider clients or pin account or
provider state. Session and prefix inputs are hashed before cache retention,
all cache and flight state is bounded, and cancellation/error paths remove
coordination state. The exact D001 policy/fingerprint golden case passes, and
structural configuration does not query catalog availability.

Unresolved mandatory findings by severity: none.

## Future-plan state

D007 is removed from the dependency-ready table and recorded as completed in
the registry, routing-domain roadmap, handoff sequence, implementation index,
and this closure record. D008 has all hard predecessors D001-D007 closed and
is promoted to the sole dependency-ready M5 implementation plan.

M6 planning may continue and its implementation handoff is still blocked on
accepted integrated D008 closure. M7 remains additionally blocked on M6
canonical request/codec work. No other future plan is safely unblocked by
D007 alone under the repository's serial handoff policy.
