# ADR-0001 — Side-by-Side Rust Migration with Python as Behavioral Oracle

Status: accepted

## Context

EggPool is a mature Python implementation with a large compatibility and correctness surface: TOML configuration, a broad CLI, SQLite durability, multiple client protocols, provider-specific wire behavior, routing, retries/finalization, and a finished SSR dashboard.

Rewriting modules in place would remove the best source of behavioral evidence exactly when it is most needed. A hybrid production process would also create a new architecture that must itself be debugged and later removed.

## Alternatives considered

1. Rewrite Python modules in place, replacing them progressively with Rust.
2. Add Rust extensions/PyO3 modules beneath the Python application.
3. Build a separate Rust implementation in another repository.
4. Build a side-by-side pure Rust implementation in the existing repository and qualify it against Python.

## Decision

Use option 4.

Production Rust source lives under `rust/`. The existing Python source remains live under its current paths until cutover. Migration planning/evidence lives under `migration-rs/`.

The Python implementation is the default behavioral oracle for unspecified observable behavior. Independently documented EggPool contracts outrank accidental Python behavior.

Differential tests invoke both implementations by explicit path. The Rust build must not replace the installed Python command during migration.

## Consequences

Benefits:

- every Rust slice can be checked against a live known implementation;
- rollback remains possible during development;
- the migration does not force an interim hybrid runtime;
- config/database/frontend assets can be tested from one checkout;
- implementation mistakes are easier to separate from intentional design changes.

Costs:

- temporary duplicate implementation code exists;
- contract fixtures/harness work must be built early;
- some Python quirks must be classified as contractual vs incidental rather than copied blindly.

## Compatibility implications

No existing config, DB, client API, or dashboard workflow changes solely because Rust work begins.

Any intentionally supported difference requires a separate accepted ADR or explicit cutover decision with differential evidence.

## Supersession

This ADR may be superseded only if the repository can no longer safely host both implementations or a different migration topology provides equal or stronger oracle/rollback guarantees.
