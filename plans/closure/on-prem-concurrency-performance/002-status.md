# On-Prem Concurrency Performance M002 — Closure Status

Status: conditionally closed — ordinary-server tooling and workload design are implemented; the isolated host run and capacity envelope remain unmeasured

Source implementation plan:

- `plans/implementation/on-prem-concurrency-performance/002-ordinary-server-team-baseline.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-concurrency-performance-roadmap.md#milestone-002--ordinary-server-small-team-baseline`

Repository baseline reviewed: `cdfb6fa8d4aaa1d9d5b43af8c87eaac68f83c402`

Implementation commits or pull requests:

- `cdfb6fa8d4aaa1d9d5b43af8c87eaac68f83c402` — ordinary Linux host attestation, safe SSD/NVMe gates, deterministic upstream latency profile, bounded host I/O observations, 16-client support, frozen 27-run driver, summary validation, and deployment/observability guidance. No runtime change.

## 1. Executive finding

The tooling/setup slice is complete. `scripts/qualification_ordinary_server.py` freezes the manifest before collection, schedules nine principal/concurrency conditions with three fresh-root runs each, and derives an aggregate report only after all raw runs pass the same build, storage, p99-sample, and settlement gates. `scripts/qualification_sbc.py` retains the SBC default and adds ordinary-host metadata, persistent database-storage checks, the 16-client tier, a deterministic 0/10/50 ms upstream profile, and host I/O observations.

No capacity run was started and no release candidate was built. The available host is a bare-metal Ubuntu 24.04.5 x86_64 workstation with durable ext4/NVMe, but it is shared with long-running unrelated CPU work and is not an explicitly isolated server qualification target. During the required Rust suite build, Linux I/O pressure exceeded the predeclared ordinary-runner stop threshold of 25% full `avg10` by a wide margin. Both suite attempts were stopped. No timing or throughput result is inferred from the host or from M001's separate exploratory NVMe cell. The team capacity envelope, SQLite gate tails, active-load cancellation/restart behavior, and policy-denial rates remain unmeasured.

Disposition is conditional under the plan's stop condition. Do not claim an ordinary-server throughput, concurrency, or latency envelope from this closure.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Freeze named-account and concurrent-request levels separately | `scripts/qualification_ordinary_server.py`, `tests/tooling/test_qualification_ordinary_server.py` | pass | Five account levels (1/5/10/25/49 at concurrency 1) and five concurrency levels (1/2/4/8/16 at five principals) form nine unique conditions and 27 planned runs. |
| Freeze workload, candidate identity, and gates before collection | M002 driver and summary contract tests | pass | The driver writes its matrix before invoking any condition; each condition is 1,000 finite requests, three repetitions, hard user/global budgets, and the fixed synthetic 0/10/50 ms upstream profile. |
| Record host/build/storage facts and require durable SSD/NVMe | `ordinary_server_metadata`, Q008 report schema, durable-path guard | partial | Host facts are implemented. The host was attested as Ubuntu 24.04.5, Linux 6.8.0-147-generic, Intel Core i9-9900K, 16 logical CPUs, 15 GiB RAM, Gigabyte Z390 AORUS MASTER, ext4 on Samsung SSD 980 1TB NVMe, bare metal (`systemd-detect-virt: none`, container: none). No release candidate was built and no raw run was produced. |
| Run three fresh-root durable-server repetitions per condition and collect latency/resource metrics | Frozen driver, full artifacts absent | not run | No ordinary-server experiment ran. CPU, RSS, throughput, TTFT, I/O, WAL, gate, and per-condition latency values are not reported. |
| Require adequate p99 observations and converged durable state | Q008 runner plus summary guard tests | pass — tooling contract only | The driver requires 1,000 completed observations, a p99 value, zero request failures, zero pending requests, and zero active reservations in all three raw runs before summarizing. No observed run passed or failed these gates because no run started. |
| Exercise failure, cancellation, active-user restart, mixed policy denies, and token revocation | Existing Q008 functional lifecycle coverage; no M002 raw run | not run | The new matrix measures successful budgeted inference. It does not yet drive cancellation/restart during active load or count provider, identity, and budget 429s separately. No behavior is claimed. |
| Publish an honest ordinary-server deployment envelope and compare with Pi solo evidence | `docs/deployment.md`, `docs/raspberry-pi.md`, this record | partial | The docs separate named users from simultaneous requests and state that there is no M002 capacity result. The immutable Pi M001/M012 evidence is unchanged. No sizing range is published. |
| Keep runtime, persistence, and user-policy invariants unchanged | Changed-file review | pass | No Rust, schema, admission, quota, auth, provider transport, SQLite journal, worker, or checkpoint policy changes. |

## 3. Production implementation evidence

No production behavior changed. M002 tooling adds ordinary-host inventory without exposing mount source paths or process command lines; root/database storage class and filesystem are bounded facts. The benchmark runner rejects tmpfs/ramfs/overlay and rotational storage, and standalone ordinary-server benchmark invocations enforce the same preflight I/O-pressure gate as the matrix driver. The deterministic latency profile cycles through 0, 0, 0, 10, 10, and 50 ms per synthetic upstream request. All provider traffic remains loopback-only.

