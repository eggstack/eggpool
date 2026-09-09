# Q011 — Q007 Live-Provider Corrective Closure

Status: blocked; queued behind maintainer-authorized live-provider access

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Corrects: blocked Q007 and blocked Q010 closure

Primary class: invariant/polish

Hard dependency: accepted Q006; Q007's deterministic harness and prior
regressions remain the starting point.

Operational dependency: a maintainer-provided, test-only live-provider
credential and a provider edge that permits the bounded Q001 request matrix.
Credentials must remain outside the repository and all evidence must remain
secret-free.

## Objective

Complete the mandatory Q007 real-provider evidence that Q010 could not accept.
The existing Q007 implementation, offline seven-cell matrix, request budget,
redaction checks, and deterministic regressions are retained. This corrective
plan owns only the missing live interoperability evidence and any narrowly
scoped provider/wire regression it exposes.

## Current finding

The prior authorized OpenCode Go attempt resolved all three planned model
identifiers but received HTTP 403 from the provider edge on its first
Responses request. The run stopped within budget, and the remaining live
finite, streaming, and cross-surface cells were not claimed. This is not
reclassified as a successful provider interaction.

## Required work

1. Confirm a test-only credential and provider endpoint are intentionally
   authorized for the existing seven-request Q007 matrix.
2. Run the live command from Q007 without increasing its fixed budget or
   adding an external retry loop.
3. Capture only the bounded Q001 evidence fields: safe provider/model/surface
   identifiers, status classes, request IDs where safe, usage presence, stream
   terminal evidence, and durable convergence.
4. If the provider edge still rejects the request, obtain an explicitly
   reviewed provider-transport decision or a different authorized
   structurally distinct provider. Do not weaken the Q001 live requirement or
   turn the loopback run into a live pass.
5. Add a deterministic regression for every EggPool defect discovered by a
   permitted live run, then rerun Q002 and the affected focused suites.

## Non-goals

Q011 does not certify providers generally, run load or failure-abuse tests,
store credentials, change the public installer/release/update authority,
promote Q008-Q010, or perform M11 cutover work.

## Verification and closure

Run the existing Q007 offline tests and loopback matrix first. With
maintainer authorization, run the bounded live command from Q007, then run:

```text
uv run pytest tests/migration_rs/test_q007_live_provider.py -q --tb=short --maxfail=1
uv run python scripts/qualification_runner.py --skip-build
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Write `migration-rs/closure/qualification/011-status.md` as an append-only
corrective record. Q011 may promote Q008 only after every mandatory Q001 live
cell has real evidence and no high/medium live interoperability finding
remains. Q008, Q009, and Q010 must then be re-accepted in dependency order;
their prior blocked records are not rewritten.
