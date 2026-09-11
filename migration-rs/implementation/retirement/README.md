# M12 Python Retirement Implementation Plans

Status: planning review complete; P001 dependency-ready; P002-P004 queued

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Proposed architecture decision: [ADR-0005 — M12 pure-Rust production boundary
and Python reference retirement](../../adrs/ADR-0005-m12-pure-rust-production-and-reference-retirement.md)

These plans remove Python from the production/runtime and canonical release
path while preserving bounded historical evidence and useful differential
fixtures. They do not authorize a Rust feature redesign or deletion of evidence
needed to audit M11.

## Sequence

1. [P001 — Final Python reference boundary and fixture freeze](001-final-python-reference-boundary-and-fixture-freeze.md) — **dependency-ready**.
2. P002 — Production packaging and release-path retirement — queued behind P001 and ADR-0005.
3. P003 — Oracle and dual-run machinery retirement — queued behind P002.
4. P004 — Rust-only M12 qualification and closure — queued behind P003.

Only `migration-rs/registry.md` authorizes implementation. P001 is the sole
M12 dependency-ready plan. No Python production/runtime removal is authorized
until P001's non-destructive boundary freeze closes and ADR-0005 is accepted or
superseded.

## Hard boundaries

- Rust remains the only installed production runtime.
- Existing Rust API, CLI, config, DB, dashboard, provider, routing, retry,
  lifecycle, and security behavior is preserved.
- Historical M11 closure records remain append-only.
- Python may remain only as explicitly development-only tooling after review;
  it may not enter the production wheel or installed service.
- Every destructive removal requires a preceding retained-evidence mapping.

## Closure discipline

Each accepted plan writes `migration-rs/closure/retirement/<NNN>-status.md` with
the source identity, requirement-to-evidence mapping, exact verification
commands, retained/archive inventory, and registry transition. Historical M11
records are not rewritten.
