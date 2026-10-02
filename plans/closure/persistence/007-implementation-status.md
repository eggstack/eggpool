# Persistence Milestone 007 — Implementation and Disposition

Status: blocked

Source implementation plan:

- `plans/implementation/persistence/007-dedicated-checkpointer-qualification-experiment.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-007--dedicated-checkpointer-qualification-experiment`

Repository baseline reviewed: `5d452b8d`

Implementation commit:

- `6f0cfd532e753a43c454b370453850c55679aab5` — implement the non-default
  dedicated-checkpointer experiment, local qualification runner mode, tests,
  and authority documentation.

Historical disposition: `plans/closure/persistence/007-status.md` records the
pre-reactivation blocker assessment at baseline `7e241ad`. The source plan was
subsequently revised to permit implementation and local qualification while
retaining physical Pi/MMC evidence as its operational closure gate. This
record covers that revised scope; the earlier assessment remains unchanged.

## 1. Executive finding

The feature-isolated experiment and all local implementation gates pass.
Ordinary builds, `qualification-db-diagnostics` alone, and the M007 feature
with its startup toggle disabled retain the single-connection topology. The
enabled experiment adds one private checkpoint-only connection and worker,
coalesces successful-commit notifications into the existing supervised task,
and preserves the primary 1000-frame automatic-checkpoint fallback.

M007's paired same-binary performance disposition is still unknown. The plan
requires three 60-request control runs, three 60-request candidate runs, and
one candidate 300-request convergence corpus on a physically attested
Linux/aarch64 Raspberry Pi-class MMC target. This environment is macOS
x86_64. No host run can satisfy that operational gate, so M007 is blocked,
not positively or negatively qualified. No production adoption or storage
performance claim follows from the local feature tests.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| SQLite/build baseline | Locked `rust/Cargo.lock`; bundled engine inspection; local Rust/Cargo build | pass | tokio-rusqlite 0.8.0, rusqlite 0.40.2, libsqlite3-sys 0.38.2, bundled SQLite 3.53.2; local toolchain Rust 1.98.1. Rust 1.89 all-target check also passes. |
| Non-default feature isolation | `rust/Cargo.toml`; default/no-default builds; default full suite | pass | M007 is not in default features or package/release commands. |
| Plan-239 diagnostics feature alone | Diagnostics-only all-target check/Clippy; `maintenance_tests` | pass | Check/Clippy passed; 5 maintenance tests passed. M007 connection fields and second connection compile only under the new M007 feature. |
| Diagnostics-only and disabled M007 topology | Feature-gated topology tests in `rust/src/db/connection.rs` | pass | Diagnostics-only and feature-enabled/toggle-off modes retain one connection. |
| Enabled private checkpoint connection | Feature-enabled DB tests | pass | One same-file checkpoint-only worker; primary autocheckpoint stays 1000, dedicated autocheckpoint is 0. |
| Successful-COMMIT-only coalesced wake | Feature-enabled DB and supervisor tests | pass | Failed body and failed commit do not signal; task remains single-owner with timer fallback and cancellation join. |
| Checkpoint/write concurrency and bounded results | Feature-enabled DB tests | pass | Maintenance does not acquire the foreground gate; PASSIVE busy/incomplete remains bounded and visible. |
| Local Rust regression matrix | Default, no-default, and feature-enabled serial workspace suites | pass | 65 suites in each profile; feature-enabled run reports 845 passing tests. |
| Strict compile, formatting, and release matrix | fmt, default/no-default Clippy/check, default and feature locked release builds | pass | No-default checks compile and lint the reduced surface. |
| Rust 1.89 compatibility | `rustup run 1.89.0 cargo check --manifest-path rust/Cargo.toml --workspace --all-targets` | pass | Executed through Rustup because the `rtk cargo` wrapper does not accept Cargo's `+toolchain` prefix. |
| Python runner/tooling | Ruff format/check, Pyright, full `tests/tooling/` | pass | 154 passed, 1 skipped. Includes M007 mode/schema projection tests. |
| Dependency policy | `cargo deny --manifest-path rust/Cargo.toml check` | pass | Advisories, bans, licenses, and sources pass; existing unrelated duplicate-version warnings remain. |
| Hosted CI | Run `37070421301`, head `6f0cfd532e753a43c454b370453850c55679aab5` | pass | 832 default Rust tests across 65 suites; formatting, strict default/no-default Clippy/check, and tooling (152 passed, 3 skipped) passed. |
| Hosted dependency audit | Run `37070423689`, same head | pass | Completed successfully. |
| Pi/MMC paired latency and convergence corpus | No physically attested Linux/aarch64 Pi-class MMC target available | blocked | Required to determine performance disposition; local host results are not substitutes. |
| Production adoption decision | Not in M007 scope | not authorized | A positive target result would still require a separate explicit architecture decision. |

## 3. Implementation evidence

The SQLite baseline is `tokio-rusqlite 0.8.0`, `rusqlite 0.40.2`,
`libsqlite3-sys 0.38.2`, and bundled SQLite 3.53.2. The experiment is behind the non-default
`qualification-dedicated-checkpointer` feature and the startup-only
`EGGPOOL_QUALIFICATION_DEDICATED_CHECKPOINTER` toggle. Invalid toggle values
fail closed. The feature adds no Config, CLI, HTTP, public Rust API, schema,
dependency, or ordinary runtime task surface. Only successful commits notify
the coalescing signal after releasing the primary gate. The existing
checkpoint task responds to that signal and retains its configured timer
fallback. Dedicated maintenance is checkpoint-only, uses NOOP observation
and PASSIVE work, and closes before the primary connection.