The frozen matrix contains 27 runs across nine unique cells. It uses a 1,000-request concurrent batch, three repetitions, 30 sequential samples per surface, native finite, native streaming, and translated streaming cases, synthetic managed principals, and hard user/global request budgets. No raw M002 report, summary, or capacity number exists because preflight did not proceed to collection.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk proxy bash -c 'CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1'
rtk uv sync --frozen
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run pytest tests/tooling/test_qualification_sbc.py tests/tooling/test_on_prem_mmc_tail_attribution.py tests/tooling/test_qualification_ordinary_server.py -q
rtk uv run python scripts/validate_release_docs.py
rtk git diff --check
```

### Results

| Command | Result |
|---|---|
| Rust fmt, default Clippy, no-default check, and no-default Clippy | pass |
| Workspace serial Rust tests, default parallel build | interrupted before test results; observed I/O PSI reached `some avg10=94.05%`, `full avg10=81.43%`, `full avg60=46.12%` during compile/link |
| Workspace serial Rust tests with one Cargo build job | interrupted before test results; observed I/O PSI reached `full avg10=43.31%`, `full avg60=42.09%` during compile/link |
| `uv sync --frozen` | pass; generated a local `requires-python` lockfile normalization, which was reverted because this task changes no Python dependencies |
| Ruff format/check and Pyright | pass; 65 files formatted, no Ruff findings, Pyright 0 errors |
| Focused Q008, M001 artifact, and M002 tooling tests | pass; 51 tests |
| Release-doc validator | pass; seven docs checked |
| Release build and ordinary-server matrix | not run; shared-host I/O pressure and lack of an isolated, provisioned server target triggered the plan stop condition |
| Hosted CI | not run from this local closure pass |

## 5. Invariant review

- The runner uses a disposable root, loopback-only provider, and synthetic identities. No customer data, real provider key, prompt, or production database is used.
- Named principal count is reported independently from in-flight request concurrency. The plan makes no 49- or 50-concurrent service claim.
- The qualification database stays on durable non-rotational SSD/NVMe for ordinary-server measurements; tmpfs is rejected.
- No database topology, WAL/NORMAL, one-worker, routing, quota, admission, or budget semantics changed.
- Existing M001 and M012 physical artifacts and Pi guidance remain intact.

## 6. Failure and recovery review

The Q008 runner still checks isolated backup/recovery and graceful stop paths, and the matrix summary requires all requests to finish with no pending rows or active reservations. M002 did not exercise server shutdown or cancellation during active inference, retries/partial upstream failure, identity disable/revocation during load, or the separate observed 429 categories. These remain unverified and must be included before using the matrix as team-operation evidence. No active reservation or failure count is fabricated.

The environment stop was safe: the Rust test build processes were interrupted, no qualification process or synthetic request batch was started, and no performance artifact was emitted.

## 7. Migration and compatibility review

No schema, migration, configuration, endpoint, CLI, or runtime compatibility change was made. The SBC runner's default target and schema remain unchanged; ordinary-server qualification is an explicit target profile.

## 8. Security review

The tools retain bounded aggregate host, timing, and storage-class facts. They do not serialize request bodies, hostnames, addresses, credentials, tokens, usernames, or environment values. Existing redaction guards and the new raw-to-summary tests pass. There are no M002 raw artifacts to audit.

## 9. Documentation and operations

`docs/deployment.md` documents the frozen workload, repeat command, safe storage requirements, preflight pressure stop, and no-SLA boundary. `docs/raspberry-pi.md` keeps the SBC solo envelope separate. `architecture/deep-dive-observability.md` distinguishes qualification artifacts from runtime telemetry. The release-documentation validator passes.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | No isolated ordinary-server run, release candidate identity, or capacity observations were collected | There is no ordinary-server team sizing envelope; downstream work cannot cite M002 as measured capacity | On an explicitly provisioned, isolated Linux host, build the locked release candidate, retain its exact SHA and host/storage attestation, pass the 25% full-I/O-PSI preflight, and complete all 27 fresh-root runs. Preserve all failures and calculate the report from raw reports. |
| medium | Active-load cancellation/restart, retries/partial provider failures, token revocation, policy denies, and classified 429s are absent from the matrix execution | Budget recovery and failure behavior under simultaneous team load remain unknown | Extend the bounded synthetic scenarios and their sanitized guard before using M002 as operational readiness evidence; exercise and report provider, identity, and budget responses separately. |
| low | The full Rust workspace suite did not complete because both compile attempts crossed the I/O stop gate | This tooling-only change has focused Python coverage and compile/lint gates, but no serial workspace test result | Re-run the exact serial workspace suite on a stable, low-pressure host. |

## 11. Roadmap disposition

M002 is **conditionally closed for its tooling/setup slice only**. The ordinary-server measurements and deployment envelope are explicitly unmeasured. A future isolated hardware pass must provide the raw corpus and failure/recovery evidence above before any capacity recommendation is made. The Pi microSD solo benchmark remains a distinct reference and does not satisfy or block M002.

## 12. Registry updates

The implementation plan moves to `implemented` with this conditional closure; the roadmap remains `active` because no future production optimization is registered and M002's operational evidence is outstanding. The dependency-ready M002 row is removed and the conditional closure is recorded. Unblock audit: searched the on-prem implementation plans, both relevant roadmap dependency sections, and registry ready/active/blocked tables. No future plan declares M002 as a hard, interface, or soft dependency. On-prem user-access M023 names ordinary-server evidence as an operational requirement but remains blocked on its hard M022 dependency; M002 has not supplied that operational evidence, so M023 is not promoted and no other plan is unblocked.
