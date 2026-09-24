# Plan 248 — EggServe Downstream Transport Closure Pass

Date: 2026-09-24
Status: complete
Closes:
- `plans/246-eggserve-0.2.2-downstream-transport-reentry-and-production-cutover.md`
- `plans/247-eggserve-transport-requalification-footprint-and-closure.md`
Implementation baseline: `d492eade677acd6fc932c9a0c487b744a3070a91`
Production implementation: `5cda27ca2e6678288e8b75d9dcb2964fe355b05f`
Expanded socket qualification: `9064eaa66ba6d98afcfe423cab63aae1c7186862`
Hosted CI: https://github.com/eggstack/eggpool/actions/runs/36009550026
Hosted dependency audit: https://github.com/eggstack/eggpool/actions/runs/36007084025

## Disposition

Plans 246 and 247 are **COMPLETE**. Plan 247's implementation-handoff state
was unblocked by the production cutover in Plan 246, then closed after the
behavioral, dependency, footprint, documentation, and hosted-CI evidence below
passed. In keeping with the append-only planning process, the original plan
documents remain unchanged; this closure pass is their final disposition.

The production path is:

```text
EggPool pre-bound TcpListener
  -> eggserve-server 0.2.1 direct HTTP/1 runtime
     -> eggserve-core 0.2.2 TowerToEggserve
        -> existing EggPool Axum Router and middleware
```

EggPool retains authentication, live generation-aware body admission,
coordinator/provider behavior, runtime generations, signal/control lifecycle,
and persistence. EggServe owns downstream HTTP/1 parsing, framing, connection
admission, and connection drain. The completion handle is passive and joined
before shared runtime and database teardown. The HTTP child drain is capped at
five seconds and is charged against EggPool's single ten-second foreground
shutdown deadline.

## Implementation and real-socket evidence

The production and qualification changes are in `rust/src/server/mod.rs`,
`rust/src/config.rs`, and `rust/tests/server_transport.rs`. The production
router and coordinator interfaces remain Axum-owned. The hard transport body
ceiling and Tower stream policy are 1 GiB; the live generation body limit
remains authoritative and is constrained to `0 < limit <= 1 GiB`.

`server_transport` passed five real-socket tests in both default and
no-default profiles. The tests cover:

- HTTP/1.0 and HTTP/1.1 health responses, HTTP/1.1 keep-alive reuse, and idle
  keep-alive closure during shutdown;
- unauthenticated protected-route rejection, successful `x-api-key` status
  access, and public static CSS with its response content type;
- oversized request targets and headers, excessive header count, incomplete
  framing, and a healthy subsequent request after each rejection;
- EggPool live-limit 413 responses for both Content-Length and chunked bodies,
  client disconnect during upload, listener health afterward, and zero tracked
  body tasks at shutdown;
- a successful finite Responses exchange through the production listener and
  a streaming Responses event delivered before the provider finishes;
- a stalled downstream stream reader: EggServe closes the downstream socket,
  cancellation reaches the provider, the child joins within the bounded drain,
  and the database closes only after child completion.

The stream fixture never reports a terminal provider event, so receiving the
first downstream delta proves that output is forwarded incrementally. Existing
focused wire/coordinator suites preserve the protocol-specific guarantees,
including native Responses byte preservation, translated terminal events,
unknown valid events, no post-handoff retry, cancellation, and exactly-once
finalization.

Focused suites passed:

| Target | Tests passed |
|---|---:|
| `server_transport` | 5 |
| `health` | 8 |
| `runtime_lifecycle_r005` | 10 |
| `status_command` | 13 |
| `coordinator_c008` | 29 |
| `coordinator_c009` | 13 |
| `coordinator_c011` | 13 |
| `coordinator_boundaries` | 17 |
| `coordinator_finalization` | 5 |
| `coordinator_publication` | 10 |
| `wire_stream` | 6 |
| `wire_runtime` | 18 |
| `wire_qualification` | 8 |
| `codex_responses_compat` | 16 |
| `codex_compaction_compat` | 15 |
| Config 1 GiB ceiling unit | 1 |

