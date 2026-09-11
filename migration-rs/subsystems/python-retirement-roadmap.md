# M12 Python Retirement Roadmap

Status: closed after accepted P007; P001-P006 remain accepted historical evidence

Planning baseline: `385cc2355e84db6071ab35e81b14f55e344afd77` (M11 closed; provisional M12 boundary planning)

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, `../003-planning-process.md`, accepted ADR-0001 through ADR-0005, closed M4-M11 roadmaps, and accepted M11 closure evidence.

Research notes: `../python-retirement-planning-notes.md`.

## Purpose

M12 removes the historical Python **application** from the current production/runtime and active repository path after the Rust cutover. It preserves the useful evidence needed to audit the migration and preserves the user-facing ability to select compatible immutable historical releases explicitly through package-manager ownership.

M12 is an ownership/evidence retirement milestone. It is not another Rust feature migration. Current runtime semantics are already qualified by M4-M11 and must not be changed merely to make deletion easier.

## Key research conclusions

1. Public historical Python wheels are immutable external evidence. PyPI now rejects adding files to releases older than 14 days and never permits filename reuse, so M12 must reference existing identities rather than plan historical republishing.
2. PyPI remains appropriate for the native Rust program. Wheel `.dist-info`/`RECORD` metadata defines package-manager ownership independently of whether the payload is an importable Python application.
3. The M11 cross-era transition engine is useful and already qualified. A pure-Rust **current** runtime does not require deleting the ability to request an old compatible version explicitly.
4. `Requires-Python >=3.11` should remain during M12 because it keeps package-manager environments capable of compatible historical Python transitions; it does not cause the native Rust process to invoke Python.
5. Full Python source need not be copied into a legacy directory. Immutable Git history plus a bounded reference manifest/selected fixtures gives better provenance with less active-tree maintenance.
6. Python may remain for developer tooling, but application imports, console scripts, publishable Python EggPool metadata and live dual-run oracle dependencies must disappear.

## Ownership target

### Current production/runtime

Owned entirely by:

- `rust/` application/runtime;
- `packaging/pypi/` current PyPI publication manifest;
- Rust-owned runtime assets/migrations/config defaults;
- existing deployment/update/release workflows operating on the Rust artifact set.

### Historical compatibility

Owned by:

- immutable PyPI/GitHub release artifacts and hashes;
- the K001 installable-release catalog;
- K004/K005 package-manager transition logic;
- accepted DB/config compatibility evidence;
- P001 retained reference fixtures/provenance.

An explicit exact historical Python target is not a current runtime fallback. `latest` and automatic default resolution remain Rust-only.

### Development tooling

Python may survive only where it is clearly a repository tool: release/catalog validators, bounded fixture processors, or similar utilities. No retained Python code may provide a current `eggpool` application/runtime package.

## M12 invariants

1. Current installed EggPool execution is Rust-only; normal serve/inference/operator paths do not start/import the historical application.
2. Current/future release publication uses `packaging/pypi/pyproject.toml` and the native Rust binary wheel only.
3. Existing config/database/API/CLI/dashboard/provider/routing/retry/lifecycle behavior remains unchanged by retirement.
4. SQLite schema 54, migration ordering/checksums and canonical state paths remain unchanged.
5. Historical closure records/source identities remain append-only/auditable.
6. Retained fixtures are deterministic, bounded, secret-free and do not require a live Python EggPool server.
7. Compatible historical exact-version transitions remain available to package-manager-owned installs; incompatible state fails before mutation.
8. `eggpool update` latest/default cannot choose a Python-era release.
9. Standalone Rust raw installs cannot transition directly to Python.
10. No full duplicate Python application archive remains in the active tree; full source is referenced by immutable Git identity.
11. Retained Python development tooling is not shipped as the current application and is not required by service startup.
12. No historical PyPI release is rebuilt, modified, yanked or deleted to simplify M12.
13. No Rust sdist/source-build fallback is introduced for unqualified targets.
14. Source deletion occurs only after P001/P002 prove the replacement authority.
15. Live oracle/test deletion occurs only after P001/P004 prove fixture/Rust replacement coverage.
16. Failed destructive or closure gates create new corrective P-plans.
17. Final closure evidence must include a green hosted CI result when hosted CI exposes a qualification failure not represented by local P006 evidence.

## Ordered implementation sequence

```text
M11 K012/K013/K014 accepted; Rust 0.8.0 canonical
  |
  v
P001 final Python reference/fixture/disposition freeze
 -> P002 Rust production package/catalog/cross-era authority
 -> P003 Python application source + runtime-asset retirement
 -> P004 oracle/differential/test + Python tooling retirement
 -> P005 repository/installer/release/docs consolidation
 -> P006 Rust-only qualification + historical M12 closure
 -> P007 provider-transport fixture determinism + hosted-CI requalification
```