`scripts/qualification_sbc.py` accepts explicit `control` and `candidate`
M007 phases, validates the feature-only bounded diagnostic projection, and
can extend one candidate invocation to five 60-request windows (300 requests)
with WAL convergence and lifecycle evidence. Candidate-close trace evidence
is required. The plan's same-binary identity is recorded by the runner.

## 4. Verification executed

The following local commands ran against the implementation commit. Hosted
CI and dependency audit results are also recorded in the evidence matrix above.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --features qualification-dedicated-checkpointer -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --features qualification-dedicated-checkpointer -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --features qualification-db-diagnostics
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --features qualification-db-diagnostics -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib db::connection::maintenance_tests --features qualification-db-diagnostics -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-dedicated-checkpointer
rustup run 1.89.0 cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
cargo deny --manifest-path rust/Cargo.toml check
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

The feature-enabled full workspace run passed 845 tests across 65 suites.
Default and no-default serial workspace runs completed successfully across 65
suites each; hosted CI independently passed 832 default tests across 65 suites.
Ruff, Pyright, the full tooling suite, strict compile/lint, both release
builds, Rust 1.89, Cargo policy checks, hosted CI, and hosted dependency audit
passed. No physical qualification run was attempted from this host.

## 5. Invariant and compatibility review

- Ordinary runtime behavior remains one SQLite connection, one primary gate,
  one worker, WAL/NORMAL, and `wal_autocheckpoint=1000`.
- M007 disabled behavior is timer-only and remains one connection; the prior
  diagnostics-only feature also remains one connection.
- The enabled experiment adds only one private checkpoint connection/worker;
  it does not acquire the primary gate or accept general repository work.
- Effective enabled-mode pragmas remain primary WAL/NORMAL with
  `wal_autocheckpoint=1000` and dedicated WAL/NORMAL with
  `wal_autocheckpoint=0`; the existing 256-frame soft threshold is unchanged.
- Schema 54, migrations, durable rows, public APIs, and provider/wire behavior
  are unchanged. No new dependency was added.
- No production/default topology change is authorized by local evidence.

## 6. Security and failure review

The new diagnostic projection is scalar and bounded; it excludes database
paths, SQL, request identity, bodies, credentials, and provider/model/account
names. Toggle parsing is strict. PASSIVE busy/incomplete results do not advance
the maintenance watermark, and no retry queue or per-request task is created.
The private connection closes before the primary connection. Feature-enabled
backup/recovery and lifecycle regressions passed in the full workspace suite.

## 7. Physical qualification handoff

On a physically attested Linux/aarch64 Raspberry Pi-class system with the
target MMC filesystem, check out implementation commit
`6f0cfd532e753a43c454b370453850c55679aab5`, build one release binary with the
qualification feature, and record its SHA-256. Keep that exact binary for
every invocation. From the repository root, build and run the Plan 239
publication-phase corpus with distinct output paths:

```bash
cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-dedicated-checkpointer
sha256sum rust/target/release/eggpool
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --diagnose-publication-phases --diagnose-dedicated-checkpointer control --output artifacts/qualification/m007-control-1.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --diagnose-publication-phases --diagnose-dedicated-checkpointer control --output artifacts/qualification/m007-control-2.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --diagnose-publication-phases --diagnose-dedicated-checkpointer control --output artifacts/qualification/m007-control-3.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --diagnose-publication-phases --diagnose-dedicated-checkpointer candidate --output artifacts/qualification/m007-candidate-1.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --diagnose-publication-phases --diagnose-dedicated-checkpointer candidate --output artifacts/qualification/m007-candidate-2.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --diagnose-publication-phases --diagnose-dedicated-checkpointer candidate --diagnose-dedicated-checkpointer-steady-state --output artifacts/qualification/m007-candidate-300.json
```

The last candidate includes its initial 60-request acceptance window and four
additional 60-request windows (300 requests total). Preserve all six reports,
the binary checksum, physical board/filesystem/storage attestation, and the
backup, recovery, restart, rehash, and shutdown evidence. A later closure must
evaluate the plan's latency, WAL growth/progress, convergence, and lifecycle
criteria without inferring a production decision. If the target remains
unavailable, M007 remains blocked with performance disposition unknown.

## 8. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| operational blocker | Required Pi/MMC paired performance corpus is unavailable from this macOS x86_64 host. | Candidate performance and target-class topology disposition remain unknown. | Run the physical handoff corpus above; do not substitute host results. |

No code correctness or compatibility finding remains from local verification.

## 9. Unblock audit and roadmap disposition

M007 is blocked only on its explicit operational evidence gate. It unblocks no
registered implementation plan, and no plan has M007 as a hard dependency.
The persistence roadmap remains active because its target-class checkpoint
tail has no accepted disposition; ordinary runtime remains unchanged.

Dashboard M010's closure audit found no newly eligible dashboard successor.
Provider-transport M002's Eggfetch 0.2.2 interface dependency was independently
satisfied before this work; its closure is recorded separately. C001 remains
ready and independent of all three requested plans.

`plans/closure/persistence/007-status.md` remains the historic pre-reactivation
assessment. This record is the current revised-scope implementation and
operational disposition.
