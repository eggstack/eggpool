# Q001 — Qualification Contract, Target Matrix, and Evidence Schema Freeze

Status: complete (2026-09-09)

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: `00dd27fa103e3c663968ecd95d9289c60fca0601` (accepted O010 / M9 closure).

Primary class: invariant/infrastructure

Hard dependency: accepted O010 / M9 closure.

## Objective

Freeze the complete M10 qualification contract before expensive platform, provider, visual, or SBC runs begin. Q001 must convert the accumulated M4-M9 closure claims into one versioned coverage manifest that says exactly what M10 must prove, where it is already covered, which environment owns the remaining evidence, and what counts as exact versus semantic parity.

Q001 adds no production capability.

## Required source audit

Inspect current repository evidence, not only historical plans. At minimum:

- canonical migration specification, terminology, ADRs, roadmap, and planning process;
- F002/F003/F004/F005/F006 closure and migration harness;
- T006, D009, W012, C011/C014, R013, and O010 closure records;
- `tests/migration_rs/`, Rust integration tests, Python smoke/integration/unit suites;
- current `README.md`, deployment/provider/dashboard documentation, and architecture deep dives;
- `.github/workflows/ci.yml` and any release/install scripts;
- `rust/Cargo.toml` platform-sensitive dependencies and `cfg` boundaries;
- Unix control/runtime/deployment code in O002/O003/O009;
- database migration/checksum inventory and backup archive contract;
- dashboard SSR/static/theme assets and Python/Rust route renderers.

Record any contradiction between current docs and actual platform/runtime behavior as a finding; do not silently choose the easier interpretation.

## Qualification manifest

Create a versioned machine-readable manifest under `migration-rs/fixtures/qualification/` or another existing migration-fixture namespace. Each row must include at least:

- stable cell id;
- subsystem/surface;
- observation class (`exact`, `semantic`, `characterization`, `manual-review`);
- Python oracle or authoritative document;
- Rust entry point;
- required environment class;
- existing test/evidence path if already covered;
- later owner Q-plan if not yet covered;
- normalization rule id, if any;
- mandatory/optional classification;
- secret/cost risk flags;
- closure status placeholder.

The manifest must not copy raw credentials/config or arbitrary response bodies.

## Mandatory surface inventory

At minimum enumerate:

### Configuration / filesystem / CLI

- config resolution/defaults/validation/env ownership;
- 63 O001 frozen command paths and exit/presentation classes;
- runtime paths/PID/socket lifecycle;
- config/provider/key mutation and live-apply/restart semantics;
- backup/recover/update/deploy/uninstall effects.

### HTTP/API

- health/readiness/models/dashboard/API routes;
- Chat Completions, Responses, Messages finite and streaming paths;
- client auth/body limits/error mapping;
- provider/profile cross-surface transformations;
- semantic model-router dispatch;
- pre-handoff retry/failover and post-handoff no-replay.

### Durable/runtime

- request/attempt/reservation/routing durability;
- quota/health/backoff/quarantine effects;
- finalization/reconciliation;
- generation reload/retirement/shutdown;
- background-task singleton behavior;
- DB migration/rollback/backup compatibility.

### Dashboard

- page routes and key empty/non-empty/error states;
- escaping/links/forms/theme/static asset behavior;
- representative visual review cells.

### Environment-specific

- non-root target builds/runs;
- rootful Linux deployment;
- live-provider smoke;
- ARM64 SBC characterization;
- sustained local resource/failure run.

## Exact versus semantic parity

Freeze explicit normalization rules. Permitted examples include:

- temporary root paths;
- ephemeral TCP ports;
- process ids;
- generation/request UUID-like identifiers where identity shape rather than exact value is the contract;
- timestamps converted to relative/rounded observations where already approved;
- latency/resource figures only as characterization, never normalized into parity.

Do **not** normalize away:

- HTTP status;
- SSE terminal category/order;
- CLI exit class;
- error/effect category;
- routing/account/model identity when semantically relevant;
- durable row state/counts;
- retry count/action;
- JSON field presence where the contract distinguishes unset/zero/null;
- HTML text/escaping or missing controls;
- backup/archive member semantics.

## Target support matrix

Freeze current intended targets from product/docs/code. The plan starts with these candidate classes, but repository evidence decides final status:

- Linux x86_64 — expected supported runtime/deployment target;
- Linux aarch64 — mandatory supported SBC/runtime target;
- macOS arm64 — qualify as supported development/non-root runtime only if current code/docs justify it;
- other Unix architectures — explicit supported/build-only/not-qualified classification;
- Windows — explicitly review Unix socket/process/deployment assumptions and mark supported/build-only/unsupported; do not infer support from Cargo alone.

For each target specify which cells are required: build only, non-root runtime, service deployment, backup/update, inference, etc.

## Environment metadata schema

Define a bounded evidence record containing:

- candidate SHA;
- Python source/version identity;
- OS/distribution/version;
- architecture;
- kernel;
- board/SoC/RAM/storage for SBC;
- Rust/Python versions;
- build profile/features;
- qualification manifest version;
- command/test id;
- pass/fail/skip/block;
- bounded reason/category;
- elapsed/resource scalar summaries;
- artifact/result hashes where useful.

Environment variables are allowlisted by name; never dump the full environment.

## CI policy freeze

Document which qualification commands are:

- normal CI smoke;
- manual/local deterministic;
- `workflow_dispatch` eligible;
- physical-host only;
- credentialed live-provider only.

Default decision: keep existing normal CI lean. Q001 must provide evidence before promoting any large qualification job into every push/PR.

## Required tests

Add tests that validate the qualification manifest itself:

- unique cell ids;
- valid owner plan/environment/observation enums;
- no mandatory cell without an owner/evidence path;
- normalization rule references resolve;
- no credential-bearing fixture fields;
- all O001 command paths represented;
- all three public inference surfaces represented finite + streaming;
- DB/dashboard/platform/live/SBC/soak categories present.

## Verification

Run at minimum:

```text
cargo test --manifest-path rust/Cargo.toml --all-targets --no-run
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
git diff --check
```

Add the focused manifest-validation command in closure.

## Non-goals

Q001 does not:

- run paid live-provider tests;
- run physical SBC characterization;
- change runtime behavior;
- add permanent broad CI;
- publish release binaries;
- decide M11 cutover;
- rewrite historical closure records.

## Closure evidence

Write `migration-rs/closure/qualification/001-status.md` with:

- manifest path/version/hash;
- complete target matrix;
- exact/semantic normalization table;
- mandatory cell counts by owner Q-plan;
- existing-coverage reuse counts;
- CI/manual/physical/live classification;
- unresolved contradictions/findings;
- verification commands/results;
- registry transition.

## Acceptance criteria

Q001 closes only when:

- every mandatory M10 cell has explicit ownership/evidence;
- target support is explicit rather than implied;
- normalization cannot hide semantic mismatches;
- expensive/live/rootful/SBC runs are clearly separated from normal CI;
- no secret-bearing evidence path is authorized;
- no unresolved architecture/product contradiction prevents later qualification.

Accepted Q001 promotes only Q002.

## Implementation closure

Q001 is closed by implementation candidate `d8885dc8fd5a319fe1152bb833f9723e5b35fbcf` and the accepted evidence record at `migration-rs/closure/qualification/001-status.md`. The frozen manifest and its six validation tests add no production capability, dependency, schema, CI job, or runtime behavior. Q002 is the only plan promoted by this closure.
