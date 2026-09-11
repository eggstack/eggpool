# P001 — Final Python Reference Boundary and Fixture Freeze

Status: dependency-ready after M12 planning review 2026-09-11

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: invariant/infrastructure

Hard dependencies: accepted M11 closure; accepted ADR-0001 through ADR-0004

Interface dependency: proposed ADR-0005 is reviewed here but is not required to
inventory or freeze evidence. P002 cannot begin until ADR-0005 is accepted or
superseded.

## Objective

Create the final, reproducible map of the Python runtime/oracle and migration
qualification boundary before M12 removes any production/runtime packaging or
dual-run machinery. Freeze source identity and selected useful fixtures, then
classify every relevant path as retain, archive, replace, or remove. This plan
must not delete or alter the Python runtime, Rust runtime, package manifests, or
public update behavior.

## Authoritative Python oracle

The inventory must name the final source and test authorities, including at
minimum:

- `src/eggpool/` runtime modules, bundled config/assets, numbered SQLite
  migrations, and `src/eggpool/py.typed`;
- root `pyproject.toml`, `uv.lock`, `.python-version` if present, and the
  Python console-script/dependency metadata;
- Python unit/integration/contract/smoke tests and `tests/migration_rs/`;
- scripts that import `eggpool`, launch a Python server, inspect Python assets,
  or implement Python-era rollback/differential qualification;
- release/qualification workflows and public docs that still describe Python
  rollback or Python reference packaging;
- accepted M11/Q012 closure artifacts, manifests, hashes, and source commits.

The inventory must distinguish production/runtime ownership from development-only
tooling. It must identify a Rust-native replacement test or retained fixture for
every contract needed after Python removal; “covered by Rust” without a named
test or fixture is insufficient.

## Exact and semantic evidence contract

Record the final Python source commit and hashes for retained machine-readable
fixtures. Preserve exact observations only for fields already declared
contractual by M10/M11. Preserve semantic projections for behavior where the
earlier qualification contract allowed ephemeral normalization. The inventory
must list every normalization and may not add body, HTML, ordering, status,
terminal, durable-state, or error-category normalization to make a mismatch
disappear.

## Compatibility and migration effects

P001 has no user-visible runtime or packaging effect. It must explicitly prove
that its manifest and fixture capture do not alter:

- the Rust wheel or installed executable;
- config path resolution or environment-variable behavior;
- SQLite schema/migration/checksum assets;
- API, CLI, dashboard, provider, routing, retry, lifecycle, or security
  behavior;
- the public catalog, installer, updater, or release workflow.

## Failure, restart, and contention boundary

The inventory process must be deterministic and read-only with respect to
production state. Interrupted capture must leave the previous evidence intact;
partial output must not be accepted as the final manifest. No live provider
traffic, service restart, database mutation, or concurrent Python/Rust writer is
needed for this plan.

## Expected artifacts

- `migration-rs/fixtures/retirement/m12-reference-manifest.json` containing the
  final Python source identity, path classifications, retained fixture list,
  replacement test/fixture names, and approved normalization set;
- `migration-rs/closure/retirement/001-status.md` recording the inventory,
  hashes, commands, and unresolved decisions;
- an updated registry entry that keeps P002 blocked until ADR-0005 and P001
  closure are accepted.

The manifest must contain no credentials, raw provider bodies, local absolute
paths, environment dumps, or mutable branch references.

## Verification

At minimum, run and record:

```bash
rtk git status --short --branch
rtk git rev-parse HEAD
rtk git ls-files src/eggpool tests scripts packaging .github/workflows
rtk rg -n "from eggpool|import eggpool|PYTHONPATH|python-wheel|differential|oracle" src tests scripts .github docs migration-rs
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk git diff --check
```

The plan is accepted only when the manifest is complete, deterministic, secret
free, and reviewed against the Python oracle and Rust replacement inventory.

## Non-goals

- no source/package deletion;
- no root `pyproject.toml` or `uv.lock` redesign;
- no release/update/catalog change;
- no test-suite pruning;
- no Rust feature or parity correction unless P001 discovers a documented
  inventory contradiction, in which case stop and create a corrective plan;
- no M12 closure claim.

## Handoff result

Accepted P001 promotes P002 only after ADR-0005 is accepted or superseded.
P001 itself does not make M12 complete and does not authorize Python removal.
