# Persistence M007 — Pi 5 Physical Qualification Closure

Status: closed

Source implementation plan:

- `plans/implementation/persistence/007-dedicated-checkpointer-qualification-experiment.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-007--dedicated-checkpointer-qualification-experiment`

Historical M007 assessments:

- `plans/closure/persistence/007-status.md` — pre-reactivation blocked assessment.
- `plans/closure/persistence/007-implementation-status.md` — implementation/local-gate disposition before physical Pi evidence.

Repository baseline reviewed: `79f678f5`

Implementation commits or pull requests:

- `6f0cfd53` — feature-only dedicated-checkpointer qualification implementation (existing M007 implementation).
- `a7ee7a4a` — allow the SBC runner to retain multi-second request observations.
- `6c068681` — read worker-close evidence from both server output streams and scope the experiment toggle to the server process.
- `ed3a01a7` — use the graceful stop operation before recovery.
- `79f678f5` — evaluate M007 gates using the runner's actual summary fields and report failed qualification gates as failure.
- `b6af7886` — isolate the unrelated Q007 loopback test lifecycle paths so the full tooling suite can run alongside a live EggPool process.

## 1. Executive finding

The physical experiment is complete and M007 is rejected. The candidate keeps foreground request latency below the plan's 500 ms maximum in the collected windows, and gate wait remains negligible, but it does not keep WAL growth bounded or make checkpoint progress across the 300-request steady-state windows. The dedicated worker's measured progress stalls after the first window; final checkpoint state does not catch the WAL end. The positive qualification criteria are therefore not met. No production topology change is authorized or made.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Physical target and MMC attestation | `artifacts/qualification/m007-pi5-2026-10-03/physical-attestation.md`; all runner reports | pass | Raspberry Pi 5 Model B Rev 1.0; Linux/aarch64; root/project filesystem is ext4 on MMC. |
| Same binary for three control and three candidate runs | `binary.sha256`; six phase reports | pass | SHA-256 `a22e0d4d…fd610d64`, built from `6f0cfd53`. |
| Three control 60-request runs | `m007-control-1.json` through `m007-control-3.json` | pass as controls | Max request latency 2,329–6,897 ms; max publication commit 2,308–6,845 ms. The control exposes the existing foreground WAL checkpoint tail. |
| Three candidate 60-request runs | `m007-candidate-1.json` through `m007-candidate-3.json` | fail qualification | p95 3–4 ms; max 8–17 ms; max publication commit 4.5–13.6 ms; foreground gate wait at most 1 μs. Each exceeded the 1,000-frame maximum (observed maxima 1,922–2,261); candidate 2 and 3 also recorded no PASSIVE progress in the measured batch. |
| Candidate 300-request convergence/lifecycle corpus | `m007-candidate-300.json` | fail qualification | Five 60-request windows completed; all request maxima were below 500 ms and requests/reservations converged to zero. Checkpoint progress did not advance in multiple windows; final checkpoint state was 2,107 log frames / 257 checkpointed, with 68,256,072 WAL bytes. WAL/checkpointer gates failed. |
| Backup, recovery, restart, graceful shutdown, close trace | candidate reports | pass in completed runs | The 300-request report records successful stop-before-recovery, isolated backup/recovery, restart, final graceful shutdown, and dedicated close marker. An intermediate repeat hit the runner's 30-second stop deadline once; the completed report reproduced the full lifecycle successfully. |
| Production/default topology unchanged | diff review | pass | Qualification feature remains opt-in; no production runtime or default-build topology change in this work. |

## 3. Production implementation evidence

No runtime implementation change was made during this qualification. The feature-enabled binary was built from the pinned M007 implementation commit. The runner was corrected so that it can record multi-second storage stalls, checks the actual stdout/stderr streams for the worker-close marker, applies the qualification toggle only to the tested server, uses graceful stop before recovery, and fails its report when M007 gates fail. Focused tooling tests cover those runner contracts.

## 4. Verification executed

### Commands run

Build and binary identity:

```bash
CARGO_BUILD_JOBS=2 cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-dedicated-checkpointer
sha256sum rust/target/release/eggpool
```

The build ran in a detached worktree at `6f0cfd532e753a43c454b370453850c55679aab5` on the Pi. The resulting checksum is stored in `artifacts/qualification/m007-pi5-2026-10-03/binary.sha256`.

