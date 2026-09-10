# K002 Closure — Rust PyPI Binary-Wheel Packaging Substrate

Status: accepted; closed 2026-09-10

Plan: [K002 — Rust PyPI binary-wheel packaging substrate](../../implementation/cutover/002-rust-pypi-binary-wheel-packaging-substrate.md)

## Implementation

- `aec2b98f291c47e983773c42b0d19649973e3377` — implement the dedicated
  Maturin binary-wheel publication manifest, candidate version transition,
  deterministic wheel inspector, isolated package-manager qualification, and
  focused K002 tests.
- The root Python oracle remains version `0.7.4`; Cargo and the Rust lockfile
  now own candidate version `0.8.0`. No public release or package upload
  occurred.

## Artifact evidence

Reviewed Maturin: `1.14.1`.

Exact build command:

```text
uv run --no-project --with maturin==1.14.1 python scripts/build_cutover_wheel.py --target aarch64-apple-darwin --out dist/cutover-k002
```

The command emitted no sdist and produced:

| Target | Wheel | Tag | SHA-256 | Size |
|---|---|---|---|---:|
| macOS arm64 | `eggpool-0.8.0-py3-none-macosx_11_0_arm64.whl` | `py3-none-macosx_11_0_arm64` | `6bdac8713f891a1b27e7f2d1c35b3f4034e60b3d3def2f60a56f4fa994745402` | 10,883,379 bytes |

The wheel inspector confirmed project name `eggpool`, exact version `0.8.0`,
`Requires-Python: >=3.11`, `Root-Is-Purelib: false`, one native
`eggpool-0.8.0.data/scripts/eggpool` executable (27,788,720 bytes), valid
RECORD hashes, and no `Requires-Dist` entries. Its complete inventory is the
executable, METADATA, WHEEL, RECORD, the MIT license file, and the generated
CycloneDX SBOM. The wheel is below the configured 100,000,000-byte PyPI file
bound and is not `py3-none-any`.

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| Maturin `bin`, Cargo lock, PyPI-compatible metadata | `packaging/pypi/pyproject.toml`; build preflight | Pass |
| Cargo/publication version identity | K001 catalog checker; Cargo `0.8.0`; dynamic publication version | Pass |
| Python oracle preserved | root `pyproject.toml` remains `0.7.4` with `eggpool.cli:main`; K002 preservation test | Pass |
| No Python runtime graph | wheel metadata inspection; zero `Requires-Dist` entries; no Python payload | Pass |
| Native runtime independence | extracted wheel executable ran `version` under `arch -arm64` with an empty PATH | Pass |
| Installed CLI/config/server/dashboard proof | disposable pip venv: `version`, `help`, `check-config`, `/v1/healthz`, `/static/dashboard.css` | Pass |
| Uninstall/reinstall ownership | disposable pip venv uninstall removed the wheel-owned executable, then reinstall restored it | Pass |
| uv tool and pipx | qualification runner attempted both in isolated roots | Skipped: installed manager executables are Intel-only on this Intel host and cannot install/run the arm64 wheel |
| Unsupported target/tag rejection | builder rejects unqualified targets; inspector rejects universal/wrong-target fixtures | Pass |
| Rust and migration behavior | commands below | Pass |

The uv/pipx result is an environment qualification boundary, not a wheel
defect. K003 must repeat those manager paths on native qualified Linux/macOS
hosts; no manager was allowed to mutate the developer's installation.

## Verification commands

All commands completed successfully:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

Results: 445 Rust tests passed; 185 migration tests passed with 3 expected
skips; 14 smoke tests passed; formatting, lint, and pyright passed. The
focused K001/K002 tests passed 15 tests, and the isolated wheel qualification
passed pip/native-runtime/dashboard checks with the uv/pipx skips recorded
above.

## Dependency and security review

Maturin is build/release-only and is pinned to `1.14.1`. No Rust runtime
dependency was added. The publication wheel contains no credentials, config,
database, user state, Python application package, or debug artifact. The root
Python dependency graph and console-script definition remain untouched.

Unresolved high/medium findings: none.

## Registry transition

K002 is accepted and closed. K003 is promoted to the sole dependency-ready
plan. K004 through K012 remain queued behind their direct predecessors; K012
alone may close M11. No M11 public publication and no M12 Python retirement is
authorized by this closure.
