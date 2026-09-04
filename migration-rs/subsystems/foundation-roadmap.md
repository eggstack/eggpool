# Migration Foundation Subsystem Roadmap

Status: active

Canonical source:

- `../000-long-term-specification.md`
- `../001-terminology-and-domain-model.md`
- `../002-long-term-roadmap.md#M0`

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

## 1. Purpose and ownership

The foundation subsystem creates the side-by-side Rust build and the compatibility machinery needed to port EggPool safely. It owns build layout, parity harness conventions, contract inventory, initial config/CLI translation, SQLite compatibility baseline, and the first Axum/SSR read-plane slice.

It does not own provider dispatch, routing, transcoding, retries, or cutover.

## 2. Invariants

- Python remains runnable and unchanged by default.
- Rust lives under `rust/`; migration planning/evidence lives under `migration-rs/`.
- tests invoke implementations by explicit path and cannot accidentally compare Python to itself.
- normalization rules are narrow and reviewable.
- existing database/schema and config files remain the source compatibility boundaries.
- dashboard work ports SSR/static behavior; it does not redesign the frontend.
- dependencies are introduced only for an immediate foundation need.

## 3. Current-state evidence

At planning baseline, EggPool is Python 3.11+ with FastAPI, Granian, HTTPX, aiosqlite, Pydantic, Click, optional orjson, and optional pproxy. It exposes a large Click command surface, a mature TOML/Pydantic config model, 54 numbered SQLite migrations, JSON/stats APIs, and server-rendered dashboard pages with static/theme resources.

The existing Python test suite remains authoritative regression coverage for the Python implementation. Migration tests must supplement it with cross-implementation cases rather than duplicate all unit tests mechanically in Rust.

## 4. Dependency graph

```text
F001 Rust scaffold
   |
   +----> F002 oracle harness
              |
              +----> F003 config + CLI
              +----> F004 SQLite baseline
              +----> F005 Axum + SSR shell
                         ^
                         |
                   F004 interface for DB-backed pages
```

F003 and F004 can proceed in parallel after F002 closes. F005 may begin against an agreed repository trait/test double but cannot claim DB-backed page parity until the needed F004 read interfaces exist.

## 5. Milestones

### F001 — Rust workspace and build scaffold

Class: infrastructure

Create the isolated Cargo package, minimal dependency policy, build/test commands, logging/error skeleton, and explicit executable-path invocation without changing Python packaging or install behavior.

Exit: `rust/target/.../eggpool` builds and runs a minimal version/help probe without replacing Python.

### F002 — Contract inventory and differential oracle harness

Class: invariant/infrastructure

Create the migration test runner, observation schema, normalization policy, deterministic fixtures, and inventory of compatibility surfaces.

Exit: at least one CLI, one config, one HTTP, one database, and one SSR/static observation can be collected from Python; Rust placeholders are wired so later cases can become true differential tests.

### F003 — Config and CLI compatibility foundation

Class: capability

Port config parsing/defaults/validation/path resolution and the complete CLI parser tree sufficiently to compare help/arguments/exit classes while underlying command implementations remain explicitly staged.

Exit: the supported config corpus and CLI parser contract pass differential gates.

### F004 — SQLite schema and repository compatibility baseline (closed)

Class: invariant/infrastructure

Open existing DBs, reuse migrations/checksums, establish serialized transactions, and port the first repositories needed by health/stats/SSR.

Exit: Python-created fixtures are readable by Rust and Rust fixture writes remain compatible with Python within the declared rollback boundary.

### F005 — Axum SSR shell and static asset parity baseline (dependency-ready)

Class: capability

Establish inbound server/auth/body-limit middleware, health/readiness, static assets, dashboard route skeleton, renderer utilities/escaping, and enough read-only endpoints/pages to verify the dashboard can be mirrored without redesign.

Exit: selected dashboard pages/static resources and health/readiness work from Rust and pass DOM/static/API differential checks.

## 6. Risks

The main foundation risks are over-normalizing behavioral differences, prematurely porting internal Python abstractions instead of contracts, duplicating static assets without drift control, and making the initial Rust crate hierarchy more complex than the product needs.

## 7. Deferred work

Provider HTTP/Eggress transport, catalog/routing/quota/health, canonical request/transcoding/SSE, coordinator/finalization, runtime rehash, full operational CLI, packaging/cutover, and performance characterization are deferred to later subsystem roadmaps.

## 8. Foundation exit condition

Foundation closes when the Rust implementation has a reproducible build plus trustworthy differential machinery and can independently satisfy the config/CLI, SQLite baseline, and initial HTTP/SSR compatibility slices without disturbing the Python runtime.
