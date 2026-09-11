# K005 Closure — Cross-Era Exact Version Transitions and Rollback

Status: accepted; closed 2026-09-11

Plan: [K005 — Cross-era exact version transitions and rollback](../../implementation/cutover/005-cross-era-exact-version-transitions-and-rollback.md)

## Decision

K005 is accepted and closed. Package-managed transitions now preserve manager
ownership across the Python/Rust boundary, perform bounded exact rollback after
post-mutation failures, and return an explicit `rollback_failed` error with the
previous and target versions plus a manual recovery command when rollback
cannot complete. Update-manager subprocesses retain only the bounded manager
environment required by the owning tool, including the helper path and local
wheelhouse/index controls.

## Implementation

- `393745ba5c8afe46024923922f360672ee92aa6e` — exact package transition
  rollback, typed rollback failure, manager helper environment, identity-checked
  stale-lock recovery, pipx/uv provenance compatibility, and focused Rust tests.
- The same commit adds `scripts/qualify_cutover_transitions.py`, an artifact-
  driven matrix runner with bounded results, and its migration test coverage.

## Exact artifact matrix

The strict native-arm64 qualification used the following immutable local
artifacts:

| Era | Artifact | SHA-256 | Size |
|---|---|---|---:|
| Python | `eggpool-0.7.4-py3-none-any.whl` | `47b61c1c9db3ee9fa8945bebed89c7294cb867240b2204b1bfc39e443d1a9581` | 1,260,813 |
| Rust | `eggpool-0.8.0-py3-none-macosx_11_0_arm64.whl` | `7dac275047b6af4ba99d90faf63af89729bf35c8c0aed2329ef42e9a79a51783` | 10,955,757 |

The runner exercised Python `0.7.4` → Rust `0.8.0` → Python `0.7.4` →
Rust `0.8.0`, with the final exact-current update treated as a no-op. Every
manager completed the cycle:

| Manager | Result | Rollback | Final CLI/metadata | DB |
|---|---|---|---|---|
| uv tool | pass | pass | `0.8.0` / `0.8.0` | integrity `ok`, migration max `54` |
| pipx | pass | pass | `0.8.0` / `0.8.0` | integrity `ok`, migration max `54` |
| isolated pip/venv | pass | pass | `0.8.0` / `0.8.0` | integrity `ok`, migration max `54` |

The runner compared config bytes before and after each cycle and preserved the
same config/DB paths. Final observations retained the provider/model fixture
inventory and the complete migration ledger; no database reset, copy, or
version-specific state path was used.

## Rollback, concurrency, and safety evidence

- A manager non-zero result after target mutation restores the previous exact
  release through the same manager and preserves the executable/configuration.
- A second manager invocation while the update lock is held returns
  `update_in_progress`; stale lock removal requires matching canonical
  executable identity and a dead recorded PID, and lock files are private and
  removed by RAII.
- Rollback-manager failure is covered by a typed `RollbackFailed` test with
  `eggpool update 0.7.4` as the recovery command.
- Manager output remains bounded; subprocesses run with cleared, allowlisted
  environments, fixed argv, a timeout, and `kill_on_drop`. No raw executable
  replacement is used for package-managed installations.
- Existing O008 standalone updater regressions remain isolated to the
  standalone provenance branch. Cancellation/timeout behavior remains bounded
  by the existing child-process lifecycle and the new package rollback path.

## Historical target and negative cases

The selected Python target is the K001 installable `0.7.4` universal wheel,
not a mutable branch/tag or an unverified historical fallback. Catalog and
focused tests retain exact-version, leading-`v`, current/latest no-op,
unknown-target, incompatible-Python, unsupported-platform, and malformed
provenance rejection coverage. The transition runner uses a local wheelhouse
for exact manager resolution and records no raw subprocess output or
environment secrets.

## Verification

Passed:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1  # 41 passed
cargo test --manifest-path rust/Cargo.toml --tests -- --test-threads=1  # 460 passed across 52 suites
uv run python scripts/check_cutover_catalog.py  # 56 releases; 8 rollback-compatible
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 191 passed, 3 skipped
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
git diff --check
```

The strict native-arm64 matrix returned exit code 0 for uv tool, pipx, and
isolated pip. A complete Linux package-manager transition run remains required
in K006/K007 environments, as specified by the plan; it is not a K005 blocker
or an unresolved version/data-loss finding.

## Findings

No unresolved high/medium version-transition, provenance, command-injection,
rollback, or data-loss finding remains for the K005 scope. One compatibility
edge found during qualification—pipx using uv as its internal backend—was
fixed and covered by a regression test.

## Registry transition

K005 is accepted and closed. Per the plan's explicit promotion rule, K006 is
now the sole dependency-ready M11 plan. K007-K012 remain queued behind their
direct predecessors; no later plan is unblocked by K005 alone, and M12 remains
sequenced behind accepted K012.