Each corpus invocation used the same binary and this fixture/timeout:

```bash
uv run --frozen python scripts/qualification_sbc.py \
  --binary /tmp/eggpool-m007/rust/target/release/eggpool \
  --expected-sha256 a22e0d4d3962206651fd6f4b7dcc4e6528370a0816dba0ce153a4123fd610d64 \
  --config-fixture /home/sugarwookie/projects/eggpool/tests/tooling/fixtures/qualification/sbc-benchmark.toml \
  --timeout 240 --diagnose-publication-phases \
  --diagnose-dedicated-checkpointer <control|candidate> \
  --output artifacts/qualification/m007-pi5-2026-10-03/<report>.json
```

Three invocations used `control`; three used `candidate`. The convergence invocation additionally used `--diagnose-dedicated-checkpointer-steady-state` and wrote `m007-candidate-300.json`.

Tooling checks after runner corrections:

```bash
uv run --frozen ruff format --check scripts/ tests/tooling/
uv run --frozen ruff check scripts/ tests/tooling/
uv run --frozen pyright scripts/
uv run --frozen pytest tests/tooling/ -q --tb=short --maxfail=1
```

Results: all 58 files formatted; full Ruff passed; Pyright reported 0 errors/warnings; full tooling suite passed 157 tests with 3 skips in 40.39 s. The focused M007 runner suite passed 37 tests. The Pi build and workload reports are local physical-target evidence; no hosted CI result is claimed here.

## 5. Invariant review

- The regular/default build remains the one-connection, one-worker topology.
- The dedicated connection exists only in the explicit qualification build and enabled candidate mode.
- The tested feature did not alter schema, provider behavior, public API, or default configuration.
- Foreground gate waits were at most 2 μs in controls and 1 μs in candidate phases; observed multi-second tails were attributed to SQLite commit/checkpoint work rather than gate acquisition.
- Request/reservation state converged to zero after the completed 300-request corpus.

## 6. Failure and recovery review

The completed M007 300-request report shows backup, isolated recovery, restart reconciliation, and graceful shutdown passed. The dedicated connection close marker was observed with `success=true`. An intermediate full-corpus attempt exceeded the runner's stop deadline after recovery preparation; the runner was corrected to use the graceful stop command, after which one subsequent diagnostic and the final recorded run completed lifecycle checks. This does not alter the negative WAL-convergence result.

## 7. Migration and compatibility review

No migration or compatibility change. Production remains on its existing topology. The qualification feature remains an experiment and is not recommended for default deployment.

## 8. Security review

The corpus used synthetic loopback-only provider traffic and aggregate scalar reports. No credentials, prompts, raw request bodies, database paths, or provider responses were retained in the committed artifacts.

## 9. Documentation and operations

Seven scalar qualification reports and a physical attestation are retained under `artifacts/qualification/m007-pi5-2026-10-03/`. The report schema records board and storage class without retaining request content. The plan and registry now point to this current physical disposition; older M007 assessments remain historical.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Candidate PASSIVE checkpointing does not make progress across the five steady-state windows; WAL grows to 68,256,072 bytes, final reported log/checkpoint frames are 2,107/257, and 300-run progress-window and catch-up gates fail. | Dedicated topology does not provide bounded catch-up on this Pi 5/MMC and cannot be recommended. | Close M007 rejected; production remains unchanged. Any new storage/checkpoint design requires a separately bounded plan and evidence. |

## 11. Roadmap disposition

M007 is closed rejected, not a production adoption. The optional 512-frame follow-up is not authorized: the observed gap is stalled progress and WAL convergence, not a narrow foreground latency miss that a higher threshold is expected to resolve. No M007-dependent implementation plan was registered, so this closure unblocks none. At the time of this closure, routing-selection M002's separate evidence gate had been measured and its follow-up was under assessment. That assessment registered M003, which is now closed with a bounded exact-LRU implementation; see `plans/closure/routing-selection/003-status.md`.

## 12. Registry updates

The registry removes M007 from blocked and active implementation work, records the rejected Pi 5 qualification in Recently closed, and keeps the persistence roadmap active because the foreground checkpoint tail remains unresolved. No automatic successor or production change is registered.
