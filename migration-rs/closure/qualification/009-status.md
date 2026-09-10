# Q009 Closure — Sustained Failure, Reload, Streaming, and Resource Stability

Status: blocked; closure attempted 2026-09-09

Plan: [Q009 — sustained failure, reload, streaming, and resource-stability qualification](../../implementation/qualification/009-sustained-failure-reload-stream-resource-stability.md)

Implementation surfaces:

- [`scripts/qualification_stability.py`](../../../scripts/qualification_stability.py)
- [`q009-stability.toml`](../../../tests/migration_rs/fixtures/config/q009-stability.toml)
- [`test_q009_stability.py`](../../../tests/migration_rs/test_q009_stability.py)
- runtime-observation correction in [`qualification_sbc.py`](../../../scripts/qualification_sbc.py)

Machine-readable evidence: [`009-run.json`](009-run.json)

Implementation commit: `0989d6e46aa184f0251eeb59be32f1a811b0f4cc`

Evidence SHA-256: `557d8c1abfdc817704a1ead5731b9600f11ae012f4ab0a05c92c38e3a68cff35`

Candidate SHA-256: `33dcbcb842b988bdd7a40dd7f17c56709b43ee5c3323c4f138db0bde1b5a31f5`

## Outcome

The deterministic sustained-local qualification passed all six phases with the
fixed seed `9009`: warmup, steady mixed traffic, bounded faults, reload and
background churn, abrupt restart/recovery, and final convergence. The run took
11.4 seconds and used one warmup cycle, two steady cycles, one fault cycle, two
reload cycles, and one restart cycle. No live provider, credential, or paid
traffic was used.

Q009 is formally recorded as blocked rather than accepted because its hard
dependency Q008 is not accepted. Q008's physical evidence is complete, but its
own hard dependency Q007 remains blocked by the unresolved live-provider
interoperability evidence. This is an upstream planning dependency, not a Q009
stability finding.

## Topology and phase matrix

The runner uses isolated temporary config/database/runtime roots, two accounts,
three declared wire profiles, a model-router selector plus sticky virtual
route, a loopback fault provider, and the real Rust background supervisor.
Automatic backup, low-wear metrics, catalog refresh, retention/checkpoint
opportunities, rehash, and runtime-status sampling are exercised. Update
checking remains disabled to preserve the no-network qualification boundary;
O008 update behavior is covered by its accepted closure.

| Phase | Cycles | Result | Evidence |
|---|---:|---|---|
| Warmup | 1 | pass | finite Chat and streaming Responses; startup/readiness |
| Steady mixed requests | 2 | pass | Chat, Responses, Messages, concrete and virtual router |
| Bounded faults | 1 | pass | connect/read timeout, 408/429/5xx, alternate wire, model absence, malformed/partial stream, cancellation/write abort |
| Reload/background churn | 2 | pass | accepted and no-op rehashes; restart-required candidates rejected; task/generation drain |
| Restart/recovery | 1 | pass | SIGKILL with durable in-flight work; startup reconciliation did not replay provider work |
| Final convergence | — | pass | final finite/stream set and bounded drain |

The provider saw 35 submissions. The deterministic fault matrix recovered the
408, 429, 5xx, alternate-wire, and timeout cases as designed; partial,
disconnect, malformed, cancellation, and write-abort cases remained terminal
without crashing the server. The explicit model-absence fixture returned HTTP
404, and the unreachable provider returned HTTP 503 without process failure.

## Resource and ownership evidence

| Sample | RSS | FDs | Threads | Tasks | Generations | Active leases | Retiring | Finalization jobs | Pending | Active reservations | DB | WAL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before warmup | 31.2 MiB | 14 | 2 | 5 | 1 | 0 | 0 | 0 | 0 | 0 | 564 KiB | 93 KiB |
| after warmup | 32.3 MiB | 14 | 2 | 5 | 1 | 0 | 0 | 0 | 0 | 0 | 564 KiB | 419 KiB |
| after steady | 32.3 MiB | 14 | 2 | 5 | 1 | 0 | 0 | 0 | 0 | 0 | 564 KiB | 2.86 MiB |
| after faults | 33.9 MiB | 14 | 2 | 5 | 1 | 0 | 0 | 0 | 0 | 0 | 596 KiB | 3.97 MiB |
| after reload | 34.3 MiB | 14 | 2 | 5 | 2 | 0 | 1 | 0 | 0 | 0 | 596 KiB | 3.97 MiB |
| after restart | 31.3 MiB | 14 | 2 | 5 | 1 | 0 | 0 | 0 | 0 | 0 | 608 KiB | 3.97 MiB |
| final convergence | 32.3 MiB | 14 | 2 | 5 | 1 | 0 | 0 | 0 | 0 | 0 | 608 KiB | 3.97 MiB |

