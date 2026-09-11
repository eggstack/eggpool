# ADR-0005 — M12 pure-Rust production boundary and Python reference retirement

Status: proposed for M12 planning review

Date: 2026-09-11

Decision scope: M12 Python production/runtime packaging, rollback metadata, and
retirement of migration-only dual-run machinery

## Context

M11 made the Rust binary wheel the canonical public EggPool runtime. The public
`eggpool` 0.8.0 release is a Maturin `bin` wheel with no Python application
dependencies, while the repository still contains the final Python runtime and
its root Hatchling project at version 0.7.4.

That Python surface currently has several different owners:

- `src/eggpool/` is the historical Python runtime/oracle;
- the root `pyproject.toml`, `uv.lock`, and Python tests provide its development
  environment;
- `scripts/` contains both production-release helpers and migration-only
  Python/Rust differential qualification tools;
- `packaging/pypi/pyproject.toml` is the current Rust publication manifest;
- the K001 catalog and K005/K012 qualification workflows still describe the
  Python-era rollback window.

Deleting these surfaces together would conflate production packaging, developer
tooling, historical evidence, and migration qualification. M12 therefore needs
an explicit ownership boundary before any removal.

## Proposed decision

1. `packaging/pypi/pyproject.toml` becomes the sole production publication
   manifest. The root Python project is not published as a current EggPool
   release.
2. The native Rust binary remains the only production/runtime implementation.
   M12 does not change API, CLI, configuration, database, dashboard, provider,
   routing, retry, or lifecycle behavior except where a packaging/removal change
   exposes an existing unsupported dependency.
3. The final Python runtime is removed from the active production tree after a
   machine-readable reference manifest and selected differential fixtures have
   been frozen. Its source remains recoverable from immutable repository history
   and the recorded source commit; it is not retained as a supported fallback.
4. Python may remain as explicitly development-only tooling where it is needed
   to build, inspect, or validate retained historical evidence. Such tooling
   must not be imported by the Rust binary, included in the production wheel,
   required by the installed service, or presented as a supported runtime.
5. The Python-era rollback window is retired as a supported update target only
   after a final Rust-only transition/recovery qualification. Historical Python
   release identities and prior M11 evidence remain append-only records.
6. The final `Requires-Python` metadata, root tooling manifest shape, catalog
   semantics, release workflow, installer/update behavior, and retained fixture
   set are M12 implementation decisions. They must be decided in bounded plans
   and must not be inferred by deletion.

## Alternatives rejected for planning

### Delete every Python file immediately

Rejected. It would destroy the reproducible oracle boundary before its useful
fixtures and provenance are frozen, and it would make the existing differential
closure evidence impossible to audit.

### Keep the Python application as a hidden production fallback

Rejected. M12's exit condition is a pure-Rust production/release path; a hidden
runtime fallback would preserve an unsupported second implementation and make
the public failure/update contract ambiguous.

### Keep the Python package as the public rollback channel indefinitely

Rejected. That preserves M11's temporary rollback contract rather than reaching
the M12 end state. Historical artifacts remain public history, but M12 no
longer promises a Python downgrade from the Rust updater.

### Convert the Rust wheel back into a Python wrapper

Rejected. M11 explicitly established a native binary wheel and M12 must remove,
not reintroduce, an application-runtime Python dependency.

## Required implementation consequences

- P001 must inventory and freeze the final reference boundary before destructive
  removal.
- A subsequent packaging plan must settle the root tooling manifest and
  `Requires-Python` decision, then update the K001 catalog and release guards.
- A subsequent cleanup plan must remove or archive migration-only Python/Rust
  dual-run paths and prove retained Rust qualification does not depend on
  `src/eggpool`.
- A final aggregate plan must qualify Rust-only fresh install, exact update,
  restart/recovery, database preservation, unsupported-target behavior, and
  release integrity.

No implementation plan may claim M12 complete until this ADR is accepted or
explicitly superseded by a replacement decision.
