# On-Prem Concurrency Performance M002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-concurrency-performance/002-ordinary-server-team-baseline.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-concurrency-performance-roadmap.md#milestone-002--ordinary-server-small-team-baseline`

Repository baseline reviewed: `cdfb6fa8d4aaa1d9d5b43af8c87eaac68f83c402`

Implementation commits or pull requests:

- `cdfb6fa8d4aaa1d9d5b43af8c87eaac68f83c402` — ordinary-host gates, frozen matrix driver, summary validation, and first deployment guidance.
- `f7343eb295ef3bb9e72b43211980934fe2ca2b45` — committed managed lifecycle and protected SQLite-diagnostic tooling; measured deployment guidance and observability updates.
- Closure/status pass — completed 27-run host matrix, bounded competing-I/O and SQLite publication diagnostics, and formal plan/registry updates.

## 1. Executive finding

M002 is complete. The locked release candidate was measured on a bare-metal
Ubuntu 24.04.5 host with ext4 on Samsung SSD 980 NVMe. All 27 fresh-root
Q008 runs across nine cells passed; the same candidate passed managed Q009
fault, hard-budget, user-disable, and abrupt-restart checks. A bounded
same-filesystem competing-writer run and an opt-in SQLite gate diagnostic add
contention and storage-phase evidence. The deployment guide now reports a
measured host-specific reference and explicitly rejects an SLA interpretation.

The result describes this synthetic loopback workload on one host. It does not
promise throughput for other hardware, providers, models, request shapes, or
policy settings. No production runtime, schema, database topology, or policy
behavior changed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Separate named accounts from simultaneous requests | `matrix.json`, 27 Q008 reports | pass | Principal cells 1/5/10/25/49 at concurrency 1; concurrency cells 1/2/4/8/16 at five principals; 1,000 requests and three fresh roots per cell. |
| Freeze candidate, workload and gates before collection | `matrix.json`; summary guards in `tests/tooling/test_qualification_ordinary_server.py` | pass | Candidate SHA-256 `43cdf634d1ffcd6759ad14adca072869101b379f074e8001b4a28cf52f10bce3`, source commit `1c3d15b8911f1f0f3e003c9e918282c0363736e3`; maximum full I/O PSI avg10 before/after any of the 27 raw runs was 13.96%, below the 25% stop gate. |
| Record durable ordinary-host measurements | `summary.json`, `u*-c*-run-*.json` | pass | Ubuntu 24.04.5, kernel 6.8.0-147-generic, Intel i9-9900K, 16 logical CPUs, 15.5 GiB RAM, bare metal, ext4, Samsung SSD 980 1TB NVMe; WAL/NORMAL and one worker. |
| Require p99 sample size and settled durable state | 27 raw Q008 reports and generated `summary.json` | pass | Each run completed 1,000 concurrent requests with p99; no HTTP failures, pending requests, or active reservations. Three repetitions per each of nine cells. |
| Exercise failure, cancellation, retry/partial response, budgets, disable and restart | `lifecycle.json`; raw-to-summary guards | pass | Provider 408/429/503/timeout, partial/disconnect streams, client cancel, alternate wire, malformed response, absent model and connect failure. Provider, user-budget and global-budget 429s are counted independently (one each). User disable revoked a second synthetic member's credential during an in-flight request; the next request received 401 before provider dispatch. Abrupt restart did not replay unknown work; recovery converged to zero pending requests/reservations. |
| Exercise disk contention and record SQLite gate phases | `disk-contention.json`; `sqlite-gate-diagnostic-session.json` | pass | The 1,000-request four-way run used a bounded same-filesystem writer capped at 4 MiB/s and 32 MiB; it wrote 23.2 MiB, completed all requests, and settled all reservations. Its p95/p99 were 85/125 ms, and full I/O PSI avg10 after was 6.06%. The separate 60-request feature-build publication diagnostic measured gate wait p50/p95/max at 0/1/1 μs for both publication and finalization. These diagnostic samples are supplementary, not per-cell or universal claims. |
| Publish an honest measured envelope and retain Pi separation | `docs/deployment.md`, `docs/raspberry-pi.md`, this record | pass | Nine-cell latency/throughput/resource summaries are documented for this host and synthetic profile; the Pi microSD solo reference remains separate and unchanged. |
| Preserve runtime, persistence, policy and compatibility invariants | changed-file review; focused Rust and tooling tests | pass | No runtime or schema change. WAL/NORMAL, the single SQLite gate/worker, and admission/budget semantics are unchanged. |