The one extra generation during the reload sample is expected staged-retirement
state and was gone by the next sample. Final durable state was 30 requests and
36 terminal attempts/reservations, with 24 completed, 5 error, and 1
interrupted request; pending requests and active reservations were zero.
Runtime evidence reported zero active leases, terminal references, retiring
generations, and finalization jobs at convergence. RSS is treated as bounded
characterization: the allocator's resident high-water behavior is not
reclassified as a logical leak. File descriptors and threads remained exactly
14 and 2 across all samples, and the sampler reported no infrastructure read
errors.

## Restart, cleanup, and evidence safety

The abrupt termination occurred after the delayed provider accepted one request.
After restart and reconciliation, the provider count remained unchanged, proving
that unknown in-flight provider work was not transparently replayed. The client
connection closed or was cancelled, durable state converged, and the runner
stopped the candidate in its `finally` path. Temporary roots are private and
removed by `TemporaryDirectory`; no backup/update temporary or lock leftovers
were retained. The JSON artifact is 14,010 bytes, contains bounded scalar and
structural observations, and retains no request body, provider response body,
credential, hostname, address, or child-process output.

## Defects and focused regressions

No Q009 implementation correctness, lifecycle, replay, crash, deadlock, or
resource-stability defect remains. The focused contract suite has 5 tests for
loopback safety, deterministic fault sequencing, placeholder safety, preflight
failure, and bounded cleanup semantics. The Q008-focused suite remains green
(6 tests in the combined Q008/Q009 invocation); the Q008 runtime sampler now
reads the current `active_generation`/`retiring_generations` diagnostics shape
and accepts the server API key as an explicit sampling parameter.

## Exact verification

```text
uv run ruff format scripts/qualification_sbc.py scripts/qualification_stability.py tests/migration_rs/test_q009_stability.py
uv run ruff check scripts/qualification_sbc.py scripts/qualification_stability.py tests/migration_rs/test_q008_sbc.py tests/migration_rs/test_q009_stability.py
uv run pyright scripts/qualification_sbc.py scripts/qualification_stability.py
uv run pytest tests/migration_rs/test_q008_sbc.py tests/migration_rs/test_q009_stability.py -q --tb=short --maxfail=1  # 11 passed
uv run python scripts/qualification_stability.py --binary rust/target/debug/eggpool --output migration-rs/closure/qualification/009-run.json  # pass
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # pass on rerun; first attempt had one unrelated timing flake, exact test and full suite then passed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 157 passed, 4 skipped
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
uv run ruff format --check src/ tests/ scripts/  # 751 files already formatted
uv run ruff check src/ tests/ scripts/  # pass
uv run pyright src/ scripts/  # 0 errors, 0 warnings, 0 informations
git diff --check  # pass
```

The local Q009 command and focused checks passed. The complete serial Rust
suite passed on rerun. Its first attempt exposed one existing timing-sensitive
failure in `operations_o002::client_timeout_is_bounded_and_handler_survives_disconnect`;
the exact test passed immediately when rerun, and the subsequent complete
all-target run passed. No Q009 change touched that Rust test or its code path.

## Findings and registry transition

| Finding | Severity | Disposition |
|---|---|---|
| Q009-F001 — accepted dependency Q008 is not formally accepted because Q007 remains blocked | blocker | planning dependency; no Q009 code change can resolve it |
| Q009-F002 — no local sustained-stability defect observed | informational | closed by the deterministic run and focused tests |

Q009 is formally closed as a blocked closure attempt, not accepted. Q008 is not
promoted, Q010 remains queued behind Q009, and no future plan is unblocked. Q007
remains the active blocked plan. A future Q007 acceptance must first promote
Q008; only accepted Q008 can promote Q009, and only accepted Q009 can promote
Q010. M11 remains blocked on accepted Q010 plus its separate planning review.

## Append-only re-acceptance — 2026-09-10

Q008 was re-accepted after Q011 closed the live-provider dependency. Q009's
deterministic six-phase run, resource convergence, restart/reconciliation
evidence, and focused regressions remain valid because Q011 changed no Rust
runtime or stability surface. Q009 is therefore re-accepted and promotes
Q010. The original blocked closure remains unchanged above.
