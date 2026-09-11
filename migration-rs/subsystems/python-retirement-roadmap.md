# M12 Python Retirement Roadmap

Status: planning review complete 2026-09-11; P001 dependency-ready; no
production/runtime removal authorized yet

Planning baseline: `66faa89826e82ea9c4bcbaf5045776fdd6e1e0f3` (accepted M11
closure and current main)

Canonical sources: `../000-long-term-specification.md`,
`../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`,
`../003-planning-process.md`, ADR-0001 through ADR-0004, proposed ADR-0005,
and accepted M11 closure evidence.

## Purpose

M12 removes Python from the production/runtime and canonical release path after
the Rust cutover has stabilized. It preserves the useful historical evidence
and differential fixtures needed to explain and audit the migration without
retaining a supported second production implementation.

M12 is a packaging, ownership, and evidence-retirement milestone. It is not a
second Rust feature migration and must not change established runtime behavior
as a side effect of deleting the Python oracle.

## Planning-review conclusion

The milestone is eligible for planning because M11 is accepted and the public
Rust release is qualified. It is not ready for broad implementation as one
change because the repository has not yet separated these current roles:

| Surface | Current role | M12 review result |
|---|---|---|
| `src/eggpool/` | Python runtime and behavioral oracle | freeze provenance before removal |
| root `pyproject.toml` | Python package plus dev/test environment | requires an explicit tooling-manifest decision |
| `packaging/pypi/pyproject.toml` | Rust production wheel publication | ready to become sole publication authority |
| `scripts/` | release tooling, operational diagnostics, and Python/Rust qualification | classify; retain only non-runtime tooling |
| `tests/` and `tests/migration_rs/` | Python behavior tests and differential harness | retain Rust contract coverage; archive/remove obsolete oracle paths deliberately |
| K001/K005/K012 records | historical rollback/catalog evidence | preserve append-only; do not silently rewrite history |

Proposed ADR-0005 records the unresolved packaging/reference boundary. P001 is
safe to start because it inventories and freezes evidence without deleting or
changing production behavior. P002 and later remain blocked until P001 closes
and ADR-0005 is accepted or superseded.

## Invariants

1. The installed EggPool process is Rust-only; no Python import, interpreter,
   worker, or fallback is introduced.
2. Existing supported Rust config, database, API, CLI, dashboard, provider,
   routing, retry, lifecycle, and security contracts remain unchanged.
3. SQLite schema 54 and existing Rust-owned state remain readable and writable;
   M12 never resets or forks the database to simplify retirement.
4. Historical closure records remain append-only and continue to identify the
   final Python source/oracle commit used for M11 evidence.
5. Retained fixtures are bounded, secret-free, deterministic, and useful for
   Rust regression tests; they do not require a live Python server.
6. The public release path has one production manifest, one runtime
   implementation, and no unsupported source-build or Python fallback path.
7. Development-only Python tooling is clearly separated from the production
   artifact and is not required by an installed service.

## Exact versus semantic parity

M12 does not re-qualify a new implementation behavior. Existing M11 Rust
qualification remains the exact authority for the supported user-visible
   runtime surfaces. Retained reference fixtures preserve exact bytes or
   structured observations only where the earlier contract declared them
   meaningful. Historical Python-vs-Rust comparisons may use the previously
   approved normalization for ephemeral IDs, timestamps, ports, and temporary
   roots; no new normalization may hide semantic differences.

The retirement work itself is semantic: it proves that deleting the Python
runtime and rollback machinery does not alter the Rust package, service,
configuration/database ownership, or operator-facing failure contract.

## Ordered implementation slices

### P001 — Final Python reference boundary and fixture freeze

Primary class: invariant/infrastructure. Dependency-ready after this review.

Inventory every Python runtime, package, test, script, workflow, fixture, and
document dependency; record the final source identity; freeze the selected
machine-readable differential corpus; and classify each path as retain,
archive, replace, or remove. No production deletion occurs in P001.

### P002 — Production packaging and release-path retirement

Primary class: infrastructure/capability. Depends on accepted ADR-0005 and
P001. Make the Rust publication manifest, installer, updater, catalog, release
workflow, and public documentation agree that Rust is the only production
runtime. Decide and verify the final Python metadata/tooling boundary.

### P003 — Oracle and dual-run machinery retirement

Primary class: invariant/polish. Depends on P002. Remove or archive the
Python application tests, Python/Rust launchers, rollback-only workflows, and
runtime imports that P001 classified as migration-only. Preserve selected
fixtures and Rust-native contract tests with no source fallback.

### P004 — Rust-only M12 qualification and closure

Primary class: invariant/capability. Depends on P002 and P003. Run fresh
Rust-only package, install, update, restart, backup/recovery, database,
unsupported-target, security/redaction, and release-integrity checks. Record
the final closure and update the registry only if every exit condition passes.

## Non-goals

- no new provider, routing, wire, dashboard, database, or lifecycle feature;
- no dashboard redesign or frontend migration;
- no schema reset, schema fork, or data migration solely for Python removal;
- no removal of historical closure records, release manifests, hashes, or
  source-identity evidence;
- no automatic deletion of all Python-based development tools;
- no M13 planning or post-retirement feature work;
- no claim that M12 is closed before P004 evidence is accepted.

## Review gates and evidence

The planning review must be rechecked before each destructive slice:

- current baseline and source identity are recorded;
- Python oracle modules/tests and Rust replacements are named;
- exact/semantic parity and normalization are explicit;
- ADR-0005 is accepted before packaging ownership changes;
- hard dependencies and retained rollback implications are explicit;
- failure, cancellation, restart, contention, and durable-state effects are
  covered where a removed path previously owned them;
- narrow and broad verification commands are listed in each plan;
- closure evidence is externally meaningful and proves the production artifact,
  not merely a clean source tree.

## Exit condition

M12 closes only when the production repository/release path and installed
runtime are pure Rust; selected reference history and useful fixtures remain
auditable; no supported command or workflow silently invokes Python; Rust-only
package/update/deploy/recovery qualification passes; and no high/medium
packaging, compatibility, security, lifecycle, or data-loss finding remains.
