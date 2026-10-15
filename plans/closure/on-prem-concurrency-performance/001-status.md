# On-Prem Concurrency Performance M001 — Closure Status

Status: conditionally closed — durable database storage on the Pi 5 microSD/MMC path is the dominant measured tail contributor; remaining workload and external-I/O controls are named below

Source implementation plan:

- `plans/implementation/on-prem-concurrency-performance/001-pi5-mmc-tail-latency-attribution.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-concurrency-performance-roadmap.md#milestone-001--pi-5mmc-concurrent-tail-attribution`

Repository baseline reviewed: `d2c0830acaeff90c45bffa393453cf0500c4fa64`

Implementation commits or pull requests:

- `03317052` — paired physical MMC/tmpfs qualification corpus and artifact guard, plus M001 closure/registry reconciliation; no Rust runtime or database-policy changes.

## 1. Executive finding

M001 has a reproducible physical attribution for the existing Pi 5 four-way finite-request tail. Across three fresh-root runs on the same Pi 5, release candidate, fixture, and 32-request/four-client workload, root ext4/MMC produced a median concurrent p95/max of **8,579 ms**. Moving the qualification temporary root, including its SQLite database, to RAM-backed `/dev/shm` reduced the corresponding median p95 to **10 ms** and median maximum to **11 ms**. Every run completed 32/32 requests and converged with zero pending requests and active reservations.

This agrees with the independent Plan 237 direct-provider and finite-phase evidence: provider service remained 0–1 ms, slow requests were pre-provider dominated, and the 60-request tmpfs control reached 4 ms maximum versus 1,785–3,473 ms on MMC. Plan 239 independently correlated the multi-second request with foreground publication `COMMIT` and WAL checkpoint-sequence changes. Taken together, the tested tail is owned by durable publication/SQLite/storage work on this Pi 5/MMC path, not provider service.

Plan 238 independently moved only the SQLite database/WAL/SHM to its temporary filesystem: its three MMC diagnostics had 3,331–4,944 ms maxima while all three database-only tmpfs controls had 4 ms maximum, with background-task contamination ruled out. This isolates the database storage path while leaving the qualification root on MMC.