Additional lifecycle coverage ran through `runtime_lifecycle_r009` (5 tests),
the default full workspace suite, and the no-default full workspace suite.

## Dependency and footprint record

Both direct EggServe versions resolve exactly as planned:

- `eggserve-core =0.2.2`, with its `tower` and required `http-interop` bridge
  features;
- `eggserve-server =0.2.1`, with no TLS, HTTP/2, or HTTP/3 feature enabled.

EggServe's current core package topology resolves `eggserve-static` and
PHF-related crates transitively. EggPool does not use the static service. No
local bridge, EggServe TLS, H2/H3 capability, Eggfetch profile change, or
Eggress/SSH feature change was introduced. `cargo deny` and the hosted
dependency audit passed. Cargo tree recorded 377 to 386 lockfile packages
(+9) and 402 to 417 normal dependency nodes (+15). The existing direct Axum,
HTTP, Hyper, Tower, and body utility dependencies remain owned by EggPool's
application/provider paths.

Release footprint and loopback evidence was collected on the same Rust 1.98.1
`aarch64-apple-darwin` host, default features, and release profile, comparing
baseline `d492eade` with the implementation candidate:

| Measurement | Baseline | EggServe | Delta |
|---|---:|---:|---:|
| Release executable bytes | 27,250,640 | 27,956,016 | +705,376 (+2.59%) |
| Lockfile package count | 377 | 386 | +9 |
| Normal dependency nodes | 402 | 417 | +15 |
| Startup RSS | 14,925,824 B | 14,958,592 B | +32,768 B |
| Idle RSS | 14,925,824 B | 14,958,592 B | +32,768 B |
| Finite latency p50 / p95 | 1 / 2 ms | 1 / 2 ms | unchanged |
| Finite throughput | 805.416 req/s | 782.667 req/s | -2.82% |
| Streaming first-byte p50 | 1 ms | 1 ms | unchanged |
| Streaming first-byte p95 | 1 ms | 2 ms | +1 ms |
| Streaming throughput | 969.119 req/s | 950.664 req/s | -1.90% |
| Streaming bytes/s | 261,662.1 | 256,679.4 | -1.91% |

This is a single, development-host relative comparison, not physical SBC
evidence. The small latency/throughput changes are explained by the additional
downstream transport and do not meet the plans' material-regression trigger.
No physical Pi qualification is claimed or reopened. The transitive static
packaging edge is a possible upstream EggServe packaging improvement; its
observed binary and runtime cost is acceptable here and does not require a
downstream corrective plan.

## Verification record

Local full gates passed:

- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`;
- default and no-default workspace Clippy with `-D warnings`;
- no-default workspace check and test suite: 601 passed across 57 suites;
- default workspace test suite: 714 passed across 61 suites;
- locked debug and release builds;
- `cargo deny --manifest-path rust/Cargo.toml check`;
- dependency feature tree and duplicate-package audit;
- frozen Python environment, Ruff format/check, Pyright, and Pytest:
  108 passed, 1 skipped.

The final hosted CI run for qualification commit `9064eaa6` passed formatting,
default Clippy, no-default check and Clippy, the 714-test serial default
workspace suite, Ruff, Pyright, and Pytest. Hosted dependency audit run
`36007084025` passed for the unchanged dependency graph. The exact EggServe
versions are pinned in `rust/Cargo.toml` and `rust/Cargo.lock`.

## Downstream plan status

A plans-directory search found no later plan blocked on Plan 246 or this
transport line. Plan 247 was the sole dependent plan and is closed by this
pass. Plan 240 concerns passive SQLite checkpoints and is independent. No
additional EggServe corrective or requalification plan is required by the
observed evidence.
