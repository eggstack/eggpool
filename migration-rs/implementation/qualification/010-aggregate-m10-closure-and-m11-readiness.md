# Q010 — Aggregate M10 Closure and M11 Readiness Report

Status: queued behind Q009

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q009.

Primary class: invariant/polish

Hard dependency: accepted Q009 and accepted Q001-Q008 closure records.

## Objective

Perform the final migration-wide qualification review, rerun mandatory deterministic gates, aggregate environment-specific evidence, resolve remaining findings, and decide whether the Rust candidate is ready for a separate M11 public-cutover planning review.

Q010 closes M10 only. It must not flip release/install/update authority, publish canonical binaries, or remove Python.

## Inputs

Review all accepted M10 evidence:

- Q001 manifest/target/normalization contract;
- Q002 aggregate deterministic differential report;
- Q003 DB upgrade/rollback/backup/recovery matrix;
- Q004 dashboard DOM/static/visual review;
- Q005 supported-target build/non-root runtime evidence;
- Q006 disposable rootful Linux acceptance;
- Q007 bounded live-provider smoke;
- Q008 ARM64 SBC functional/resource characterization;
- Q009 sustained failure/reload/stream/resource-stability report;
- current M4-M9 closure records and any corrective plans created during M10.

Use current main, not the original planning baseline, for all final runs.

## Findings ledger

Maintain a bounded findings table with:

- id;
- severity (`high`, `medium`, `low`, `informational`);
- affected manifest cells/target/provider;
- symptom and invariant violated;
- owning source subsystem;
- fixing commit/plan or explicit accepted difference;
- failing-before/passing-after regression;
- closure status.

High/medium findings block Q010. Low findings may remain only when they do not contradict canonical migration requirements and have a documented post-cutover/non-blocking disposition.

## Mandatory final reruns

At minimum rerun on current main:

### Deterministic core

- Cargo format and Clippy `-D warnings`;
- full Rust all-target suite serially where runner supports it;
- full migration oracle;
- Q002 aggregate deterministic runner;
- Python smoke/project gates applicable to touched code.

### Data

- Q003 headline Python->Rust->backup/restore->Python transition;
- current migration/checksum compatibility probe.

### Dashboard

- Q004 DOM/static inventory comparison;
- verify screenshot/manual-review evidence still refers to current rendering hash/commit; rerun screenshots if renderer/assets changed after Q004.

### Platform/environment

Do not unnecessarily rerun expensive environment acceptance if no relevant code changed, but prove evidence freshness by diff ownership:

- if platform/process/deploy code changed after Q005/Q006, rerun affected target/rootful acceptance;
- if provider/wire/codec/routing changed after Q007, rerun affected live cells;
- if runtime/resource-sensitive code changed after Q008/Q009, rerun affected SBC/stability characterization.

Record this freshness decision explicitly.

## Compatibility delta report

Produce a concise final comparison of Python and Rust:

- exact parity surfaces;
- semantic parity surfaces and approved normalization;
- supported targets and qualification level;
- explicitly unsupported/not-qualified targets;
- known intentional differences;
- DB rollback boundary;
- dashboard parity result;
- provider/live qualification result;
- operational Linux result;
- ARM64 SBC characterization summary;
- resource/stability summary;
- dependencies added during migration and their purpose;
- remaining Python-only responsibilities that M11/M12 will remove or replace.

An intentional difference that changes external behavior must already be allowed by canonical planning/ADR or it blocks closure pending a decision.

## M11 readiness checklist

Q010 should answer, without implementing, whether M11 can safely plan:

- supported release target matrix;
- binary artifact naming/build strategy;
- integrity/signing policy around O008 update descriptor;
- public installer switch;
- service/upgrade rollback between final Python and Rust;
- release documentation/README changes;
- canonical Rust updater authority;
- preservation of config/DB/runtime paths;
- fallback/rollback procedure during initial Rust releases.

Record prerequisites M11 still owns; do not pull them into Q010.

## CI recommendation

Based on Q001-Q009 duration/value, recommend the post-M10 CI posture:

- which fast Rust/migration tests should join normal CI before M11;
- which remain manual `workflow_dispatch`;
- which remain physical/live/rootful release qualification.

Keep the recommendation minimal. Do not add jobs simply because M10 used them once.

## Documentation consistency audit

Before closure, search README/docs/architecture for claims that conflict with qualified reality, especially:

- supported platforms;
- Python vs Rust installation authority;
- update mechanism;
- systemd/cron behavior;
- provider/transcoding surfaces;
- backup/recovery;
- dashboard behavior;
- SBC/lightweight claims.

M10 may correct factual qualification/support documentation, but must not perform M11's Rust-default quick-start/cutover edits.

## Required verification

Record exact current-main commands/results. Minimum:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# Q002 aggregate deterministic runner
# Q003 headline rollback/backup transition
# Q004 DOM/static qualification
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

Also cite accepted Q005-Q009 environment commands/evidence and any freshness reruns.

## Non-goals

Q010 does not:

- publish Rust release assets;
- change public installer/README quick-start to Rust;
- remove Python package/runtime;
- create an M11 implementation plan automatically;
- invent new performance SLAs;
- waive missing physical/live/rootful evidence merely because deterministic tests are green.

## Closure record

Write `migration-rs/closure/qualification/010-status.md` with:

- current main implementation SHA;
- Q001 manifest completion counts;
- aggregate deterministic results;
- DB/dashboard/platform/rootful/live/SBC/stability evidence summaries;
- supported-target matrix;
- resource characterization summary;
- complete high/medium findings disposition;
- intentional compatibility-difference table;
- CI recommendation;
- M11 readiness checklist/prerequisites;
- dependency/schema/security review;
- registry/roadmap transition.

## Acceptance criteria

Q010 closes M10 only when:

- every mandatory Q001 cell is passed or has an explicitly authorized non-applicable classification;
- no unresolved high/medium correctness, security, data-loss, compatibility, lifecycle, resource, portability, dashboard, or provider finding remains;
- deterministic migration-wide parity is green on current main;
- DB rollback/backup/recovery evidence is accepted;
- dashboard visual/DOM/static review is accepted;
- supported-target and rootful Linux evidence is accepted;
- mandatory live-provider cells are accepted;
- a real Linux ARM64 SBC has accepted functional/resource evidence;
- sustained stability qualification is accepted;
- documentation does not claim support beyond evidence;
- M11 is made eligible for a separate planning review and no M11 implementation work is auto-promoted.

If any gate fails, create a new corrective Q011+ plan rather than rewriting prior Q-closure history.