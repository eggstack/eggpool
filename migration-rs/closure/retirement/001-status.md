# P001 Closure — Final Python Reference Boundary and Fixture Freeze

Status: accepted/closed 2026-09-11

Plan: [P001 — Final Python reference boundary and fixture freeze](../../implementation/retirement/001-final-python-reference-boundary-and-fixture-freeze.md)

## Evidence

The final reference identity is recorded in
[`m12-reference-manifest.json`](../../fixtures/retirement/m12-reference-manifest.json):

- source commit: `c6b5d2c25038a8ac155c71f68fd50afea03fa459`;
- repository tree: `2887b6b6a3be38ad8b386781cda76f888d5ac0dc`;
- SQLite migration inventory: 54 migrations with the recorded checksum file;
- Rust-owned dashboard/config successors and retained schema fixtures are named;
- historical catalog identities and the schema-54-compatible exact target window are retained;
- full Python source remains recoverable from immutable Git history and is not duplicated.

The manifest is secret-free, contains no absolute paths or mutable branch
references, and separates current Rust ownership, historical external
artifacts, development tooling, migration evidence, and future removal targets.

## Verification

```text
rtk git rev-parse HEAD HEAD^{tree}
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run pytest tests/migration_rs/test_k001_catalog.py -q --tb=short --maxfail=1
rtk git diff --check
```

The captured source identity predates P002 changes by design. P002’s package,
catalog, and release-guard changes do not alter the frozen Python reference
identity.

## Handoff

P001 is accepted. P002 is promoted as the sole dependency-ready plan. P003
through P006 remain serially gated; no Python application deletion is
authorized by this closure.
