# M12 Python Retirement Research Notes

Status: planning input frozen 2026-09-11

Baseline: `385cc2355e84db6071ab35e81b14f55e344afd77`

These notes summarize the repository and packaging research used to expand M12 from the provisional P001-P004 outline into a bounded P001-P006 implementation sequence.

## Current repository facts

- Rust `0.8.0` is the canonical public runtime after accepted M11 closure.
- `packaging/pypi/pyproject.toml` already builds the native Rust executable with Maturin `bindings = "bin"`.
- The public package identity remains `eggpool`; M11 qualified Linux x86_64, Linux aarch64, and macOS arm64 wheels.
- `src/eggpool/` remains the full historical Python application/oracle and root `pyproject.toml` remains a publishable Hatchling Python project at `0.7.4`.
- `tests/migration_rs/` contains both useful deterministic fixtures/contracts and live Python/Rust oracle machinery; it cannot be deleted as a unit without first classifying its contents.
- `scripts/` mixes production release tooling, operational qualification, and migration-only Python helpers.
- K004/K005 implement package-manager-owned exact transitions through uv, pipx, and pip/venv; K012 qualified public `0.7.4 -> 0.8.0 -> 0.7.4 -> 0.8.0` transitions with preserved state.
- The K001 installable-release catalog now records every stable release through `0.8.0`, including immutable PyPI file identities and the schema-54 compatible Python rollback window `0.6.7..0.7.4`.

## Packaging research conclusions

### Historical artifacts should be referenced, not rebuilt

PyPI announced in July 2026 that releases older than 14 days reject new file uploads. PyPI also forbids reusing a distribution filename even after deletion. M12 therefore treats existing Python wheels/sdists as immutable external evidence. It does not plan to "repair" old releases, add new wheels to them, or copy every historical artifact into the repository.

Reference:
- https://blog.pypi.org/posts/2026-07-22-releases-now-reject-new-files-after-14-days/
- https://pypi.org/help/

### A wheel can remain the package-management envelope for a native program

The Wheel specification installs distribution metadata plus payload files and uses `.dist-info/RECORD` to describe installed ownership. EggPool's Maturin `bin` wheel already places the native executable in that package-managed envelope. Removing the Python application source from the repository therefore does not require abandoning PyPI, pipx, uv tool, or ordinary pip/venv ownership.

Reference:
- https://packaging.python.org/en/latest/specifications/binary-distribution-format/
- https://packaging.python.org/en/latest/specifications/recording-installed-packages/

### `.dist-info` remains the transition/provenance authority

The installed-project specification defines `METADATA`, `RECORD`, `INSTALLER`, and `direct_url.json` as the normal package-manager provenance surface. K004 already consumes those signals. M12 should preserve that implementation rather than replacing it with PATH heuristics or a new installer database.

Reference:
- https://packaging.python.org/en/latest/specifications/recording-installed-packages/
- https://packaging.python.org/en/latest/specifications/direct-url/

### `Requires-Python` is metadata, not a Rust runtime dependency

EggPool's native wheel currently advertises `Requires-Python >=3.11`. Normal EggPool execution does not start Python, but package-managed exact downgrade to a historical Python release requires a compatible interpreter environment. Because cross-era exact switching is already qualified and useful, M12 should retain this floor rather than removing it merely to make metadata look more "pure Rust".

### Do not publish an sdist as an M12 cleanup shortcut

Rust production remains wheel-only on the qualified targets. A new sdist would allow installers on unqualified targets to attempt local source builds, reintroducing build-environment variability after M10/M11 deliberately constrained the public support matrix.

## M12 design conclusions

1. Pure Rust means one current production/runtime implementation, not erasing immutable historical versions from package indexes.
2. Explicit compatible historical downgrade remains supported; automatic/latest behavior remains Rust-only.
3. The full Python source should not be copied into an archive directory. Freeze source commit/hashes and selected fixtures, then rely on Git history for the full source.
4. Runtime assets and database migration evidence must be checked before deleting `src/eggpool/`; no asset may disappear merely because it originated under the Python package.
5. Python developer tooling may remain when it is clearly tooling-only. The heavy historical application dependency graph and publishable root Python package should not.
6. Live Python-oracle tests should be converted to frozen fixtures or Rust-native contract tests before deletion, not simply removed because M11 passed once.
7. M12 closure should include an actual wheel build/install from the post-retirement tree and a compatible historical exact-version transition, proving both pure-Rust current operation and preserved cross-era package behavior.

## Resulting implementation decomposition

- P001 — final reference/fixture boundary freeze.
- P002 — current package/catalog/update authority and accepted ADR-0005.
- P003 — Python application source/runtime-asset retirement.
- P004 — oracle/differential/test and Python development-tooling retirement.
- P005 — repository, installer, release workflow, and documentation consolidation.
- P006 — aggregate Rust-only qualification and M12 closure.

This decomposition keeps source deletion, test deletion, packaging changes, and final qualification separately reviewable and prevents a single broad cleanup commit from destroying evidence needed to diagnose a regression.
