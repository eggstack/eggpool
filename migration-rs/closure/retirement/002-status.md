# P002 Closure — Rust Production Package, Catalog, and Cross-Era Authority

Status: accepted/closed 2026-09-11

Plan: [P002 — Rust production package, catalog, and cross-era authority](../../implementation/retirement/002-rust-production-package-catalog-and-cross-era-authority.md)

Implementation commit: `36605a8d890855fa255b9cf96b1ba29f914a9826`

## Outcome

P002 is complete without publishing or mutating a release. The current Rust
runtime and future publication authority are explicit and bounded:

- `packaging/pypi/pyproject.toml` is the sole current/future publication
  manifest and is marked as the current Rust production package;
- the root `pyproject.toml` remains executable historical/development tooling,
  pinned to Python `0.7.4`, and cannot be selected by the release workflow;
- K001 `k001.v2` distinguishes Rust `0.8.0` from immutable historical Python
  artifacts, makes `latest` Rust-only, and keeps exact historical selection
  compatibility-gated;
- `Requires-Python >=3.11` is documented and tested as package-manager
  compatibility for historical transitions, not as a native runtime
  dependency;
- latest raw release resolution rejects Python-era releases, while the
  existing K004/K005 manager ownership, fixed argument vectors, compatibility
  checks, and rollback behavior remain intact;
- incompatible historical state and standalone-Rust-to-Python transitions
  fail before package mutation; no Python fallback is added after Rust failure;
- historical PyPI filenames and hashes are explicitly immutable external
  evidence, with no backfill, delete, yank, replacement, or re-upload;
- no Python application source, Rust migration asset, SQLite schema, database,
  config, service, PyPI release, or GitHub release was mutated by verification.

The missing accepted P001 handoff in the planning baseline was supplied as a
secret-free reference manifest and closure record before accepting P002. Its
frozen source identity remains `c6b5d2c25038a8ac155c71f68fd50afea03fa459`;
P002 does not retroactively alter that identity.

## Verification

Passed:

```text
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
rtk uv run python scripts/validate_m12_package_boundary.py --workflow .github/workflows/release.yml
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 216 passed, 3 skipped
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 466 passed
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk git diff --check
```

Focused Rust operation suites passed: O008 `10 passed`, O009 `8 passed`.
Strict workspace Clippy remains a pre-existing baseline finding outside the
P002 scope: it reports 66 errors and 1 warning across unrelated existing
modules, including one advisory in the already-owned updater module. P002
introduced no Clippy suppression or warning debt.

## Forward handoff

P003 is promoted to the sole dependency-ready plan because accepted P001 and
P002 now provide the reference manifest, package authority, catalog boundary,
and cross-era guards it requires. P004, P005, and P006 remain serially gated
behind their direct predecessors. M12 remains open; only accepted P006 may
close the milestone.
