# ADR-0005 — M12 pure-Rust production boundary with immutable historical-version compatibility

Status: accepted for M12 implementation planning

Date: 2026-09-11

Decision scope: M12 Python production/runtime retirement, historical Python release compatibility, repository tooling, and migration-oracle retirement

## Context

M11 made Rust `0.8.0` the canonical public EggPool runtime. The public PyPI project remains `eggpool`; current supported wheels are Maturin `bin` wheels whose installed command is native Rust. The repository still contains the final Python application under `src/eggpool/`, a root Hatchling project at Python version `0.7.4`, Python application tests, and Python/Rust differential machinery.

M11 also established a useful user-facing capability that M12 must not discard accidentally: a package-manager-owned install can select an exact catalogued EggPool version, including a compatible historical Python release, using the same package identity. The public historical wheels are immutable artifacts on PyPI. Current PyPI policy also rejects adding new files to releases older than 14 days, so those artifacts must be treated as immutable external evidence rather than as releases that M12 can later repair or republish in place.

A pure-Rust current production tree does not require deleting the ability to install an old release intentionally. It requires that current/future EggPool releases, services, source authority, and automatic runtime behavior no longer depend on the Python application.

## Decision

1. `packaging/pypi/pyproject.toml` remains the sole production publication manifest for current and future EggPool releases. The native Rust binary is the only current production/runtime implementation.
2. The final Python application source under `src/eggpool/` is removed from the active repository after P001 freezes its source identity, selected fixtures, migrations/assets provenance, and Rust replacement coverage. The full historical source remains recoverable from the immutable recorded Git commit; M12 does not duplicate the complete application into an archive directory.
3. Python may remain only as clearly development-only tooling where a script is still useful for release inspection, fixture processing, or repository maintenance. Such tooling must not be imported by the Rust binary, included as an EggPool application package, required by the installed service, or advertised as a supported current runtime.
4. Historical Python releases remain immutable public history and may remain **explicit exact-version transition targets** when all of the following hold: the release is in the installable catalog, its public artifact identity is known, the owning package-manager environment can run it, and the current DB/config state is compatible with that target. This is an intentional historical-version selection feature, not a hidden runtime fallback.
5. `eggpool update` without an explicit version always resolves a current Rust stable release. Automatic rollback after a failed current Rust transition returns to the previously installed exact version, but no latest/default/error path silently selects Python merely because Rust failed.
6. M12 preserves the K004/K005 package-manager ownership boundary. uv, pipx, and pip/venv continue to own their installed files and exact `eggpool==VERSION` transitions; standalone Rust binaries continue to use the verified raw-asset updater and cannot transition directly to a Python release.
7. The current Rust wheel retains `Requires-Python >=3.11` during M12 because package-manager-owned cross-era exact transitions intentionally preserve the ability to install compatible Python-era releases. This metadata is a package-management compatibility floor, not a Rust runtime interpreter dependency. Removing it requires a later explicit compatibility decision, not incidental cleanup.
8. The root Python `pyproject.toml` ceases to be a publishable EggPool application manifest. Retained Python development tooling must move to or be represented by a clearly non-production tooling configuration whose dependencies are limited to the scripts/tests that survive P004/P005.
9. Migration-only Python/Rust dual-run tests and launchers are retired after useful observations are frozen. Selected deterministic fixtures and Rust-native contract tests remain. No new normalization may be introduced simply to make retirement tests pass.
10. Existing SQLite migrations/checksums, config semantics, assets, API/CLI behavior, dashboard behavior, provider/routing/retry behavior, and service lifecycle are not redesigned by M12. Any discovered parity defect gets a bounded corrective plan rather than being hidden by source removal.
11. Historical PyPI files, M11 release manifests, hashes, attestations, and closure records are append-only evidence. M12 does not delete/yank/rebuild historical releases to simplify the current repository.
12. M12 closes only after a Rust-only source tree can build the supported wheel/artifact set, install and run without the Python application source, preserve config/database state, and still perform the documented compatible historical exact-version transition through package-manager-owned installs.

## Consequences

### Positive

- The current repository and production release path become genuinely Rust-only without losing the exact-version workflow users already have.
- Historical Python compatibility becomes an explicit package-management feature rather than a second current implementation.
- PyPI remains the single package identity and `.dist-info`/`RECORD` ownership stays coherent for uv/pipx/pip installs.
- Full Python source does not have to remain duplicated in-tree merely for auditability; the recorded immutable commit plus selected fixtures are sufficient.
- M12 can remove the heavy Python application dependency graph while retaining small development-only Python utilities where they materially reduce maintenance work.

### Costs

- Keeping compatible historical exact-version transitions means package-managed installs continue to need a usable Python >=3.11 environment even though normal Rust execution does not.
- The installable release catalog remains part of the updater contract and must continue to distinguish artifact availability from DB/config compatibility.
- A small amount of Python-based repository tooling may survive M12; it must remain visibly tooling-only rather than becoming a disguised runtime fallback.

## Alternatives rejected

### Remove every Python-era version from the updater

Rejected. It would remove a qualified user-facing M11 feature without helping the current production runtime become Rust-only. Historical wheels already exist as immutable public artifacts.

### Keep `src/eggpool/` indefinitely as a rollback implementation

Rejected. Explicit installation of an immutable historical package does not require retaining a second current application tree. Keeping it would leave two implementations in the active repository and defeat M12's purpose.

### Delete every Python file from the repository

Rejected. Release/qualification tooling may still be useful and does not make the production runtime Python-dependent. M12 removes application/runtime ownership, not a programming language from all developer tooling.

### Remove `Requires-Python` immediately

Rejected for M12. The current native process does not need Python, but cross-era package-manager transitions do. Dropping the floor would weaken the already-qualified downgrade environment for little operational benefit.

### Publish or repair old Python releases during retirement

Rejected. Historical artifacts are already public and immutable; PyPI now rejects adding files to releases older than 14 days. M12 records and consumes their existing identities rather than mutating them.

## Implementation consequences

- P001 freezes the final Python reference/fixture boundary and classifies every relevant path.
- P002 applies this ADR to production package/catalog/update authority without removing application source.
- P003 removes the Python application/runtime source after proving Rust owns every required runtime asset and migration.
- P004 retires live-oracle/differential tests and reduces Python development tooling to the retained bounded set.
- P005 consolidates installer/release/docs/repository metadata around the Rust-only current tree while preserving explicit compatible historical version switching.
- P006 performs aggregate Rust-only qualification and is the only plan allowed to close M12.

A failed destructive gate creates a new corrective P-plan. Historical M11/M12 evidence is never rewritten to conceal the failure.
