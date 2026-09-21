# Plan 238 — Narrow Publication/Storage Diagnostic

Date: 2026-09-21
Status: draft
Follows: `plans/237-raspberry-pi-finite-tail-diagnostic-pass.md`
Priority: P2 narrow diagnostic
Execution target: physical Linux/aarch64 Raspberry Pi-class SBC

## Purpose

Follow up Plan 237 Outcome 1 (durable publication / SQLite / storage path)
with a narrow diagnostic that inspects finite-request durable publication
cost boundaries before any implementation is considered.

Plan 237 showed, on Pi 5 with MMC storage, that the slowest finite request
in all three 60-sample diagnostic runs was pre-provider dominated
(3467/2006/1778 ms) with provider-service max 0–1 ms and a stable
direct-provider control (max 1–2 ms), while one RAM-backed temporary-root
run contracted the same batch to max 4 ms. No runtime change was made.

## Governing constraints

1. No production behavior change in this plan: no second SQLite connection,
   no `synchronous=OFF`, no durability weakening, no Tokio/routing/streaming
   change, no Cargo dependency/profile change, no config-default change.
2. Diagnostics stay bounded and scalar-only. Never retain prompts,
   request/response bodies, credentials, headers, or URLs.
3. Inspect transaction count and durability boundaries first. A connection-count
   change may only be proposed by a later implementation plan with comparable
   loopback evidence, never assumed here.

## Authority paths

- `rust/src/coordinator/publication.rs`
- `rust/src/coordinator/finalization.rs`
- `rust/src/coordinator/finite.rs`
- `rust/src/db/connection.rs`
- `scripts/qualification_sbc.py`
- `tests/tooling/fixtures/qualification/sbc-benchmark.toml`
- `architecture/deep-dive-database.md`
- `architecture/deep-dive-request-lifecycle.md`
- `artifacts/qualification/237-sbc-finite-tail-diagnostic.json`

The single serialized WAL connection and durable publication/finalization
ownership stay in place while they are measured.

## Diagnostic questions

1. How many SQLite transactions (and which durability boundaries) does one
   native finite request incur from admission through finalization?
2. Which boundary dominates the pre-provider phase on MMC storage, and does
   it contract on tmpfs under otherwise identical settings?
3. Does the intermittent post-provider second-slowest from Plan 237 runs 1–2
   repeat, and on which side of the provider boundary does it fall?

## Workstreams (sketch for implementation handoff)

- Reuse `scripts/qualification_sbc.py` and the Plan 236 benchmark fixture;
  no new benchmark framework.
- Count durable boundaries per finite request from existing code paths
  (publication insert, attempt/usage updates, finalization, WAL checkpoint
  behavior) as scalar facts; add bounded timing only if it stays
  scalar-only and off by default.
- Compare MMC vs tmpfs temporary roots with identical settings
  (WAL on, `synchronous = "NORMAL"`, one database worker, same cadence,
  same fixture, same binary, same corpus).
- Record p50/p95/max per boundary, slowest-five scalars, direct-provider
  control, convergence state, and one explicit owner classification.
- Stop after localization. Any implementation (batching, boundary reduction,
  or connection change) belongs to a later plan with its own evidence gate.

## Completion criteria

- [ ] per-request durable-boundary counts recorded as scalar facts;
- [ ] MMC vs tmpfs comparison with identical settings;
- [ ] direct-provider control remains stable or is recorded as noisy;
- [ ] all runs converge ownership state;
- [ ] one explicit owner classification recorded;
- [ ] no production runtime/API/config/dependency behavior change.
