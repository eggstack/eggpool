# Foundation Milestone F002 — Contract Inventory and Differential Oracle Harness

Status: closed; see [closure record](../../closure/foundation/002-status.md)

Repository baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

Source roadmap: `migration-rs/subsystems/foundation-roadmap.md#F002`

Long-term requirements: specification sections 2, 3, 10.

Applicable ADRs: ADR-0001, ADR-0002, ADR-0003.

Primary class: invariant/infrastructure

## 1. Objective

Build the reusable black-box compatibility harness and reviewed contract inventory that every later Rust milestone will use to prove parity rather than relying on source-code resemblance.

## 2. Readiness

Hard dependency: F001 is closed and provides a stable explicit Rust executable
path and test invocation convention. Python remains runnable and remains the
oracle.

## 3. Current oracle evidence

Relevant Python sources include `src/eggpool/cli.py`, `cli_full.py`, config models/validation, API routes, DB migrations/repositories, dashboard renderer/static resources, provider clients, and architecture/docs. Existing Python tests provide internal correctness evidence but do not yet constitute a cross-implementation comparator.

## 4. Invariants

- harness cannot accidentally invoke the same implementation twice;
- fixtures are deterministic and avoid real provider cost by default;
- observations retain enough detail to expose semantic differences;
- normalization removes only reviewed incidental differences;
- secrets/request contents prohibited from persisted diagnostics remain prohibited in captured artifacts;
- existing Python tests remain independent and runnable.

## 5. Scope

### In scope

- migration-specific test/helpers, preferably under `tests/migration_rs/` plus fixture data under a clearly named migration test-data directory;
- implementation launcher abstraction for Python and Rust executable/server paths;
- normalized observation schemas for CLI, HTTP/SSE, config validation, database effects, and rendered HTML/static hashes;
- deterministic temp config/data/state directories;
- local stub HTTP provider/server helper where needed;
- contract inventory document generated/maintained under `migration-rs/` or test data;
- explicit normalization-policy document/data;
- first representative oracle captures.

### Out of scope

Full parity corpus, live providers, browser automation framework, load testing, performance benchmarks, provider routing implementation.

## 6. Required production/test changes

The harness should be language-neutral at the observation boundary. Python test tooling may orchestrate both processes initially because Python is already the repository test environment; this is migration test infrastructure, not production coupling.

Each launcher must expose implementation identity in the observation metadata. Rust may return `unsupported/not implemented` for surfaces not yet ported, but tests must distinguish that from parity.

Normalize ephemeral ports, temp paths, timestamps, PIDs, framework-only headers, and other approved incidental fields only in targeted adapters. Do not recursively drop unknown JSON fields or broadly canonicalize HTML text.

## 7. Ordered work packages

### A — Contract inventory

Inventory documented CLI commands/options, config sections/defaults/aliases, HTTP routes/methods/auth, SQLite migrations/tables/checksums, dashboard routes/static files/themes, and per-account proxy forms.

Acceptance: every later roadmap area has a named oracle/source and parity class.

### B — Launcher and isolated environment

Implement Python/Rust invocation with separate executable identifiers, temp HOME/config/data/state/env, port allocation, subprocess timeout/cleanup, and deterministic timezone/hash settings where applicable.

Acceptance: harness proves it ran two distinct binaries/processes.

### C — Observation models

Capture CLI exit/stdout/stderr; HTTP status/selected headers/body or SSE frames; config result/error category; DB schema/selected row effects; HTML DOM-relevant facts and static hashes.

Acceptance: observations serialize to a reviewable deterministic form.

### D — Normalization policy

Create an explicit allowlist of incidental differences. Every rule includes rationale and test coverage that contractual differences remain visible.

### E — Seed fixtures

Add at minimum one CLI help/version fixture, one valid+invalid config fixture, one Python DB schema observation, one health/API observation, and one SSR/static observation from the Python oracle.

## 8. Failure/cancellation/restart semantics

Harness subprocesses must have bounded startup/command timeouts, reliable teardown, and diagnostics on failure. Test cancellation must not leave background Python/Rust servers running or persistent config/data outside temp directories.

## 9. Compatibility/migration

This plan changes no production compatibility surface. It defines how differences are judged.

## 10. Required tests

- distinct-implementation guard;
- timeout/teardown cleanup;
- normalization removes approved ephemeral values only;
- an intentionally changed contractual JSON field fails comparison;
- an intentionally changed CLI exit code fails comparison;
- HTML normalization does not erase text/DOM structure changes;
- temp filesystem isolation.

## 11. Verification commands

Run migration harness tests plus existing narrow Python smoke tests, then Rust fmt/clippy/test from F001. Avoid the full ~thousands-test Python suite unless shared helper changes justify it.

## 12. Documentation

Document how to add a differential case, how to approve a normalization, and which implementation is authoritative for each contract class.

## 13. Acceptance criteria

A later agent can add a Rust behavior, run one command/test family, and receive an actionable parity diff against Python. The harness cannot pass by dropping unknown fields or invoking the wrong implementation.

## 14. Stop conditions

Stop if parity requires deciding a material supported difference not covered by an ADR, or if the harness begins embedding production Rust/Python logic rather than observing it externally.

## 15. Closure evidence

Contract inventory, normalization list, launcher identity proof, seed fixture outputs, cleanup tests, and exact verification results.

## 16. Handoff notes

This is foundational correctness infrastructure, not a request to snapshot the entire Python suite. Keep fixtures small and high-value.