The target is the Raspberry Pi 5 microSD boot device, not NVMe. The original six-run corpus used `/dev/mmcblk0p2` on the Pi's SD device; a read-only host check reports `/sys/block/mmcblk0/device/type` as `SD`. This is the intended low-cost SBC storage class and is the device on which the paired MMC/tmpfs attribution was measured. SSD/NVMe is not a closure requirement. An additional microSD qualification smoke run completed with four concurrent requests, five managed principals, and hard user/global request budgets enabled; all four requests returned HTTP 200 and durable state converged. Its four timing observations are functional smoke evidence only, not a latency estimate or capacity claim. A separate NVMe run remains exploratory and out of scope. M001's remaining gaps are controlled competing-I/O and the broader concurrency/principal/budget matrix; these limit broader workload claims but do not create a requirement to test alternate persistent media. No new capacity claim, durable-tmpfs recommendation, or SQLite topology/journal/checkpoint/concurrency-policy change is authorized. Existing restricted Pi guidance remains authoritative.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Freeze physical target, candidate, workload, and G3 gates | `artifacts/qualification/on-prem-concurrency-performance/matrix.json`; six raw Q008 reports | pass | Raspberry Pi 5 Model B Rev 1.0, Ubuntu 24.04.4, Linux/aarch64, kernel 6.8.0-1065-raspi; root is ext4 on `/dev/mmcblk0p2`, the MMC device identifies as SD; candidate SHA-256 `8609119a7f0494c8541b7a47ddbbe86be45ce3f84096b5ee87c228c71513a538`; preserved G3 p95 ≤1,000 ms and max ≤3,000 ms. |
| Ordinary physical qualification before benchmarks | `functional.json` | pass | Q008 v1, fresh temporary root, all functional checks passed. |
| Three paired MMC and tmpfs runs | `mmc-run-1..3.json`, `tmpfs-run-1..3.json`; matrix guard | pass | Same release binary, fixture, 30-sample sequential phases, 32-request four-way batch, and device. The tmpfs variable changes the temporary root and DB placement; output artifacts stay on MMC. |
| Reproduce the MMC tail without relabeling a failure | MMC artifacts and `test_m001_storage_control_reproduces_and_removes_the_g3_tail` | pass — G3 fails | All three four-way MMC runs exceed both G3 thresholds; p95/max are 7,599/7,599 ms, 12,058/12,058 ms, and 8,579/8,579 ms. The reports are qualification `pass` because functional/convergence checks pass; their latency gate outcome remains fail. |
| Falsify provider as the dominant contributor | `artifacts/qualification/237-sbc-finite-tail-diagnostic.json`; `artifacts/qualification/237-sbc-finite-tail-run-1..3.json` | pass | Direct-provider maximum 1–2 ms; slowest request pre-provider phase 1,778–3,473 ms, provider-service maximum 0–1 ms. |
| Falsify durable MMC storage as the dominant contributor | Paired MMC/tmpfs corpus; Plan 237, 238, and 239 physical artifacts | pass | New four-way p95 median falls from 8,579 ms to 10 ms; new max median falls from 8,579 ms to 11 ms. Plan 237's sequential diagnostic falls from 1,785–3,473 ms maximum to 4 ms on tmpfs. Plan 238 moved only the SQLite files: MMC max 3,331–4,944 ms vs. tmpfs max 4 ms, with no task contamination. Plan 239's slowest requests correlate with foreground publication commit/checkpoint-sequence changes. |
| Capture p50/p95/max and bounded resource/convergence facts | Six Q008 v2 reports and matrix guard | pass | The original 30-sample reports do not support an independent p99 estimate. All 192 concurrent requests completed and every report converged. |
| Exercise configurable concurrency, principal, budget, p99, and competing-I/O reporting | Runner implementation; six Q008 reports; microSD smoke artifact; NVMe exploratory artifact | partial | The runner exposes these controls. The microSD smoke covers four-way concurrency, five principals, and user/global budgets with four successful observations; this verifies the loopback path but is too small for latency inference. Competing-I/O was not enabled, and the broader matrix is incomplete. Alternate persistent media are optional and not required by M001. |
| Vary 1/2/4/8 concurrency and 1/5/49 managed principals, budget policy, and controlled host I/O | M012 closure/artifacts plus the M001 microSD corpus and matrix manifest | partial | The repeated attribution corpus holds four-way concurrency and one synthetic admin principal with hard budgets disabled. The new microSD smoke adds five principals and user/global budgets but only four concurrent requests. Controlled competing-I/O and concurrency 1/2/8 plus 49-principal budget cases remain unqualified. |
| Keep production, schema, and operator policy unchanged absent causal design review | `git diff --name-only -- rust/` empty; no migration/config/API change | pass | Existing single SQLite gate, WAL/NORMAL and the documented restricted envelope remain unchanged. |
| Machine-check the committed M001 qualification corpus | `tests/tooling/test_on_prem_mmc_tail_attribution.py` | pass | The guard covers all six required Q008 microSD/tmpfs reports. The separately labeled NVMe report is exploratory and outside the M001 corpus. |

## 3. Production implementation evidence

No production Rust, SQLite schema, config, endpoint, auth, or persistence-policy change was justified or made. The production owners remain `rust/src/coordinator/publication.rs`, `rust/src/coordinator/finalization.rs`, and `rust/src/db/connection.rs`; the independent phase evidence attributes the tested tail to the current durable SQLite publication/checkpoint path. Changing WAL mode, transaction topology, checkpoint policy, worker count, or database placement would require a separate reviewed plan and, where ownership/journal semantics change, an accepted ADR.

Implementation additions include the sanitized physical reports, a self-describing experiment manifest, a tooling regression guard, and benchmark-only controls for concurrency/request count, synthetic managed-principal count, hard budget mode, p99 at 1,000+ concurrent observations, mount-class reporting, and capped same-filesystem competing writes. No production runtime or database-policy change was made.

## 4. Verification executed

### Commands run

