# D008 Closure — Differential Qualification and M5 Closure

Status: closed

Implementation commit: [`477aade`](https://github.com/eggstack/eggpool/commit/477aade)

Plan: [D008 — differential qualification and M5 closure](../../implementation/routing-domain/008-differential-qualification-and-closure.md)

Contract and oracle: [M5 routing-domain contract](../../routing-domain-contract.md),
D001's [Python observations](../../fixtures/routing-domain/d001-python-observations.json)
and [fixture matrix](../../fixtures/routing-domain/d001-fixture-matrix.json), plus the
Python account/catalog/quota/health/routing/model-router modules named by D001-D007.

## Outcome

D008 qualifies the complete M5 state/policy layer as one Rust generation-style
fixture. `rust/tests/routing_domain_d008.rs` opens the schema-54 database,
hydrates account identity, catalog/provider metadata, usage windows, health
backoffs, model quarantine, routing, and compiled model-router state, then
exercises the same request-independent facts across those boundaries. The
suite includes strict-priority fallback, expiry, quota score-only versus
hard-cap behavior, authoritative health exclusion, catalog uncertainty and
withdrawal/reappearance, exact quarantine lifecycle, local claim rollback,
model-router affinity hit behavior, and multi-threaded claim/probe contention.

During qualification two M5 defects were fixed. Catalog observations are
validated before an authoritative update can withdraw support, and the global
catalog projection is pruned when the final provider/model reference is
withdrawn. Catalog repository reads cast legacy NUMERIC/TIMESTAMP values to
text before parsing, preserving schema-54 compatibility for both historical
and newly-written SQLite values. Routing now honors canonical-only quarantine
rows whose upstream model identity is NULL before concrete dispatch selection.
The focused catalog fixture was also corrected to assert the frozen
unresolved-protocol and HTTP header semantics; its disabled-endpoint teardown
now proves no request is required.

No D009 corrective plan is required. The discovered defects were bounded M5
correctness issues, now covered by regression tests and passing the complete
Rust target suite.

## Contract-to-evidence matrix

| M5 invariant | Evidence | Result |
|---|---|---|
| Secret-free account identity | `rust/tests/routing_domain_d008.rs` hydrates identities from schema 54; `rust/tests/routing_domain.rs` covers non-secret identity; `tests/migration_rs/test_d008_routing_domain.py` and D001 fixture redaction assertions reject keys, proxy passwords, authorization values, and raw content. | Pass |
| Catalog non-destructive uncertainty | `rust/tests/catalog_refresh.rs` covers failed refresh preservation; `rust/tests/routing_domain_d008.rs::catalog_uncertainty_and_restart_state_are_non_destructive` covers malformed input, partial/empty update, authoritative withdrawal, and reappearance. | Pass |
| Per-account freshness | `catalog_refresh` and `routing_domain` suites cover successful refresh state, disabled/static no-freshness, legacy timestamp fallback, and account-local outcomes. | Pass |
| Provider/model protocol, capability, and limit identity | `rust/tests/routing_domain.rs` covers provider-specific catalog rows, static precedence, capability overrides, and per-field limits; `catalog_refresh` covers live normalization and semantic persistence. | Pass |
| Request/token score formula and local hard-cap policy | `rust/tests/quota.rs` and D008's `local_quota_mode_only_changes_scoring_gate_not_authoritative_health` cover request/token pressure, score-only routing, hard-cap exclusion, and health independence. | Pass |
| Pending/reserved claim ownership | `rust/tests/quota.rs` and `rust/tests/routing_claims.rs` cover pending publication, conversion, exact rollback, underflow protection, and bounded ownership mirrors; D008 verifies generation-integrated rollback. | Pass |
| Learned-state caps | Quota EWMA tests, `FairnessRotor` 4,096-key tests, and `ModelRouterAffinity` TTL/LRU tests cover bounded learned maps; D008 uses the same shared state in a generation fixture. | Pass |
| 30-minute backoff cap and Retry-After policy | `rust/tests/health.rs` covers reason schedules, Retry-After precedence/downward jitter, wall-to-monotonic hydration, expiry, and the 1,800-second cap. | Pass |
| Auth/operator terminal behavior | `rust/tests/health.rs` covers terminal authentication and operator state transitions; corrupt terminal/enum input fails closed. | Pass |
| Read-only circuit checks and one half-open probe | `rust/tests/health.rs` covers non-consuming read checks; `rust/tests/routing_claims.rs` and D008's multithreaded test prove one probe, rollback, and reacquisition. | Pass |
| Exact-key quarantine lifecycle | `rust/tests/health.rs` covers scoped promotion, expiry, exact success, authoritative reappearance, terminal provenance, and bounds; D008 hydrates and clears exact durable keys. | Pass |
| Stable routing exclusions | `rust/tests/routing_domain.rs`, `routing_claims.rs`, and D008 assert disabled, cooldown/quarantine, quota, provider, and catalog gates with stable reason codes. | Pass |
| Strict priority tiers | `rust/tests/routing_domain.rs` and D008's healthy/fallback scenario prove lower priority is selected only after all higher-tier candidates are excluded. | Pass |
| Fairness band and cap behavior | `rust/tests/routing_claims.rs` covers near-tie rotation, read-only non-mutation, and the 4,096-key LRU bound; D006 routing tests cover tier/native boundaries. | Pass |
| Local claim atomicity | D008's `claims_and_half_open_probe_are_serialized_under_contention` runs eight concurrent selectors on Tokio's multi-thread runtime and verifies unique claims, exact active counts, one probe, and rollback recovery. | Pass |
| Bounded model-router fingerprint/affinity/single-flight | `rust/tests/model_router.rs` covers stable fingerprinting, hashed identity, TTL/LRU, sticky bypass, single-flight, errors, and leader cancellation; D008 composes a first selector decision with a cache hit and D001 Python oracle assertions. | Pass |
| Schema-54 restart hydration | D008 creates a migrated schema-54 copy, applies `schema54-routing-domain-seed.sql`, hydrates every M5 state family, and checks controlled wall/monotonic expiry. `rust/tests/database_compatibility.rs` covers historical Python DB upgrade, seed reopening, rollback, busy behavior, and Python SQLite readback. | Pass |
| Rust/Python durable compatibility | Rust health writes are read by Python SQLite in `rust/tests/health.rs`; the historical repository round-trip is read back by Python in `rust/tests/database_compatibility.rs`; migration Python tests assert the same schema/fixture semantics. | Pass |
| Failure isolation and recovery | Catalog account failures remain local; malformed catalog metadata is non-destructive; invalid backoff/score/database state fails closed; quota underflow, duplicate claim compensation, selector errors/cancellation, and affinity cancellation recovery are covered by focused suites. | Pass |
| Public/read-plane boundary | Existing M5 read/state APIs remain backed by hydrated domain objects; no new public redesign was needed. Inference endpoints remain explicit placeholders and are not claimed as M5 parity. | Pass |
| Dependency/resource posture | `Cargo.toml` and `cargo tree -e features` show the existing Hyper/Rustls/M4 and SQLite stack only: no Reqwest, ORM, actor/DI framework, scheduler, or second proxy implementation was added. | Pass |
| M6/M7 boundaries | No canonical request parser, codec/SSE implementation, semantic selector call, inference dispatch, retry/failover, durable attempt persistence, or finalization was added. | Pass |

Rows that rely on predecessor evidence are explicitly linked to executable
Rust suites rather than construction-only claims. The D008 integrated suite
is the cross-boundary evidence; D001 remains the behavioral oracle for the
Python-side semantic snapshot. Container, lock, monotonic-clock, and JSON
format differences are normalized only where the D001 parity classifications
permit it.

## Restart, corruption, and concurrency review

The generation fixture uses a fresh copy of the deterministic schema-54 seed,
hydrates durable wall-clock timestamps into controlled monotonic health state,
and verifies that expired nonterminal quarantine/backoff state is not eligible.
The catalog and repository regressions found during this review now preserve
support on malformed input, parse both legacy and current SQLite timestamp
affinities, and remove withdrawn global rows only after their final provider
reference is gone. Invalid model input and non-finite durable backoff input
fail before mutation. Existing transaction tests verify rollback without
partial rows.

The D008 contention test uses a four-worker Tokio runtime. Concurrent claims
receive unique ownership IDs and publish exact active/pending state. A
half-open circuit yields one probe claim; rollback releases it and a later
claim reacquires it. Existing routing and affinity tests cover fairness
mutation ordering, keyed single-flight, and cancellation cleanup. No SQLite
or M4 network operation is held under the local selection critical section.

## Resource and security observations

The D008 multithreaded suite ran four integrated scenarios in 0.63 seconds
under `/usr/bin/time -l` with approximately 94.5 MiB maximum resident set
size for the test process and Cargo/runtime overhead. This is a local
observation, not an SBC acceptance threshold. The tests exercise a small
schema-54 state size; no allocation, thread, timer, or per-candidate SQLite
explosion was observed. The design retains bounded 4,096-entry fairness and
affinity state and uses hydrated in-memory routing reads.

Debug/serialized observations are built from non-secret identity and reason
codes. Credential stores, proxy values, raw catalog authorization headers,
raw response bodies, session headers, and conversation content do not enter
claims, routing plans, affinity entries, errors, traces, or closure fixtures.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 102 passed, 12 suites
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=4  # 4 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 55 passed, 3 skipped
rtk uv run pytest tests/unit/test_accounts.py tests/unit/test_catalog.py tests/unit/test_catalog_withdrawal_policy.py tests/unit/test_catalog_service_ping.py tests/unit/test_catalog_service_limits.py tests/unit/test_catalog_resolvers.py tests/unit/test_quota.py tests/unit/test_health.py tests/unit/test_account_backoff_repository.py tests/unit/test_failure_effects_table.py tests/unit/test_routing.py tests/unit/test_routing_priority.py tests/unit/test_routing_provider.py tests/unit/test_routing_transcode_eligibility.py tests/unit/test_model_router_config.py tests/unit/test_model_router_registry.py tests/unit/test_model_router_affinity.py tests/unit/test_model_router_selector.py -q --tb=short --maxfail=1  # 485 passed
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk uv sync --frozen --extra ci
rtk cargo tree --manifest-path rust/Cargo.toml -e features  # reviewed; no M5 architecture expansion
rtk rustc --version  # rustc 1.98.0; Cargo declares rust-version 1.85
rtk git diff --check
```

The full Rust target command, previously characterized as hanging at the
catalog fixture, now completes successfully after the D008 fixes. No live
provider, paid catalog, cloud matrix, or load farm was needed.

## Acceptance and stop-condition review

All M5 invariants have executable state or runtime evidence. Integrated
Python/Rust fixtures agree on semantic identity, support, exclusions,
priority, score policy, local claim ownership, health suppression, quarantine
expiry, and bounded affinity behavior. Nonterminal suppression and learned
state are bounded and recoverable. Corrupt mandatory state fails closed
without poisoning a later valid operation. The dependency review found no
unjustified architecture expansion. M6/M7 behavior remains outside this
closure.

Unresolved mandatory findings by severity: none.

## Future-plan state

D008 is removed from the dependency-ready table and recorded as completed in
the registry, routing-domain roadmap, handoff sequence, implementation index,
and this closure record. The routing-domain roadmap is closed after D008
qualification.

M6 canonical request/codec/transcoding/SSE planning and implementation
handoff are explicitly unblocked. No M6 implementation plan is registered or
promoted by this closure; its own planning review must establish the next
dependency-ready handoff. M7 coordinator/retry/finalization remains blocked
on M6, including canonical request and codec work. M8 through M12 remain
sequenced behind their stated predecessors and are not unblocked by M5 alone.
