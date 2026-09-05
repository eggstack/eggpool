# C001 Closure — Coordinator Contract and Deterministic Failure Corpus

Status: closed

Recommendation: closed; C002 is dependency-ready.

Implementation commit: [`59eda5ab`](https://github.com/eggstack/eggpool/commit/59eda5ab)

Plan: [C001 — coordinator contract and deterministic failure corpus freeze](../../implementation/coordinator/001-contract-and-failure-corpus-freeze.md)

Contract: [M7 coordinator contract](../../coordinator-contract.md)

Repository baseline observed: `04820555479dc3ab86622d9c658c44c45c2c07e7`

## Outcome

C001 freezes the externally meaningful Python coordinator boundary before Rust
durable dispatch work begins. The contract is grounded in the current request,
claim, finalization, failure/effects, retry, stream, wire-resolution, and
repository modules. It records state transitions and ownership without using
the Python coordinator's implementation structure as a Rust design.

The deterministic corpus contains a machine-readable matrix, a committed
secret-safe scalar observation, and an executable observation adapter. The
adapter calls production Python failure/effects classification, stream
terminal classification, wire candidate learning/rejection and
leader/follower coordination, finalization identity/progress types, and the
monotonic response-handoff state. A local HTTP provider stub proves request
shape/response handling without retaining request or provider bodies.

## Requirement-to-evidence matrix

| C001 requirement | Evidence | Result |
|---|---|---|
| Every M7-owned transition has an observable contract | [`coordinator-contract.md`](../../coordinator-contract.md) defines admission, local claim, durable publication, wire selection, dispatch, response handoff, streaming, terminal command, durable terminal, runtime release, and completion | Pass |
| Durable rows/columns and local ownership are explicit | Contract and observation enumerate `requests`, `request_attempts`, `reservations`, `routing_decisions`, `FinalizationIdentity`, `RuntimePublicationReceipt`, cleanup progress, and compensation progress | Pass |
| Pre-handoff retry is distinct from post-handoff terminal behavior | Failure projection includes transport retry and `post_handoff_500` with retry disabled; `ResponseHandoffState` proves repeated marking is monotonic | Pass |
| Failure corpus covers required HTTP and transport phases | `c001-fixture-matrix.json` lists connect/proxy/TLS/write/header/read/pool, 400/401/403/404/408/409/429/5xx, provider signals, malformed envelopes, and retry-after variants; Python projection covers the policy-bearing cases | Pass |
| Wire negotiation is not conflated with retry | Resolver observation covers operator preference, learned preference, deterministic rejection, suppression, and leader/follower acceptance; effects observation distinguishes alternate-wire from other-account actions | Pass |
| Streaming terminal, EOF, cancellation, and midstream cases are represented | Stream observations cover OpenAI, Anthropic, Responses, Gemini completion/incomplete, empty/partial/malformed/compatibility EOF, terminal failure, and midstream exception vocabulary | Pass |
| Concurrency and cancellation barriers are represented | Matrix covers claim races, duplicate finalization, cleanup races, negotiation leader/follower cancellation, and every cancellation phase through finalization | Pass |
| Restart permutations are represented | Matrix covers pending requests, incomplete/terminal attempts, active reservations, replayed commands, and multi-attempt recovery snapshots | Pass |
| Determinism and schema compatibility | Observation generation is repeatable; latest Python schema is migrated in a temporary DB and checked against the durable projection | Pass |
| Secret safety | Committed projection and tests reject API-key, proxy-password, authorization, session, raw-body, and host-path markers | Pass |
| No production Rust capability or dependency was added | Changes are limited to migration contract/fixture/test artifacts; no Rust, Python production, Cargo, database migration, or live-provider path changed | Pass |

## Fixture inventory

| Artifact | Coverage |
|---|---|
| `migration-rs/fixtures/coordinator/c001-fixture-matrix.json` | Lifecycle, durable rows, transport/HTTP/provider signals, retry-after, wire negotiation, streams, cancellation barriers, concurrency, restart snapshots, parity classes, boundary, and forbidden markers |
| `migration-rs/fixtures/coordinator/c001-python-observations.json` | 23 failure/effect projections, 11 stream observations, retry-after parsing, wire learning/rejection and leader/follower results, ownership progress, and lifecycle vocabulary |
| `migration-rs/coordinator-contract.md` | State machine, durable/local ownership, retry/effects, wire negotiation, terminal evidence, cancellation, restart, parity, and security rules |
| `tests/migration_rs/coordinator_fixtures.py` | Production-bound observation adapter with injected clock values and bounded scalar projection |
| `tests/migration_rs/test_c001_coordinator.py` | 9 deterministic contract, schema, database-fault, local-stub, and secret-safety assertions |

## Verification commands actually run

```text
uv run ruff format --check tests/migration_rs/coordinator_fixtures.py tests/migration_rs/test_c001_coordinator.py  # 2 files already formatted
uv run ruff check tests/migration_rs/coordinator_fixtures.py tests/migration_rs/test_c001_coordinator.py  # passed
uv run pytest tests/migration_rs/test_c001_coordinator.py -q --tb=short --maxfail=1  # 9 passed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
git diff --check  # passed
```

No live provider, paid inference, Rust production code, Cargo dependency, or
database migration was required. The local provider test uses the existing
bounded `StubHttpServer`; its request body is measured and discarded by the
fixture server.

## Ownership, security, and resource review

- Durable publication remains a C002 responsibility; C001 only verifies that
  the current schema exposes the required identity columns.
- The committed observation uses synthetic IDs and fixed timestamps. Host
  paths, process/task IDs, random values, auth headers, proxy credentials,
  prompts, response bodies, raw provider error text, and session identities
  are absent.
- Wire resolver state is observed after leader/follower completion and the
  bounded snapshot reports zero in-flight flights. The resolver does no
  network or database work.
- The stream fixtures are small byte strings and retain only counters,
  terminal kinds, and EOF classifications. No complete stream buffer or
  per-event task is introduced.
- The schema check uses a temporary database and disconnects in `finally`.
  No persistent test database or production state is changed.

## Supported differences and unresolved findings

No mandatory stop condition remains and there are no unresolved high- or
medium-severity correctness or security findings. The observation preserves
current Python semantics even where they are intentionally conservative:
ambiguous 401 does not disable credentials, strong model absence is distinct
from wire mismatch, 429 ends negotiation discovery, and downstream handoff
disables transparent retry. Fixed clock values and scalar exception/body
wording are semantic fixture normalizations documented in the contract.

C001 does not claim Rust dispatch, provider capability, runtime-generation,
rehash, shutdown, or recurring-background parity. Those boundaries remain
with C002-C011 and M8 as planned.

## Registry transition and future-plan audit

C001 is removed from the dependency-ready section of
`migration-rs/registry.md` and recorded in the completed table with
implementation commit `59eda5ab` and this accepted closure record. C002 is
moved to the sole **ready for handoff** position in the registry, coordinator
handoff README, handoff sequence, C002 plan header, and M7 roadmap state.

C003 remains queued behind C002; C004-C011 remain queued behind their named
serial predecessors. No later coordinator plan can be safely unblocked by
C001 alone. M8 runtime-generation/background lifecycle remains blocked on
accepted C011 M7 closure and its separate planning review. M9-M12 retain the
long-term roadmap sequencing.

Unresolved mandatory findings: none.