### Measured matrix summary

Median throughput and latency across three repetitions per cell:

| Named principals | Concurrency | Throughput (req/s) | p95 (ms) | p99 (ms) | Max observed (ms) | Peak RSS (MiB) |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 36.5 | 82 | 148 | 232 | 24.9 |
| 5 | 1 | 42.2 | 61 | 89 | 243 | 44.2 |
| 5 | 2 | 94.0 | 61 | 79 | 96 | 44.3 |
| 5 | 4 | 193.7 | 59 | 75 | 100 | 44.6 |
| 5 | 8 | 335.3 | 69 | 76 | 1,088 | 45.0 |
| 5 | 16 | 341.3 | 88 | 106 | 1,119 | 45.8 |
| 10 | 1 | 42.9 | 61 | 77 | 160 | 44.2 |
| 25 | 1 | 42.5 | 61 | 81 | 228 | 44.6 |
| 49 | 1 | 42.3 | 61 | 81 | 195 | 45.1 |

The 8/16 concurrency maxima show occasional long tails despite moderate p95/p99;
they are retained rather than hidden by medians. This is a descriptive result,
not a latency threshold or service guarantee.

## 3. Production implementation evidence

No production behavior changed. The ordinary-server qualification profile
preserves the SBC runner's default and records sanitized host/build/storage
facts. The matrix uses a fixed 0/10/50 ms loopback provider profile, native
finite, native streaming, and translated streaming samples, hard user/global
request budgets, and fresh roots. The optional diagnostic build adds only
feature-gated scalar SQLite observations; it is not the measured release
candidate and did not alter the product binary.

The committed artifact corpus is in
`artifacts/qualification/on-prem-concurrency-performance/ordinary-server/`.
It includes all 27 passing raw reports, `matrix.json`, generated `summary.json`,
`lifecycle.json`, the controlled `disk-contention.json`, the supplementary
SQLite gate report, and preserved failed attempts. A secret-pattern scan found
no credential, token, prompt, authorization, or cookie material in the corpus.

## 4. Verification executed

### Commands run

```bash
rtk cargo build --manifest-path rust/Cargo.toml --locked --release
rtk uv run python scripts/qualification_ordinary_server.py --binary rust/target/release/eggpool --output artifacts/qualification/on-prem-concurrency-performance/ordinary-server
rtk uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --target-class ordinary-server --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --concurrency-workers 4 --concurrency-requests 1000 --benchmark-principals 5 --benchmark-budget-mode user-global --provider-latency-profile mixed-0-10-50ms --competing-io-mib-per-second 4 --competing-io-max-mib 32 --output artifacts/qualification/on-prem-concurrency-performance/ordinary-server/disk-contention.json
rtk proxy bash -c 'CARGO_BUILD_JOBS=1 cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-db-diagnostics'
rtk uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --target-class ordinary-server --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --diagnose-publication-phases --output artifacts/qualification/on-prem-concurrency-performance/ordinary-server/sqlite-gate-diagnostic-session.json
rtk proxy bash -c 'CARGO_BUILD_JOBS=2 cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets --test on_prem_global_budgets --test on_prem_attribution -- --test-threads=1'
rtk uv run pytest tests/tooling/test_qualification_sbc.py tests/tooling/test_qualification_ordinary_server.py tests/tooling/test_on_prem_mmc_tail_attribution.py tests/tooling/test_qualification_stability.py -q --tb=short --maxfail=1
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run python scripts/validate_release_docs.py
rtk git diff --check
```

### Results

| Command | Result |
|---|---|
| Locked release build | pass; measured binary SHA-256 is recorded above. |
| Ordinary-server matrix and managed lifecycle | pass; 27/27 Q008 runs and both Q009 phases; generated summary validated all reports against the frozen candidate. |
| Bounded same-filesystem contention run | pass; 1,000/1,000 requests completed, zero HTTP failures, zero pending requests and active reservations. |
| Feature-gated SQLite phase diagnostic | pass; 60/60 requests; runtime state converged; gate timing reported above. |
| Rust budget/attribution targets | pass; 48 tests total (29 user budget, 16 global budget, 3 attribution), serial. |
| Focused qualification/tooling pytest | pass; 57 tests. |
| Ruff, Pyright, release-doc validator | pass; no Ruff findings, Pyright 0 errors, release-doc validator passed. |
| Full workspace Rust suite | not rerun; no Rust runtime/schema source changed. The directly relevant user/global budget and attribution targets passed here; integrated M022 closure `plans/closure/on-prem-user-access/022-status.md` records 1,039 default-feature tests and 1,040 no-default-feature tests passing (one ignored in each run). |
| Full tooling suite | pass; 197 passed, 3 skipped. The final exact-commit summary guard also passed its focused five-test file. |
| Hosted CI | not run in this local closure pass. |