```bash
CARGO_BUILD_JOBS=1 cargo build --manifest-path rust/Cargo.toml --locked --release
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --output artifacts/qualification/on-prem-concurrency-performance/functional.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-concurrency-performance/mmc-run-1.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-concurrency-performance/mmc-run-2.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-concurrency-performance/mmc-run-3.json
TMPDIR=/dev/shm uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-concurrency-performance/tmpfs-run-1.json
TMPDIR=/dev/shm uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-concurrency-performance/tmpfs-run-2.json
TMPDIR=/dev/shm uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-concurrency-performance/tmpfs-run-3.json
TMPDIR=/mnt/qualification-ssd/eggpool-m001-2026-10-14 uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --expected-sha256 8609119a7f0494c8541b7a47ddbbe86be45ce3f84096b5ee87c228c71513a538 --candidate-origin on-device-release-build --benchmark-samples 30 --concurrency-workers 4 --concurrency-requests 1000 --benchmark-principals 5 --benchmark-budget-mode user-global --output artifacts/qualification/on-prem-concurrency-performance/ssd-c4-u5-user-global-run-1.json
TMPDIR=/tmp/eggpool-m001-microsd-2026-10-14 uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --expected-sha256 8609119a7f0494c8541b7a47ddbbe86be45ce3f84096b5ee87c228c71513a538 --candidate-origin on-device-release-build --benchmark-samples 30 --concurrency-workers 4 --concurrency-requests 1000 --benchmark-principals 5 --benchmark-budget-mode user-global --output artifacts/qualification/on-prem-concurrency-performance/microsd-c4-u5-user-global-run-1.json
TMPDIR=/tmp/eggpool-m001-microsd-c4-2026-10-15 uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --expected-sha256 8609119a7f0494c8541b7a47ddbbe86be45ce3f84096b5ee87c228c71513a538 --candidate-origin on-device-release-build --benchmark-samples 1 --concurrency-workers 4 --concurrency-requests 4 --benchmark-principals 5 --benchmark-budget-mode user-global --output artifacts/qualification/on-prem-concurrency-performance/microsd-c4-u5-user-global-minimal-run-1.json
findmnt -no SOURCE,FSTYPE /
cat /sys/block/mmcblk0/device/type
uv run pytest tests/tooling/test_on_prem_mmc_tail_attribution.py -q
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run ruff format --check tests/tooling/test_on_prem_mmc_tail_attribution.py
uv run ruff check tests/tooling/test_on_prem_mmc_tail_attribution.py
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```

The attempted debug refresh was `CARGO_BUILD_JOBS=1 cargo build --manifest-path rust/Cargo.toml --locked`; it was interrupted due to sustained shared-host I/O pressure.

### Results

| Command | Result |
|---|---|
| Locked release build | pass; release binary SHA-256 matches all six physical reports. |
| Ordinary Q008 run | pass on physical Pi 5; functional fixture only. |
| Three MMC + three tmpfs Q008 benchmark runs | pass on physical Pi 5; every 32-request batch completed 32/32 and converged. G3 latency thresholds fail on MMC and pass on tmpfs. |
| Supplemental NVMe benchmark | Exploratory and outside M001 target requirements; 1,000/1,000 HTTP 200, p50/p95/p99/max 13/30/32/33 ms, five principals, user/global request budgets, and zero pending requests or active reservations. Competing I/O was disabled. |
| Supplemental microSD budget smoke | pass; four-way/four-request batch, five principals, user/global budget mode, 4/4 HTTP 200, zero pending requests and reservations. Four timing observations only; percentiles are not used for performance inference. |
| Supplemental microSD 1,000-request attempt | stopped under the resource limit; I/O pressure reached `full avg10=72.21%`, `full avg60=56.82%`; no report produced. |
| Supplemental microSD 64-request and 16-request attempts | both stopped before longer-window I/O pressure reached the prior limit; peak observed `full avg10` was 71.59% and 70.56%, respectively. No reports produced. |
| Read-only target-media attestation | pass on the physical Pi 5: root `/dev/mmcblk0p2` is ext4 and the MMC device type is `SD`. This confirms the repeated MMC corpus targeted microSD storage. |
| `uv sync --frozen` | pass; 9 packages checked. |
| `uv run pytest tests/tooling/test_on_prem_mmc_tail_attribution.py -q` | pass; 4 tests. |
| `uv run pytest tests/tooling/test_qualification_sbc.py -q` | pass; 41 tests. |
| Full tooling suite | first attempt stopped after 81 passed / 3 skipped at the known stale-debug-binary Q007 test (`rust/target/debug/eggpool` lacks the current `users` command). |
| Current debug build | interrupted after the build drove shared-host I/O pressure above 90% over 60 seconds; no build result. This respects the plan's stop condition. |
| Ruff format/check on the new file | attempted after the saturation stop; `uv run` did not return, so the command was interrupted with no result. The file was formatted with Ruff before the focused test. |
| Full tooling rerun, Pyright, Rust fmt/Clippy/no-default/full serial tests | not run after the saturation stop; no result is claimed. |
| Hosted CI | not run from this local closure pass. |

The six original reports' runner-origin field says `q005-qualified-aarch64-copy` because the CLI default was not overridden. The release binary was built on this Pi immediately before that corpus and the measured SHA-256 is consistent across the reports; the manifest and this closure retain and disclose the provenance-label mismatch instead of rewriting raw reports. The supplemental NVMe run explicitly passed `--candidate-origin on-device-release-build`.

