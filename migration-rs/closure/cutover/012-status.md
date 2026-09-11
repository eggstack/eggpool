# K012 — Aggregate M11 Cutover Qualification and Closure

Status: **blocked; not accepted**

Plan: [K012 — Aggregate M11 cutover qualification and closure](../../implementation/cutover/012-aggregate-m11-cutover-qualification-and-closure.md)

Recommendation: **corrective pass required**. This is the formal K012 closure
review record for the available evidence; it does not close M11, promote Rust
to the accepted canonical public runtime, or authorize M12 planning.

## Decision and blocker

K012 cannot be accepted because K011 has not completed its mandatory public
PyPI release. The frozen Rust candidate is `0.8.0` at tag `v0.8.0`, source
commit `431cad4f46a2d4bbcbc8839c18b71f392c0616ca`. The GitHub release contains
the raw target assets and release manifest, but no `eggpool 0.8.0` PyPI
release exists. The recovery workflow reached the PyPI Trusted Publisher
exchange and failed with `invalid-publisher`; no PyPI wheel was uploaded.

This prevents the required public `uv tool`/`pipx`/isolated-pip installation,
public exact update, and public Python -> Rust -> Python -> Rust rollback
matrix. K014 owns the maintainer-side publisher configuration and exact-bundle
recovery. K011, K013, and K014 remain incomplete or blocked. No future plan is
unblocked by this review; M12 remains blocked behind accepted K012.

## Implementation and release identity

The reviewed implementation baseline is `257f35c188a13110c47c9cbaecd533bf858aac48`.
The cutover/recovery commits reviewed are:

- `431cad4f46a2d4bbcbc8839c18b71f392c0616ca` — frozen `v0.8.0` release/tag;
- `3d15473e2cafd011f64bc2a5ad98eff1b7abff02` — exact-bundle recovery;
- `84aa0818ffe57ae9cfac91d0f35c0a83812a602` — recovered bundle layout;
- `30e5bf466dbecbe0e9e81836adf9b818049415fd` — bundle normalization;
- `5aef5201e4282030de5e7e99621ffd616ef02968` — checkout-before-recovery;
- `257f35c188a13110c47c9cbaecd533bf858aac48` — blocked K011/K013 status.

The production workflow runs were `34570717210` (initial failed publisher
action) and `34574234702` (corrected exact-bundle recovery). The latter passed
identity, target builds, aggregation, provenance, and hashes, then failed
only at the PyPI OIDC publisher exchange.

## Public artifact inventory

The public GitHub release `v0.8.0` has the following five assets: three raw
executables, `SHA256SUMS`, and the release manifest. The downloaded public
assets matched the manifest and sidecar hashes:

| Target | Raw asset | Size | SHA-256 |
|---|---|---:|---|
| Linux x86_64 | `eggpool-0.8.0-linux-x86_64` | 27,502,448 | `7892c7a3115f4a496e2aff820c6381b39a46e8694642278511c5e265b3da5db1` |
| Linux aarch64 | `eggpool-0.8.0-linux-aarch64` | 23,527,480 | `2e9382561f7a7267be4eefd08c555d5b53c3a395c68cbe5eacab18732177abe0` |
| macOS arm64 | `eggpool-0.8.0-macos-aarch64` | 27,609,520 | `785ca3cf2100d38bce0b0d32a2ef8dfeb16eecfb45a42c4166795179d1e0315a` |

The public release manifest SHA-256 is
`7b967a29f0416034fd5c0272c717d5607c124c8611a43be4567d0cc4986f99a7`.
The manifest records the three wheel names and hashes, but those wheels are
not GitHub assets and are absent from public PyPI. Therefore the public wheel
inventory, PyPI metadata, attestations, and post-publication wheel verifier
cannot pass.

The raw asset inspectors passed after restoring executable mode in the
temporary download directory. The macOS portability inspector passed; the
Linux portability inspector could not run on this macOS host because
`readelf` is unavailable. Existing hosted Q005/K003 evidence remains the
target-appropriate portability authority.

## Public package-manager matrix

| Path | Result | Evidence |
|---|---|---|
| Public `uv tool` fresh install | blocked | PyPI has no `0.8.0` wheel |
| Public `pipx` fresh install | blocked | PyPI has no `0.8.0` wheel |
| Public isolated `pip`/venv fresh install | blocked | PyPI has no `0.8.0` wheel |
| Staged `uv`/`pipx`/`pip` transitions | pass | Accepted K005/K006/K009 wheelhouse evidence |
| Latest/exact public update | blocked | No public Rust package target |
| Wheel-manager/raw-updater isolation | pass in code/tests | K004-K010 validators and migration tests |
| Unsupported-target no-sdist behavior | pass in code/tests | K001/K003/K008/K009 contract evidence |

No staged wheelhouse or TestPyPI result has been relabeled as public evidence.
The public install/metadata verifier was run against current PyPI and GitHub
metadata and failed with `PyPI version does not match the release manifest`,
which is the expected blocker result.

## Cross-era rollback and existing-install adoption

The accepted K005/K006/K007 evidence proves the manager-owned and deployed
state-preservation paths against the staged Rust wheel: Python `0.7.4` -> Rust
`0.8.0` -> Python `0.7.4` -> Rust `0.8.0`, for uv, pipx, isolated pip, and a
disposable Linux service. Config bytes, database paths/integrity, migration
maximum `54`, provider/model fixture facts, manager ownership, and service
state were preserved.