`uv run` normalizes `uv.lock`'s `requires-python` field in this environment;
that incidental change was reverted and is not part of this work.

## 5. Invariant review

- All inference traffic and controlled contention stayed on loopback and a
  disposable root. No real provider, external billing, production database, or
  customer identity was used.
- Principal count and active concurrency remain separate in the manifest and
  report. No 49-user simultaneous-inference claim is made.
- Ordinary-server measurements require durable non-rotational SSD/NVMe;
  temporary and rotational storage are rejected. The controlled writer has
  explicit rate/volume caps and remained below the frozen I/O stop gate.
- No storage topology, WAL/checkpoint policy, SQLite worker/gate, admission,
  routing, provider quota, user budget, or migration behavior changed.
- Pi M001/M012 artifacts and the Pi solo envelope were not rewritten.

## 6. Failure and recovery review

The lifecycle corpus covers client cancellation, partial/disconnected streams,
provider failure classes, budget denial before dispatch, user disable during an
in-flight request, abrupt process termination, and restart reconciliation. The
disabled credential was rejected with HTTP 401 and did not add a provider
request. Restart did not replay unknown in-flight provider work; durable state
settled with zero pending requests and reservations.

Failed attempts were retained: the initial Q008 run exposed insufficient
post-run budget headroom; the first lifecycle attempt used retired shared-key
inference; two initial SQLite diagnostic attempts exposed protected runtime
diagnostic access/quiescence assumptions. The qualification tools were
corrected to use a managed synthetic member for disable, an ephemeral browser
session for protected runtime diagnostics, and a larger budget headroom. The
final reruns passed; failed reports remain in the artifact directory.

## 7. Migration and compatibility review

No schema, migration, configuration, endpoint, CLI, or runtime compatibility
change was made. The Q008 SBC default remains unchanged. Ordinary-server
qualification is an explicit target profile. Diagnostic browser-session
material is held only in process memory and is never serialized.

## 8. Security review

The raw-to-summary tooling retains only bounded synthetic scenario labels,
scalar timings, host classes and aggregate state. Fixed synthetic account
labels may appear in CLI fixture records; no real usernames or customer
identities are used. Tokens, passwords, browser cookies, request/response
bodies, authorization headers, hostnames and environment values are not
written to reports. The synthetic member token and browser cookie stay in
memory. The committed artifact corpus passed the secret-pattern content audit.

## 9. Documentation and operations

`docs/deployment.md` reports the measured host envelope, workload, p95/p99 and
maximum observations, and repeat command with an explicit no-SLA boundary.
`docs/raspberry-pi.md` links to the separate server evidence without changing
the Pi results. `architecture/deep-dive-observability.md` distinguishes
qualification artifacts and supplementary gate timing from runtime telemetry.
Release documentation validation passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | The capacity envelope comes from one host and one deterministic synthetic provider profile; the competing-I/O and SQLite-gate diagnostics are supplementary single runs | Other hardware, provider latency, request size, background load and policy can produce different tails; the optional phase diagnostic is not per-cell p99 evidence | Treat the published numbers as a relative reference only. Run the same frozen corpus on any materially different deployment class before making sizing or SLA claims. |
| low | Hosted CI was not run in this local closure pass | This documentation/tooling/evidence closure has local verification only | Standard CI remains available for the next code-bearing change; no runtime release depends on M002. |

No high- or medium-severity M002 finding remains. The plan's acceptance
criteria are met and its stop conditions were not triggered during collection.

## 11. Roadmap disposition

M002 is closed. The result supplies the ordinary-server operational evidence
required by on-prem user-access M023 and supports host-specific relative sizing.
It authorizes no production performance change. M001 remains conditionally
closed on its own Pi/microSD attribution conditions; M002 does not reopen or
resolve them.

## 12. Registry updates

The implementation plan remains `implemented`; the closure record and roadmap
mark M002 `closed`. Unblock audit searched `plans/implementation/`, both
on-prem roadmap dependency sections, and the ready/active/blocked registry
tables. M023's hard M022 dependency is closed and its operational M002
requirement is satisfied, so M023 moves `blocked` to `ready`. No other future
plan declares M002 as a hard, interface, or soft dependency; nothing else is
promoted or blocked.