Only `../registry.md` authorizes implementation. P001-P006 remain accepted and append-only. A hosted-CI failure discovered after P006 invalidated P006 as the current final closure authority without erasing what its local qualification proved. P007 is accepted/closed and is the current M12 closure authority.

## P001 — Reference boundary and fixture freeze

Inventory the final Python application, tests, tooling, assets, migrations, public historical package identities and Rust replacement coverage. Create the machine-readable M12 reference manifest. No production behavior changes.

Exit: every destructive target has a disposition and named surviving authority.

## P002 — Production package/catalog/cross-era authority

Apply ADR-0005 before source deletion. Make the Rust publication manifest uniquely current, prevent root Python publication, preserve package-manager exact historical transitions under compatibility checks, retain the Python compatibility floor, and prove latest/default behavior is Rust-only.

Exit: package/update/release authority is coherent without deleting the oracle yet.

## P003 — Python application source/runtime assets

Delete the historical Python application only after migrations, defaults, templates/static assets and other runtime material have independent Rust/fixture ownership. Do not copy the full app elsewhere.

Exit: Rust builds/packages/runs with `src/eggpool` absent and runtime assets intact. **Accepted/closed by P003.**

## P004 — Oracle/test/tooling retirement

Replace live Python/Rust comparisons with retained fixtures or Rust-native contract tests, remove migration-only server launchers/application tests, and shrink Python to clearly development-only tooling.

Exit: no CI/test path needs the historical application source or a live Python EggPool server. **Accepted/closed by P004.**

## P005 — Repository/release/installer/docs consolidation

Remove stale Python-current assumptions from the installer, updater, release validators/workflow, repository metadata and documentation. Preserve explicit compatible historical exact-version selection and existing-install adoption.

Exit: the repository and public docs have one coherent current Rust authority. **Accepted/closed by P005.**

## P006 — Rust-only aggregate qualification

Build/install the supported production artifacts from the post-retirement tree; qualify state/recovery/security/release behavior and a package-managed historical Python -> post-retirement Rust -> Python -> Rust cycle; close M12 only if no high/medium finding remains.

P006 is **accepted historical closure evidence**. Its local qualification remains valid for the surfaces it actually exercised. It is no longer the current final closure authority because hosted GitHub CI subsequently failed the provider-transport account-isolation test on the P005/P006 closing trees.

## P007 — Provider transport fixture determinism and M12 requalification

Root-cause the hosted-CI `ReadTimeout` in the identical-proxy account-isolation regression, correct the fixture or smallest production defect without loosening production timeout policy, repeatedly qualify both account-isolation paths, rerun the complete Rust/retirement gates, and require a successful hosted GitHub Actions CI run before re-closing M12.

Exit: provider/account isolation is deterministic locally and in hosted CI, all broad qualification gates are green, and no unresolved high/medium finding remains. **Accepted/closed by P007; P007 supersedes P006 only as the current M12 closure authority, while P006 remains append-only historical evidence.**

## Qualification posture

M12 should reuse accepted M10/M11/P006 evidence when source-freshness is valid, but any gate whose owner/path changes during P002-P007 must be rerun. P007 specifically owns fresh provider-transport and hosted-CI evidence because that is the surface that invalidated the final P006 closure claim.

No broad new target matrix or live-provider campaign is required unless the corrective change touches those surfaces. Linux x86_64 remains the minimum manager-transition closure host; artifact builds still inherit the three M11 targets when packaging evidence must be rerun.

## Non-goals

- no provider/routing/wire/dashboard/lifecycle feature work;
- no DB schema reset/fork;
- no dashboard redesign;
- no target expansion;
- no requirement to remove Python as a developer tooling language;
- no deletion/yank/rebuild of historical public packages;
- no replacement package name;
- no M13 migration milestone;
- no broad timeout inflation or flaky-test retries to manufacture green CI.

## M12 closure

P006 remains an accepted historical record. Current final closure was reopened by the post-P006 hosted-CI failure and is restored by accepted P007.

The accepted P007 closure record is `closure/retirement/007-status.md`; it records the root-cause classification plus the successful hosted CI run for the corrected tree. M12 is closed and the migration program has no dependency-ready implementation plan.

After accepted P007 re-closure, further EggPool work returns to normal product/maintenance roadmaps rather than continuing the migration milestone series. No M13 plan is auto-created.
