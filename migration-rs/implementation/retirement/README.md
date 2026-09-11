# M12 Python Retirement Implementation Plans

Status: corrective requalification open; P001-P006 accepted historical evidence; P007 dependency-ready

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Architecture decision: [ADR-0005 — M12 pure-Rust production boundary with immutable historical-version compatibility](../../adrs/ADR-0005-m12-pure-rust-production-and-reference-retirement.md)

Research notes: [`migration-rs/python-retirement-planning-notes.md`](../../python-retirement-planning-notes.md)

These plans retire the historical Python application from the **current** production/runtime/repository path while preserving immutable historical package artifacts, useful reference fixtures, and compatible explicit exact-version transitions.

## Sequence

1. [P001 — Final Python reference boundary and fixture freeze](001-final-python-reference-boundary-and-fixture-freeze.md) — **accepted/closed**.
2. [P002 — Rust production package, catalog, and cross-era authority](002-rust-production-package-catalog-and-cross-era-authority.md) — **accepted/closed**.
3. [P003 — Python application source and runtime-asset retirement](003-python-application-source-and-runtime-asset-retirement.md) — **accepted/closed**.
4. [P004 — Oracle, differential, test, and Python tooling retirement](004-oracle-differential-test-and-python-tooling-retirement.md) — **accepted/closed**.
5. [P005 — Repository, installer, release, and documentation consolidation](005-repository-installer-release-and-documentation-consolidation.md) — **accepted/closed**.
6. [P006 — Rust-only qualification and M12 closure](006-rust-only-qualification-and-m12-closure.md) — **accepted historical closure evidence**.
7. [P007 — Provider transport fixture determinism and M12 requalification](007-provider-transport-fixture-determinism-and-m12-requalification.md) — **dependency-ready corrective plan**.

Only `migration-rs/registry.md` authorizes implementation. Hosted CI exposed a provider-transport account-isolation failure after P006, so P006 remains append-only evidence but is no longer the current final closure authority. P007 is the sole dependency-ready M12 plan.

## Hard boundaries

- Current/future production is Rust-only after retirement.
- Historical Python PyPI releases are not deleted, rebuilt, yanked, or copied wholesale into the repository.
- Compatible historical exact-version transitions remain supported through package-manager ownership; latest/default behavior remains Rust-only.
- `Requires-Python >=3.11` is retained during M12 as a cross-era package-management compatibility floor, not a runtime interpreter requirement.
- No SQLite schema/reset/fork is introduced for retirement.
- No provider/routing/wire/dashboard/lifecycle feature redesign is folded into cleanup.
- Full Python source is recoverable from immutable Git history; only bounded useful fixtures remain active.
- Python may remain as development tooling, but no retained tool may constitute a hidden EggPool runtime fallback.
- Failed closure creates a new corrective P-plan; accepted historical evidence is never rewritten.
- P007 must not manufacture green CI by inflating production transport timeouts, sleeping/retrying assertions, ignoring the test, or weakening account-isolation expectations.

## Closure discipline

Each accepted plan writes `migration-rs/closure/retirement/<NNN>-status.md` with implementation commit(s), exact verification, retained/removed evidence, unresolved findings, and the registry transition it authorizes.

`006-status.md` remains historical accepted evidence for the local P006 qualification. Only accepted `007-status.md`, including a successful hosted CI run on the corrected tree, may restore current M12 closure authority after the post-P006 CI failure.