## 5. Invariant review

- Single SQLite worker/gate, WAL/NORMAL, generation ownership, retries/finalization, quota policy, and profile gates were not changed.
- No second connection, writer, checkpointer, runtime worker, service, API bypass, or provider-wire alteration was introduced.
- The physical G3 failure remains explicit. No result was relabeled as a performance pass.
- Reports contain bounded timing/resource facts and no credentials, prompts, bodies, user tokens, or provider keys.
- tmpfs is only a diagnostic control. It is not proposed for durable production database placement.

## 6. Failure and recovery review

All six original benchmark reports completed the 32-request batch without request failures and converged to zero pending requests and active reservations. The supplemental NVMe run completed its 1,000-request batch, and the minimal microSD budget smoke completed 4/4 requests; both converged to zero pending requests and active reservations. MicroSD runs at 1,000, 64, and 16 measured requests were interrupted under the plan resource stop condition before producing reports; their isolated servers exited. No result is claimed from those interrupted attempts. The completed qualification runs include isolated backup/recovery, restart reconciliation, and graceful shutdown. No production-data operation occurred. Ambient host load and the absence of a controlled competing-I/O trial limit causal claims beyond the MMC-versus-tmpfs storage result.

## 7. Migration and compatibility review

No schema, protocol, config, CLI, or client compatibility change was made. Migrations remain unchanged. Existing M012 G3 and capacity outcomes are preserved as historical evidence.

## 8. Security review

Qualification used a loopback-only synthetic provider and the runner's disposable managed bootstrap. The report guard rejects bootstrap password/token material. No real provider/user credentials or production database were used.

## 9. Documentation and operations

`docs/raspberry-pi.md` was not changed: the evidence does not justify a new durable-storage requirement or a safe operator mitigation. The existing restricted Pi 5 envelope remains accurate. The closure and matrix manifest link the six original raw physical reports and the supplemental NVMe report. No physical-SBC claim is made from a hosted or non-aarch64 environment.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | G3 p95/max fail on microSD/MMC in every repeated four-way run; tmpfs removes the tail. Controlled competing-I/O has not been isolated | Existing small-team envelope remains restricted; no new concurrency SLA or safe storage workaround can be claimed | Keep Pi guidance restricted. Before any optimization or wider claim, a bounded physical plan must compare controlled idle/competing-I/O conditions while preserving WAL/NORMAL and one-writer safety. SSD/NVMe comparison is not required. |
| low | The out-of-scope NVMe run covers four-way concurrency, five principals, and user/global hard budgets; the matching microSD attempt stopped under the plan's I/O-pressure limit. Repeated 1/2/8 concurrency and 49-principal cases also remain unqualified | No broad account-count or budget-overhead inference can be drawn | Use existing M012 results only for their recorded scope; if these factors affect an implementation decision, include them in a registered follow-up with three paired runs per condition. |
| low | p99 is available from the supplemental 1,000-sample run only; no p99 acceptance threshold is frozen | One sample's p99 does not define a stable gate or tail guarantee | Keep the reported p99 diagnostic-only; define a repeated sampling and acceptance method before using it for a decision. |
| low | The six original benchmark reports retain the runner's default `q005-qualified-aarch64-copy` origin label although the same-hash binary was built locally immediately before qualification | Raw candidate-origin metadata is inaccurate; the binary hash, repository revision, and preceding build command remain consistent | Preserve raw evidence and this disclosure; the supplemental run records `on-device-release-build`, and any repeat should pass that option. |

## 11. Roadmap disposition

M001 is **conditionally closed** with the named competing-I/O and workload-matrix conditions above. Its core attribution is supported by three paired physical runs per microSD/MMC and tmpfs condition plus independent phase evidence. Alternate persistent media, including SSD/NVMe, are not required to close this diagnostic plan; the supplemental NVMe cell is out-of-scope evidence. No optimization is authorized: changing SQLite ownership or checkpoint policy would need separate architectural review and its own bounded acceptance gates. The subsystem roadmap remains active because future storage or concurrency performance work is not yet registered.

## 12. Registry updates

The registry removes M001 from dependency-ready work and records this conditional closure. A search of `plans/implementation/`, the subsystem dependency graph, and the dependency-ready, active, and blocked registry tables found no plan declaring M001 as a hard, interface, soft, or operational dependency. No plan is promoted or unblocked. On-prem user-access M016 remains blocked on M014 and M015; this optional workstream has no dependency edge to that security/merge sequence. Future controlled-I/O and budget-matrix research requires a new bounded plan and does not reopen this immutable closure.
