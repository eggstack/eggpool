# Q002 Closure — Migration-Wide Deterministic Differential Qualification

Status: accepted; closed 2026-09-09

Implementation candidate: `de50b7a27aa7829a41baa5c6a8ff5e08248dd401`

Plan: [Q002 — migration-wide deterministic differential qualification runner](../../implementation/qualification/002-migration-wide-differential-qualification-runner.md)

## Outcome

Q002 implemented the bounded aggregate runner at
[`scripts/qualification_runner.py`](../../../scripts/qualification_runner.py),
with focused contract tests in
[`test_q002_runner.py`](../../../tests/migration_rs/test_q002_runner.py) and
real two-sided scenarios in
[`test_q002_cross_boundary.py`](../../../tests/migration_rs/test_q002_cross_boundary.py).
The runner validates the frozen Q001 manifest, fails closed on ownership drift,
keeps pass/fail/skip/block/infrastructure-error distinct, records exact argv and
implementation identities, and omits command output and credentials from its
artifacts.

The canonical aggregate artifacts are [`002-run.json`](002-run.json) and
[`002-run.md`](002-run.md). The JSON artifact is 13,169 bytes and its SHA-256 is
`01f2251026f5b63b25d9bce280fda793e2dc4a567c9ef97e7da2700f9f325165`.
The Markdown artifact is 1,883 bytes and its SHA-256 is
`ea92a3260af0235bbd72f32c3c6b681e1ad9a0d5bdbec2548a42bb3d566f95a5`.

## Q001 coverage

The accepted Q001 manifest is version `m10-q001.v1`, with 101 mandatory cells.
Q002 owns all 18 deterministic-local cells assigned to it. The aggregate
covered all 18 exactly once:

| Result | Count |
|---|---:|
| pass | 18 |
| fail | 0 |
| skip | 0 |
| block | 0 |
| infrastructure-error | 0 |

The six aggregate commands ran against the distinct Python launcher
(`sys.executable`) and Rust launcher (`rust/target/debug/eggpool`). The report
candidate SHA is the implementation baseline above; the manifest remains the
Q001 frozen artifact.

## Cross-boundary scenarios

The new tests cover:

- isolated config, environment, database, runtime paths, startup/readiness,
  deterministic static catalog, finite and streaming inference, runtime stats,
  live rehash, graceful shutdown, and schema/durable-state comparison;
- two-provider finite failover after a valid upstream 503, with two upstream
  attempts, and streaming success with exactly one attempt after downstream
  handoff;
- pending durable request insertion, restart reconciliation without replay,
  released terminal ownership, a live-reloadable mutation, and a
  restart-required port mutation;
- the existing focused provider, wire, coordinator, runtime, and operations
  evidence suites composed by the runner.

Backup archive allowlist coverage is composed from O001. Fresh backup/restore
compatibility remains the explicit Q003 owner and is not claimed by this
closure.

## Normalization and findings

The aggregate exercised only the Q001-approved rules: `none`,
`isolated_path_root`, `uuid_identity`, and `json_object_order`. HTTP status,
error categories, SSE terminal behavior, retry counts, durable state, and
ordering were not normalized away. The runner emits normalization rule,
Python/Rust observation slots, first differing semantic field, and owning
subsystem fields for every cell, including empty mismatch slots on passing rows.

One implementation defect found during the boundary run was corrected: Rust
mapped every publication error to HTTP 409 “Duplicate request identity”. The
endpoint now maps only an actual `DuplicateConflict` to 409 and maps other
publication failures to the existing 503 attempt category. The Rust coordinator
and the complete Q002 aggregate were rerun after that correction. No unresolved
high- or medium-severity deterministic compatibility finding remains.

## Verification evidence

The aggregate command was:

```text
rtk uv run python scripts/qualification_runner.py --skip-build
```

It completed in 114,605 ms with 18 passes and no other result class. The
individual evidence commands and observed results were:

```text
rtk uv run pytest tests/migration_rs/test_q001_manifest.py -q --tb=short --maxfail=1
# 6 passed
rtk uv run pytest tests/migration_rs/test_f003_config_cli.py tests/migration_rs/test_o001_operations.py tests/migration_rs/test_r001_runtime_lifecycle.py -q --tb=short --maxfail=1
# 23 passed
rtk uv run pytest tests/migration_rs/test_f005_server.py tests/migration_rs/test_f006_safety.py tests/migration_rs/test_q002_cross_boundary.py -q --tb=short --maxfail=1
# 15 passed
rtk uv run pytest tests/migration_rs/test_t001_provider_transport.py tests/migration_rs/test_w012_canonical_wire.py -q --tb=short --maxfail=1
# 17 passed, 3 skipped
rtk cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 --test coordinator_c013 --test canonical_request --test model_router -- --test-threads=1
# 35 passed
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r003 --test runtime_lifecycle_r004 --test runtime_lifecycle_r006 --test runtime_lifecycle_r008 --test runtime_lifecycle_r009 -- --test-threads=1
# 28 passed
rtk uv run pytest tests/migration_rs/test_q002_runner.py tests/migration_rs/test_q002_cross_boundary.py -q --tb=short
# 9 passed
```

The plan-level repository gates also passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
# pass
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
# No issues found
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
# 440 passed (52 suites, 205.44s)
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# 117 passed, 3 skipped
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
# 14 passed
rtk uv run pyright src/ scripts/
# 0 errors, 0 warnings, 0 informations
rtk uv run ruff format --check src/ tests/ scripts/
# 737 files already formatted
rtk uv run ruff check src/ tests/ scripts/
# all checks passed
rtk git diff --check
# pass
```

The complete runner is deliberate/manual qualification evidence, not a normal
CI gate. Normal CI remains the existing smoke gate. Q002 does not run live
providers, rootful deployment, physical SBC tests, screenshots, or long-duration
soak; those remain Q006–Q009 scope.

## Registry transition and future-plan audit

Q002 moves from ready to complete in the implementation plan, registry,
qualification README, and qualification roadmap. Q003 is promoted to **ready**
as the sole dependency-ready M10 plan because its direct dependency is now
accepted. Q004–Q010 remain queued behind their direct predecessors. M10 remains
active, M11 remains blocked on accepted Q010 and its separate planning review,
and M12 remains sequenced behind M11.
