# M12-P006 Closure — Rust-Only Qualification and M12 Closure

Status: accepted/closed 2026-09-11

Plan: [P006 — Rust-only qualification and M12 closure](../../implementation/retirement/006-rust-only-qualification-and-m12-closure.md)

Machine evidence: [`006-run.json`](006-run.json)

## Decision

P006 is accepted. M12 is closed. The post-retirement repository, current
publication path, supported artifacts, state/recovery behavior, and explicit
historical package-manager compatibility now satisfy the ADR-0005 and P006
acceptance criteria. Current EggPool production and release behavior is
Rust-only; retained Python is bounded tooling and historical package metadata
evidence is external and immutable.

The final qualification baseline before this append-only closure record was
commit `9916071456a1e218690d4f7308df1460825bece4`, with P001 reference commit
`c6b5d2c25038a8ac155c71f68fd50afea03fa459`. The closure commit is the Git
commit containing this record and the registry transition.

## Source and ownership boundary

- `src/eggpool/` is absent; current runtime, migrations, templates, wire
  profiles, and dashboard assets are Rust-owned.
- `packaging/pypi/pyproject.toml` is the sole current publication manifest.
- The root project is tooling-only. Retained Python is limited to bounded
  release/qualification scripts and `tests/tooling/`; no current production or
  test path imports or launches the historical application.
- The P001 manifest remains recoverable from immutable Git history and is not
  duplicated into an active archive. Seven Rust-owned runtime assets and 54
  migration/checksum entries were revalidated.

## Artifact and runtime qualification

Fresh locked release builds produced wheel/raw pairs for Linux x86_64, Linux
aarch64, and macOS arm64. The machine report records filenames, sizes, hashes,
manifest identity, native payload inspection, and the local portability limit.
All wheels contain the native `eggpool` executable, no Python dependencies,
and `Requires-Python >=3.11`; no sdist was emitted. The macOS arm64 wheel
passed installed `version`, `help`, `check-config`, foreground health, and
bounded dashboard checks. The Linux target runtime/portability checks are
source-fresh hosted M11/Q005 evidence; the local macOS host has no `readelf`.

The release catalog, cutover documentation, M12 package boundary, retirement
boundary, release workflow, and complete artifact manifest validators all
passed. No public publication was performed for P006.

## Historical exact-version qualification

The immutable public Python 0.7.4 wheel was used as the historical leg. The
local arm64 run passed both `uv tool` and isolated `pip` for
Python 0.7.4 → Rust 0.8.0 → Python 0.7.4 → Rust 0.8.0. Each leg preserved
configuration, reported SQLite integrity `ok`, retained migration maximum 54,
and ended with package metadata/CLI version 0.8.0. The accepted hosted M11
qualification additionally passed the target matrix's Linux x86_64 `uv tool`,
`pipx`, and isolated `pip` paths plus Linux aarch64 and macOS arm64 `uv tool`.

The Rust latest/current path is Rust-only; incompatible historical targets are
rejected before mutation, standalone Rust rejects Python targets, and no
repository-local Python source participates in these transitions. These
properties are covered by the accepted K001/K005/K006/K012 evidence and the
retained tooling/Rust tests.

## State, recovery, release, and security review

Accepted Q003/O010/R013/C011 evidence remains source-fresh because P002–P005
changed retirement/package/documentation ownership, not schema, runtime
lifecycle, backup, restart, rehash, or request-finalization semantics. It
continues to prove schema-54 integrity, backup/restore, bounded restart
reconciliation without provider replay, stop/restart/rehash/runtime status,
and no high/medium lifecycle or data-loss finding. The accepted K014 public
release evidence remains source-fresh for hosted native wheel checks and
Trusted Publishing/recovery structure.

The strict Clippy gate was run and reproduced the documented pre-existing M11
baseline: 66 errors and one warning in unrelated runtime code. P006 adds no
Rust runtime changes and the finding is explicitly reviewed as unrelated to
retirement; it is not silently omitted from the required gate. No P006-
introduced high/medium packaging, compatibility, security, lifecycle,
evidence-loss, or data-loss finding remains.

## Required verification

Passed on the final qualification baseline:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
  468 passed across 52 suites
rtk cargo build --manifest-path rust/Cargo.toml --locked --release
  finished successfully
rtk uv sync --frozen
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
  76 passed
rtk uv run ruff format --check scripts/ tests/tooling/
  42 files already formatted
rtk uv run ruff check scripts/ tests/tooling/
  all checks passed
rtk uv run pyright scripts/
  0 errors, 0 warnings, 0 informations
rtk uv run python scripts/check_cutover_catalog.py
  K001 catalog valid: 57 releases; 0.8.0 current; 8 rollback-compatible
rtk uv run python scripts/validate_cutover_docs.py
  pass; 7 docs; 3 targets
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
  pass; 35 actions; 9 jobs; 3 targets
rtk uv run python scripts/validate_m12_package_boundary.py --workflow .github/workflows/release.yml
  pass; current runtime Rust; packaging/pypi/pyproject.toml authority
rtk uv run python scripts/validate_m12_retirement.py
  pass; 7 assets; 54 migrations
rtk uv run python scripts/validate_cutover_artifacts.py <manifest> --artifact-dir <artifacts>
  pass; 3 targets
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
  reviewed pre-existing baseline; 66 errors and 1 warning, no P006 delta
rtk git diff --check
  pass
```

The artifact paths above are disposable qualification paths and are not
claimed as repository artifacts; bounded machine-readable evidence is retained
in `006-run.json`.

## Registry transition and future-plan audit

P001–P006 are accepted/closed and M12 is closed in the registry, roadmap,
handoff sequence, and retirement plan index. The dependency-ready table is
empty. The future-plan audit found no later migration implementation plan that
can be promoted: the planning process says no M13 milestone is auto-created,
so subsequent work returns to ordinary product/maintenance roadmaps.

This accepted record is the final M12 closure authority. Historical P001–P005
records remain append-only and unchanged.