K012 cannot independently requalify that path through the public index because
the first Rust wheel was never published. Existing-install adoption and
deployed-service evidence therefore remain staged/previously accepted
evidence, not K011 public-release evidence. No config, database reset, manual
package cleanup, or raw package-manager overwrite was performed by this
review.

## Standalone path isolation

The public GitHub raw assets and manifest are internally consistent, and the
raw release path remains distinct from package-managed update authority. The
static release/workflow validators and migration tests pass the fixed-argv,
hash, target, provenance, and no-sdist contracts. A package-managed public
install could not be exercised, so standalone-to-wheel adoption is not a
complete public end-to-end result.

## M10 freshness and required verification

No Rust runtime, Python runtime, database, provider, coordinator, or deployment
code changed after the `v0.8.0` release commit; post-release changes are
release workflow, validation, documentation, and closure-record changes.
Accordingly, the existing Q005/Q006/Q007/Q008/Q009/Q011 target, rootful,
provider, SBC, and stability evidence remains source-fresh. The following
fresh local gates were run on 2026-09-11:

| Command/evidence | Result |
|---|---|
| `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` | pass |
| `cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1` | 463 passed across 52 suites |
| `uv run pytest tests/migration_rs -q --tb=short --maxfail=1` | 213 passed, 3 skipped |
| K001-K011 focused migration tests | 77 passed |
| Q002 aggregate (`qualification_runner.py --skip-build`) | 18 pass, 0 fail/block/infrastructure-error/skip |
| Q003 aggregate (`qualification_database.py --skip-build`) | 4 pass, 0 fail/infrastructure-error |
| Q012 dashboard aggregate (`qualification_dashboard.py --skip-build`) | pass |
| `uv run pytest tests/smoke/ -q --tb=short --maxfail=1` | 14 passed |
| `uv run pyright src/ scripts/` | 0 errors, 0 warnings, 0 information |
| `uv run ruff format --check src/ tests/ scripts/` | pass |
| `uv run ruff check src/ tests/ scripts/` | pass |
| `scripts/check_cutover_catalog.py` | 56 releases; 8 rollback-compatible; candidate 0.8.0 |
| `scripts/validate_cutover_docs.py` | pass |
| `scripts/validate_release_workflow.py .github/workflows/release.yml` | pass |
| `git diff --check` | pass |

The required full-workspace Clippy command was run with the available Rust
1.98 toolchain and with the installed 1.88 toolchain. Both report the same 66
pre-existing `-D warnings` findings across unrelated runtime files; this is
the known baseline recorded by K010, not a K012 or release-workflow change.
It is retained as an environment/toolchain finding rather than hidden or
expanded into an unrelated refactor.

## Footprint and security review

The public raw executables are native ELF/Mach-O binaries. Public wheel
contents, wheel size, package-manager dependency count, and public fresh
process-tree/RSS measurements remain unavailable because PyPI publication did
not occur. The accepted K002/K003/K009 staging characterization remains the
available footprint evidence and found no Python application dependency in the
Rust wheel or Python child-process requirement for normal Rust operation.

The release workflow and recovery path were reviewed for fixed command vectors,
immutable tag/source/manifest/hash checks, package-manager ownership, raw asset
hashes, OIDC-only publication, secret isolation, unsupported-target behavior,
rollback compatibility, PATH collision handling, and partial-publish recovery.
The corrected recovery run failed closed at the missing Trusted Publisher; no
credential fallback or unverified upload occurred. No new high/medium security
finding was found. The missing public wheel is an acceptance blocker, not a
security waiver.

## Documentation, Python retention, and finding triage

The current README, upgrading/deployment/releasing docs, changelog, catalog,
workflow, and migration registry consistently describe the intended Rust-wheel
channel, supported targets, exact transitions, rollback window, standalone
distinction, unsupported-target policy, and Python reference retention. They
must not be read as proof that the candidate is currently installable from
public PyPI; the release guard and this record preserve that distinction.

The Python source, root package metadata, migration fixtures, differential
tests, final Python catalog entry (`0.7.4`), and rollback evidence remain
intact. No M12 retirement work was performed.

| Finding | Severity | Disposition | Owner/next action |
|---|---|---|---|
| No public PyPI `0.8.0` wheel set | K012 acceptance blocker | open; prevents K011 and K012 acceptance | K014: configure publisher, resume run `34570717210`, then complete K011 |
| Public package-manager install/rollback not executable | K012 acceptance blocker | open as consequence of the missing wheel set | K011 after exact PyPI publication |
| Linux raw portability inspector lacks local `readelf` | informational environment limitation | existing hosted Q005/K003 evidence remains valid | no K012 code action |
| 66 full-workspace Clippy warnings | known pre-existing toolchain baseline | documented in K010; no new K012 runtime delta | separate maintenance decision if desired |

No high/medium cutover, packaging, data-loss, lifecycle, or security finding
can be closed as accepted while the first row remains open. K013 and K014 are
the corrective path; K012's historical record is not rewritten after they
complete.

## Registry transition

The registry remains truthful and unchanged in dependency effect:

- K011 remains blocked on production PyPI Trusted Publisher configuration;
- K013 remains implementation-complete but acceptance-blocked;
- K014 remains blocked pending maintainer PyPI account access;
- K012 is recorded as blocked and is not in the accepted/completed table;
- no future plan is dependency-ready or unblocked;
- M11 remains active, and M12 remains blocked behind accepted K012.

After K014 completes the external publisher exchange and K011 receives an
accepted public-release/rollback closure, this K012 plan may be revisited with
fresh public evidence. Only then may a new accepted K012 closure record close
M11 and make M12 eligible for a separate planning review.
