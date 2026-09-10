# K004 Closure — Install Provenance and Package-Manager Transition Engine

Status: accepted; closed 2026-09-10

Plan: [K004 — Install provenance and package-manager transition engine](../../implementation/cutover/004-install-provenance-and-package-manager-transition-engine.md)

## Decision

K004 is accepted and closed. Rust now detects the owning installation class
from bounded executable, environment, and distribution metadata evidence, then
routes package-managed transitions through exactly one owning manager. The
standalone Rust path retains O008's verified raw-asset replacement. Source and
ambiguous installations fail closed. K001 remains the release/catalog
authority; no public publication or cross-era rollback claim is made by K004.

## Implementation

The implementation is contained in:

- `b33658be47d337c2c8a875b41d50ff436eb0f10b` — provenance model and bounded
  detector, K001-backed release catalog, package-manager transition service,
  runtime integration, typed failures, post-install verification, and focused
  tests.

No database schema or package-manager SDK dependency was added.

## Provenance evidence and decision table

| Class | Required corroborating evidence | Authority | Conflict/failure result |
|---|---|---|---|
| uv tool | `eggpool-*.dist-info`, owning environment under bounded `uv/tools` structure, optional `INSTALLER=uv` | absolute `uv tool install --force` | missing manager executable or conflicting signals is typed failure/ambiguity |
| pipx | `eggpool-*.dist-info`, owning environment under bounded `pipx/venvs` or `pipx/shared` structure, optional `INSTALLER=pipx` | absolute `pipx install --force` | missing manager executable or conflicting signals is typed failure/ambiguity |
| ordinary pip/venv | distribution metadata and owning environment with `pyvenv.cfg`; interpreter is inside that environment | owning interpreter with `-m pip` | absent safe venv marker is ambiguous; system/external environment is not mutated |
| standalone Rust | no trusted EggPool distribution metadata and ELF/Mach-O executable evidence | O008 verified raw asset updater | non-native/unmanaged candidate is ambiguous |
| source checkout | `.git` ancestor or editable/local direct URL resolving to a checkout | explicit developer source workflow | public package updater returns `source_checkout` |
| ambiguous/unmanaged | multiple distributions, malformed metadata, conflicting manager signals, or insufficient ownership evidence | none | typed fail-closed result; never falls back to raw replacement |

The detector retains an exposed executable path separately from the resolved
inspection path, bounds metadata/evidence, and does not print environment
contents, site-package listings, user names, or secrets. Missing `INSTALLER`
is accepted when structural evidence is sufficient.

## Exact manager argv templates

The transition service constructs structured argv and never accepts a shell
command string:

```text
<absolute-uv> tool install --force eggpool==VERSION
<absolute-pipx> install --force eggpool==VERSION
<owning-python> -m pip install --upgrade --force-reinstall eggpool==VERSION
```

`VERSION` is normalized and validated by `ReleaseVersion`, and the requirement
is required to be exactly `eggpool==VERSION`. The subprocess uses a cleared,
manager-specific allowlist containing only HOME/XDG/proxy/certificate settings
and the relevant UV, PIPX, or PIP index settings. Output is bounded to 64 KiB
per stream and execution is bounded to five minutes; timeout, non-zero exit,
and output overflow are distinct typed failures.

## Catalog, preflight, and self-check

K001's embedded `k001.v1` catalog is parsed before mutation. Unknown,
unavailable/yanked, unsupported-target, and incompatible-Python requests stop
before manager execution. Exact current and latest-not-newer requests are
no-ops. The runtime performs a read-only config parse before stopping a running
service; the transition receives the catalog's rollback-compatibility fact and
the target config path. After manager success, provenance is rediscovered,
manager class and target metadata version are checked, `eggpool version` is
checked, and `check-config` is run when a config path is supplied. K005 owns
the full real cross-era database rollback cycle.

## Raw-overwrite and standalone proofs

The package-managed branches invoke only the constructed uv, pipx, or owning
Python command. They do not call `UpdateService::apply_current_executable`;
the focused fake-pip transition test records the manager invocation and proves
the target metadata/entrypoint changed without raw replacement. The only raw
call is in the `StandaloneRust` branch, and it first requires a Rust-era
catalog target. Existing O008 regression coverage remains green, including
verified replacement, bad digest/version refusal, self-check failure
retention, platform selection, and bounded checker behavior.

## Transition journal decision

K004 does not add a persistent journal or database state. A process-local
create-new update lock serializes package mutation and is released by RAII;
the owning package managers provide the installation transaction, while the
post-install checks prevent a false success. A crash therefore leaves the
manager's previous/managed environment for the subsequent detector rather
than an EggPool-authored half-written executable. Persistent crash recovery,
rollback restoration, and service health recovery are explicitly deferred to
K005/K007, where the real installation matrix and deployment state are
available.

## Verification

All required focused and regression checks passed:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run python scripts/check_cutover_catalog.py
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
git diff --check
```

Results: the all-target Rust suite passed (including 36 library tests and all
integration binaries), the focused K004 library set passed 16 tests, O003/O008/
O009 passed 2/7/8 tests, the migration suite passed 189 tests with 3 skips,
the smoke suite passed 14 tests, the catalog checker reported 56 releases and
8 rollback-compatible entries, and Python formatting/lint/type checks passed.

## Findings and security review

No unresolved high/medium update-authority or command-injection finding
remains. Manager paths and arguments are structured; arbitrary environment
variables and provider credentials are excluded; manager output and metadata
are bounded; malformed direct URLs cannot inject arguments or commands; and
ambiguous provenance cannot select standalone replacement. K005 remains the
required qualification for cross-era rollback and recovery semantics.

## Registry transition

K004 is accepted and closed. Per the plan's explicit promotion rule, K005 is
now the sole dependency-ready M11 plan. K006-K012 remain queued behind their
direct predecessors; no later plan is unblocked by K004 alone, and M12 remains
sequenced behind accepted K012.
