# K003 Closure — Supported Wheel and Raw Release Artifact Matrix

Status: accepted; closed 2026-09-10

Plan: [K003 — Supported wheel and raw release artifact matrix](../../implementation/cutover/003-supported-wheel-and-raw-artifact-matrix.md)

## Decision

K003 is accepted and closed. The complete M10-inherited Rust artifact matrix
was built from one source revision, qualified on native hosted target classes,
and recorded in the bounded release manifest. No production PyPI/GitHub
publication occurred, and no historical backfill was required.

## Implementation

The implementation is contained in these commits:

- `a15adc4` — target-aware wheel/raw inspectors, manifest builder and
  validator, matrix builder, focused tests, and manual qualification workflow.
- `09c80fd` — support Maturin's dual manylinux platform tags.
- `3c040aa` — qualification result and manifest controls.
- `111a238` — recursive downloaded-artifact layout handling.
- `11c2abd` — preserve executable mode while assembling raw assets.
- `803a114` — deterministic incompatible-Windows resolver proof.
- `e1445e9` — explicit Linux `readelf` and macOS `otool` portability checks in
  each matrix job.

The artifact source revision is `e1445e9560049171a24d7fa3e3aca2f252b6130c`.
The Rust candidate version is **0.8.0**; the root Python oracle remains
0.7.4. The build used Maturin 1.14.1 and Rust 1.98.1.

## Artifact manifest

The immutable staged manifest is
[`k003-release-manifest.json`](k003-release-manifest.json), schema
`m11-release-manifest.v1`, SHA-256
`b348c57bd730423560b9abfa5b0d76be69858b07ea7b2d7308f418f929e6e586`.
It records the source commit, Cargo.lock SHA-256
`7876485f4a4aea44aa830175342b2a5721e47db036b2f1dcf65f21cef50e8a64`,
packaging manifest SHA-256
`c92725a4f970bd9bc808519f669501c83d24188c3d7c5b7e291edf248a308b48`,
qualification result `pass`, and an empty historical backfill list.

| Target | Rust target | Wheel / platform tags | Wheel SHA-256 / size | Raw SHA-256 / size | Payload match |
|---|---|---|---|---|---|
| Linux x86_64 | `x86_64-unknown-linux-gnu` | `manylinux_2_17_x86_64`, `manylinux2014_x86_64` | `3dc565d637c30f55391df445e541d6695745982f3a5932a91dbaa0f7f4bcc968` / 10,708,133 | `36797b324cda3142c6b888713d2a71606aaec7b5232698b65caf745edce44d43` / 25,943,040 | exact executable SHA-256 |
| Linux aarch64 | `aarch64-unknown-linux-gnu` | `manylinux_2_17_aarch64`, `manylinux2014_aarch64` | `a351cfbcdb51556d4cd68d7e27f8504ee03043380b08aebe09caf5cd3842b653` / 10,243,216 | `5649db80ee42c4076d1311c575442c40952b9ce8efda80ee383fe2e961ade79a` / 22,694,088 | exact executable SHA-256 |
| macOS arm64 | `aarch64-apple-darwin` | `macosx_11_0_arm64` | `b8d8283ec968db2a280c5734cc92a0ba65ad00103a1c8ed97c331e7507fd55ac` / 10,819,057 | `28d1ef913b5c2aed0c3b74a5595620d8c36bd9d86dfa9063310f378b5df3d899` / 28,903,400 | exact executable SHA-256 |

The raw names are `eggpool-0.8.0-linux-x86_64`,
`eggpool-0.8.0-linux-aarch64`, and `eggpool-0.8.0-macos-aarch64`. All raw
assets were staged executable (`0755`). The wheel-contained executable is
`eggpool-0.8.0.data/scripts/eggpool` in every wheel.

## Qualification evidence

GitHub Actions run
[`34539302030`](https://github.com/eggstack/eggpool/actions/runs/34539302030)
passed all matrix and manifest jobs on native `ubuntu-latest` x86_64,
`ubuntu-24.04-arm` aarch64, and `macos-14` arm64 runners. Each target ran the
existing K002 qualification sequence: native runtime, isolated pip/uv/pipx
install, `version`, `help`, `check-config`, foreground health/loopback smoke,
dashboard static/read checks, uninstall, reinstall, and version-command
verification. All three target rows reported pass for those manager/runtime
checks.

The same run proved binary portability:

- Linux used explicit Maturin `manylinux2014`, `--auditwheel check`, and Zig
  builds. `readelf` reported observed maximum glibc symbol `2.17`; x86_64
  dependencies were `libc`, `libdl`, `libm`, `libpthread`, and the x86_64
  loader, while aarch64 had the four libraries without an unexpected loader
  dependency.
- macOS used `MACOSX_DEPLOYMENT_TARGET=11.0`; `otool` reported deployment
  target `11.0`.
- A disposable Windows resolver check failed deterministically with no
  matching `win_amd64` wheel. No Windows, other-Unix, universal wheel, sdist,
  or raw fallback was emitted.

The Linux and macOS raw executables were also format/architecture checked as
ELF x86-64, ELF ARM aarch64, and Mach-O arm64 respectively. The manifest
validator rechecked filenames, tags, metadata, hashes, sizes, payload
correlation, executable mode, target completeness, and unsupported-artifact
absence.

## Verification commands

All focused checks and the hosted target qualification completed successfully:

```text
uv run pytest tests/migration_rs/test_k001_catalog.py tests/migration_rs/test_k002_packaging.py tests/migration_rs/test_k003_artifacts.py -q
uv run python scripts/validate_cutover_artifacts.py migration-rs/closure/cutover/k003-release-manifest.json --artifact-dir <downloaded-k003-artifacts>
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
git diff --check
```

The local focused K001/K002/K003 suite passed 9 tests; the full migration and
smoke suites, Rust checks, formatting, lint, and pyright were also run during
the K002/K003 qualification cycle. The final hosted run is the authoritative
native three-target installation/runtime result.

## Findings and security review

No unresolved high/medium artifact portability, integrity, packaging, or
unsupported-target finding remains. No Rust runtime dependency was added.
Build jobs contain no provider credentials or package-index credentials, and
the manifest contains no runner paths, environment dumps, tokens, or download
URLs. Artifact sizes are below the K002 PyPI file bound. Historical backfill
candidates: none.

## Registry transition

K003 is accepted and closed. K004 is promoted to the sole dependency-ready
M11 plan; K005 through K012 remain queued behind their direct predecessors.
No M11 public publication is authorized, and K012 alone may close M11.
