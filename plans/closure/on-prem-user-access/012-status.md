# On-Prem User Access Milestone 012 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m012--physical-sbc-small-team-qualification-and-final-disposition`

Repository baseline reviewed: `161ba3ef`

Implementation commits or pull requests:

- `89012fe7` — Activate M012 at `10eeee25` (schema 62, hard M008–M011 closures accepted)
- `b1f6d20b` — M012 SBC runner boundary contracts (`tests/tooling/test_on_prem_sbc_qualification.py`, 9 tests)
- `820fdc98` — Record M012 blocked closure with tooling foundation
- `161ba3ef` — Record M012 head CI outcomes in blocked closure
- This commit — M012 physical close on Raspberry Pi 5: managed-auth SBC runner corrective (`scripts/qualification_sbc.py` synthetic-admin bootstrap + personal-token inference, `epk1_` redaction, managed-fencing records), two contract tests (11 total in `test_on_prem_sbc_qualification.py`), Pi 5 functional + 3× benchmark artifacts, `docs/raspberry-pi.md` managed envelope, and this closure with registry/roadmap/plan updates

## 1. Executive finding

M012 closes on measured Raspberry Pi 5 evidence. The operational hardware
blocker from the prior blocked record is satisfied: a provisioned physical
Pi 5 (Model B Rev 1.0, 4 cores, 8 GB, Ubuntu 24.04.4, kernel 6.8.0-1065,
ext4 on MMC) ran the full single-admin managed corpus plus three fresh-root
benchmark batches against release `0.8.2` built on-device (SHA-256
`8609119a…1538`, schema 62). The SBC runner required a tooling corrective
first: on-prem identity (M001/M002) retired the shared server key for
inference and catalog, so the pre-managed runner failed with 401s. The runner
now bootstraps one synthetic administrator and drives inference and catalog
with the issued personal token (mirroring the Q007 pattern), records the
bootstrap as a redacted command, and records the two expected managed
fencings (server-key `runtime-status` CLI and dashboard shell require a
browser session) as pass-with-note rather than failures. Reports stay
secret-free and bounded.

Measured envelope (honest, no 50-concurrent claim): sequential p50 about
5 ms with streaming TTFT p95 4–19 ms; 49 named users provision at about
51 ms p50 with all spot logins passing; Argon2id logins (19 MiB, t=2, p=1,
two slots) run about 51 ms one-way and 71–73 ms two-way idle and hold under
gentle load; RSS stays near 20 MB with a 34.9 MB binary; every batch
converges with zero pending/active reservations and green backup, recovery,
restart, and shutdown. MMC tails reach seconds under batched and concurrent
load (predeclared p95/max gates fail — carried as the single medium finding
with the envelope restricted accordingly); 8-way concurrency saturates at
p50 about 4.1 s and exceeds this hardware class. Solo personal use is
practical in one process with no extra services; up to 2–4 concurrent
requests work with degraded tails. M001/M003/M005/M006/M007 conditional
closures are unchanged (immutable); M008/M009/M010/M011/M013 closures stand
and are cross-referenced. No high-severity finding remains.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A — Freeze actual target and work corpus | `on-prem-m012-pi5-functional.json` environment + candidate; `board_metadata` attestation `Linux aarch64 plus device-tree board model` | pass | Pi 5 Model B Rev 1.0, 4 cores, 8322752512 RAM, Ubuntu 24.04.4, kernel 6.8.0-1065, ext4, `mmc`, non-rotational, 52.4 °C; candidate 0.8.2 `8609119a…1538`, 34984648 bytes, `on-device-release-build` 1521999 ms, rustc 1.99.0, schema 62, WAL/NORMAL, 1 worker, threads 1. Le Potato and separate reference-Linux host recorded separately (see §10). |
| A — Loopback-only, secret-free fixtures | `test_m012_sbc_fixtures_carry_no_user_identity_matrix` (pass); artifact secret scan over all four new reports | pass | Fixtures carry `__SBC_UPSTREAM__`, no `[users]`/tokens/passwords/argon/`https://`; reports contain no `epk1_`, provider/server keys, passwords, or `http(s)://`. |
| A — Managed bootstrap + personal-token inference | Runner bootstrap step + `bootstrap-admin` command; `model-listing` + all inference via `agent_token`; new `test_m012_inference_paths_accept_a_managed_personal_token`, `test_m012_managed_bootstrap_secret_is_redacted` | pass | Mirrors Q007; password/token never enter reports (`epk1_` redaction in `bounded`). |
| A — Managed fencing honestly recorded | Functional `runtime-status` + `dashboard-page` pass-with-note cells; raw `runtime-status` CLI 401 retained in commands | pass | Server-key rejected for browser-session surfaces is intended M002 fencing; full runtime projection remains via browser session (M011). |
| B — 1/5/10/25/49 named users | Live 49-user run: 48 member creates (p50 51 ms, p95 78 ms, max 4607 ms, min 48 ms), `users list` 49 lines, DB 831488 bytes, spot logins 303 ×3; 5-user run with 5/5 tokens | pass | 1/5/10/25 reached as prefixes of the sequential 48-create run; 49 is not 49 simultaneous generations. One 4.6 s create outlier is an MMC stall, not a correctness gap (all 48 returned 302/303). |
| B — Actual concurrency/completion totals (1/2/4/8) | Runner concurrency-4 32/32 ×3; matrix concurrency-1 16/16, -2 16/16, -8 16/16; `CONCURRENCY_WORKERS == 4`, `CONCURRENCY_BATCH_SIZE == 32` | pass | Reports record actual counts only; no `named_users`/`capacity` fields. 16-way not attempted: 8-way already saturates (per plan conditional). |
| B — Allowed/denied selection, alias/semantic, tiny/moderate payloads, budget admission + last-slot | M008 hard/estimated + proof-or-reject (closed), M009 global intersection incl. two-user last-slot (closed), M004 profile gates (closed); live corpus covers Chat/Responses/Messages finite+SSE with terminal evidence | pass | No new bypass: live model-listing shows all three fixture models; translated stream carries Messages-upstream proof. |
| B — Operator-local self-hosted fixture, isolated, no prod keys | Loopback-only provider in runner + matrix scripts; temporary roots; test-only keys | pass | Never pointed at production DB or real credentials. |
| C — Physical Argon2id 1-and-2-way idle + under load | Same-origin login POST timing (302/303 = verified): 1-way idle p50 51–52 ms, 2-way idle p50 71–73 ms; gentle-load 1-way p50 52 ms, 2-way p50 71 ms | pass | Predeclared gates met (1-way p95 <2500, 2-way p95 <4000 idle, <5000 load). One 2765 ms 2-way idle outlier within gate; 4-worker hammering stalls (unrepresentative, documented contention). No OOM/swap. |
| C — RSS/CPU/gate/WAL/TTFT posture + solo deltas | Functional + 3× benchmark resource samples, durable convergence, integrity checks; Sep-21 pre-managed cross-build comparison | pass with note | Binary 34.9 MB (<40), baseline RSS ~20 MB (<25), peak ~20.8 MB (<50), threads 2–3, fds ≤17; single-request p50 unchanged (4→4–5 ms); benchmark p95 tails exceed predeclared gates (see §10 medium finding). WAL 0.1–1.6 MB, pending/active 0 every run, `quick_check` ok. Streaming TTFT p95 4–19 ms (gate ≤500 pass). |
| C — Single-user path stays practical, no extra services | One `eggpool serve` process; `users bootstrap` + personal token; fresh + legacy flows preserved via migration/M010 restore | pass | No new writer/service/process; dashboard themes, client profiles, `EGGPOOL_API_KEY` compatibility untouched. |
| D — Predeclared pass/fail gates | Gates chosen before new runs from Sep-21 baseline + solo path (G1–G10 in handoff); results reported verbatim | partial | G1/G2/G4/G5/G6/G7/G8 pass; G3 (p95 ≤1000 ms, max ≤3000 ms) fails on managed MMC tails; G9 passes with envelope caveat (8-way saturates); G10 solo single-request passes, batch tails documented. No thresholds fabricated after results; G3 carried as medium finding. |
| D — Benchmark corpus/fixture binding | `test_m012_benchmark_corpus_requires_benchmark_fixture` (pass) | pass | Corpus refuses ordinary fixture. |
| D — Baseline/candidate pairing discipline | `test_m012_control_candidate_modes_are_explicit_and_exclusive` (pass) | pass | Modes explicit, exclusive, phase-bound. |
| D — Honest capacity, no 50-user throughput marketing | Docs envelope + §11 disposition | pass | 49 named users inexpensive; 8 concurrent exceeds hardware; 16-way correctly not attempted. |
| E — M008–M011/M013 cross-reference | `008/009/010/011/013-status.md` (all closed) | pass | Token/cost admission, global intersection, crash recovery + v54 restore, browser qualification, transcode eligibility all accepted; this record adds only physical/small-team proof. |
| E — Focused regression targets | Tooling suite on Pi5: 178 passed (Q007-live + installer-harness deselected); `test_on_prem_sbc_qualification.py` 8 passed/3 skipped; `fmt --check`, `ruff`, `pyright`, docs validator pass | pass with note | Rust workspace not recompiled on Pi (tooling/docs-only diff; prior serial suite at same source baseline stands as substitute — see §4). Q007-live fails only on stale debug `0.8.1` (no `users`); release path used here is fresh `0.8.2` (see §10 low finding). |
| E — Whole-workspace hosted CI | Prior branch CI recorded in blocked closure (`820fdc98`/`161ba3ef`); no Rust source changed in this pass | pass (substitute) | `git diff --stat` shows only `scripts/`, `tests/tooling/`, `docs/`, `plans/`, `artifacts/`; zero `rust/` diff. Hosted CI re-runs on push. |
| E — Older `operations::update` platform disposition | M010 documented environment-specific disposition; benchmark/functional `operator-stats` + `transcoding-stats` pass | pass | No updater code touched. |
| Work packages 1–4 | 1: runner managed corrective + 2 contract tests + fixture validations green. 2: controls/artifacts/SHA/schema frozen; paired corpus + Argon2 run. 3: raw + derived aggregates archived per hardware class; tails investigated (MMC + managed contention + loaded-host I/O pressure). 4: capacity caveats + release matrix published; roadmap/registry closed here | pass | Diagnostic-only tail/storage/phase modes still use the shared key (low finding; M012 does not invoke them). |

## 3. Production implementation evidence

Zero production diff. No runtime, migration, config, fixture, or protocol
change: `git diff --stat` for this pass touches only `scripts/`
(runner managed corrective + `epk1_` redaction), `tests/tooling/`
(two contract tests), `docs/raspberry-pi.md` (managed envelope),
`artifacts/qualification/on-prem-m012-pi5-*.json` (four new reports), and
`plans/` (this closure, registry, roadmap, implementation-plan status).
Schema stays 62; no second writer/connection; no upstream key moved; no
wire change. Implemented (planned): managed-auth SBC runner corrective with
redacted bootstrap evidence. Planned but absent: nothing in M012's scope is
unimplemented — the named-user matrix, Argon2/resource measurement,
predeclared gates, and §15 physical artifacts (fingerprints, target corpora,
percentiles, WAL/recovery, eligibility checklist) are all archived below or
in the cited reports. Diagnostic-only tail/storage/phase modes were
deliberately left on the shared key (low finding in §10).

## 4. Verification executed

### Commands run

```bash
cargo build --manifest-path rust/Cargo.toml --locked --release
uv run --frozen python scripts/qualification_sbc.py --binary rust/target/release/eggpool --candidate-origin on-device-release-build --build-elapsed-ms 1521999 --output artifacts/qualification/on-prem-m012-pi5-functional.json
uv run --frozen python scripts/qualification_sbc.py --binary rust/target/release/eggpool --candidate-origin on-device-release-build --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-m012-pi5-benchmark-run-1.json
uv run --frozen python scripts/qualification_sbc.py --binary rust/target/release/eggpool --candidate-origin on-device-release-build --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-m012-pi5-benchmark-run-2.json
uv run --frozen python scripts/qualification_sbc.py --binary rust/target/release/eggpool --candidate-origin on-device-release-build --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output artifacts/qualification/on-prem-m012-pi5-benchmark-run-3.json
uv run --frozen pytest tests/tooling/ -q --tb=short --deselect tests/tooling/test_qualification_live_provider.py::test_q007_loopback_fake_emits_secret_free_evidence --deselect tests/tooling/test_installer.py::test_quick_installer_harness_passes
uv run --frozen pytest tests/tooling/test_on_prem_sbc_qualification.py tests/tooling/test_qualification_sbc.py tests/tooling/test_qualification_live_provider.py -q
uv run --frozen ruff format --check scripts/ tests/tooling/
uv run --frozen ruff check scripts/ tests/tooling/
uv run --frozen pyright scripts/
uv run --frozen python scripts/validate_release_docs.py
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
git diff --check
```

Ephemeral measurement aids (not committed; synthetic credentials only,
secret-free aggregates recorded here): `/tmp/m012_argon2_idle.py`,
`/tmp/m012_argon2_light.py`, `/tmp/m012_matrix.py`, `/tmp/m012_49users.py`
— same-origin login timing, gentle-load inference, admin-session member
provisioning, and concurrency 1/2/8 batches against temporary roots with a
loopback provider.

### Results

Local physical (this Pi 5 — local contract/physical evidence, never hosted):

- Release build on-device: 25m21s (1521999 ms), `0.8.2`, rustc 1.99.0.
- Functional (managed): status pass, 25 cells, startup-to-ready 136 ms,
  8/8 inference 4–6 ms with terminal evidence, model-listing 200,
  accounts/modelinfo/operator/transcoding pass, `runtime-status` raw 401
  with pass-with-note managed fencing, dashboard shell 401 with
  pass-with-note fencing, 5 static assets 200, rehash/backup/recover/
  maintenance/restart/shutdown pass, durable 8/8/8 with 0 pending/active,
  findings 0, stability pass, backup 3 members 842130 bytes.
- Benchmark runs 1–3 (30 samples, concurrency 4, 32-batch): all pass,
  schema v2, 145/145 requests each with 0 pending/active, findings 0.
  Sequential p50 4–5 ms finite/streaming/translated; concurrent p50
  13/14/15 ms; streaming TTFT p95 19/10/10 ms; translated TTFT p95 4/6/8 ms.
  Concurrent p95 10148/13897/13168 ms, finite p95 2011/402/6820 ms,
  streaming p95 223/3993/2519 ms, translated p95 7066/7870/6267 ms, maxima
  to ~14 s. RSS baseline ~20.0 MB, peak/final ~20.8 MB. Durable converges
  every run. Predeclared G3 (p95 ≤1000, max ≤3000) fails — medium finding.
- Argon2 (19 MiB, t=2, p=1, two slots): 1-way idle p50 51–52 ms (n=10);
  2-way idle p50 71–73 ms (n=10, one 2765 ms outlier within gate);
  gentle-load 1-way p50 52 ms, 2-way p50 71 ms. Gates pass. No OOM/swap.
  Four-worker hammering stalls (unrepresentative contention, documented).
- Named users: 48 creates p50 51 ms, p95 78 ms, max 4607 ms (MMC stall),
  `users list` 49 lines, DB 831488 bytes + WAL 1417312 bytes, spot logins
  303 ×3; 5-member run issued 5/5 personal tokens at 53–55 ms creates.
- Concurrency matrix (16-batch, admin token): 1-way 16/16 p50 5 ms
  (p95 10061 ms single stall); 2-way 16/16 p50 6 ms (p95 10783 ms);
  8-way 16/16 p50 4102 ms, p95 4716 ms — saturates, exceeds hardware.
- Tooling: 178 passed, 6 skipped, 2 deselected (full tooling sweep);
  SBC suites 47 passed/3 skipped pre-test-addition, 8 passed/3 skipped for
  the M012 file post-addition (11 tests); Q007-live fails solely on the
  stale debug `0.8.1` binary (no `users` subcommand) — low finding, release
  path unaffected. Ruff format/check, pyright (0 errors), docs validator
  (7 docs, 0.8.2, pass), `fmt --check`, `git diff --check`: clean.
- Rust workspace on Pi: not recompiled here. Substitute (justified): zero
  `rust/` diff in this pass, so the serial default (1008 passed) and
  no-default (1009 passed) suites recorded at the same source baseline in
  the blocked closure still stand for the Rust code; hosted CI re-runs on
  push. No test claimed without running it; unrun scope is explicit.
- Host conditions (recorded, not concealed): 11-day uptime, background
  `opencode` CPU, `kblockd` MMC I/O pressure with one transient D-state
  server worker that cleared, load averages up to ~10 during loaded
  phases. Tails include environmental storage contention alongside managed
  per-request DB work.

## 5. Invariant review

Per plan §4 (one admin/SBC instance, <50 named users, no SaaS/tenancy/
billing/IAM/WAN; upstream keys in TOML/environment, never in user tables or
web administration; generation leases, coordinator publication/finalization,
single SQLite gate/worker WAL/NORMAL, canonical wire, transparent streaming,
bounded hot-path memory, provider fairness; budget refusal never marks
provider unhealthy; published Chat/Responses/compact/Messages behavior,
auth headers, client profiles, dashboard themes, legacy upgrade preserved;
identity/cost redaction, secret-free logs/snapshots/artifacts, fail closed):
all hold. Zero production diff; schema 62; one gate/worker; loopback-only
fixtures; token/password material never in reports (scanned); denials roll
back routing claims without health effects (M008/M009, unchanged); routing
stays deterministic and load-based with selector-before-routing and no
account pinning (unchanged).

## 6. Failure and recovery review

Off-target execution still fails closed before measurement (contract tests).
Managed wrong-password logins fail closed with `Invalid username or
password` (no oracle); cross-origin/duplicate-provenance logins fail closed
(M011 semantics preserved); server-key inference/catalog rejected with 401
(proven live). Cancellation, forced-shutdown/restart, WAL growth and
convergence, revocation-during-flight, and backup/restore all pass in the
functional report (rehash, graceful-stop-before-recovery, isolated recover,
bounded maintenance, restart reconcile, graceful shutdown). Benchmark state
converges after stabilization every run (pending/active 0). Memory-pressure
abort behavior on target hardware remains unmeasured beyond the observed
no-OOM/swap outcome — carried as a bounded limitation, not asserted. Local
crash-recovery semantics remain those accepted by M010; nothing here
re-opens them.

## 7. Migration and compatibility review

No migration (schema 62 unchanged), no config-surface change, no protocol
change. Single-user TOML/provider/agent setup, v54-restore path (M010),
backup permissions, discovery, readiness probes, and
`EGGPOOL_API_KEY`/personal-token compatibility are untouched. Fresh
managed bootstrap and legacy-upgrade flows both work (migrate + bootstrap
exercised in every run). Downgrade still requires a pre-upgrade backup. No
rollback involved.

## 8. Security review

Secret-free: fixtures use loopback placeholders and test-only keys; live
passwords/tokens are synthetic, temporary-root-only, and absent from all
four reports (scanned for `epk1_`, keys, passwords, URLs); new tests assert
`epk1_` redaction and token-parameterized inference paths. Managed fencing
verified live: shared-key inference/catalog 401, server-key runtime-status
401, dashboard shell 401 without session, wrong-password HTML denial,
duplicate/cross-origin rejection preserved. No password/token/bearer in new
columns, logs, snapshots, or artifacts. No new network, privilege, or DoS
surface (tooling tests only; live servers bind 127.0.0.1 with bounded
batches and timeouts). Budget arithmetic/overflow semantics unchanged
(M008). Audit rows record admin member creation (live 48 creates).

## 9. Documentation and operations

Updated `docs/raspberry-pi.md` with the frozen managed Pi 5 envelope
(target, candidate, functional/benchmark/Argon2/named-user/concurrency
aggregates, artifact paths, honest capacity caveats, uncovered dimensions);
release-docs validator passes. `docs/deployment.md`,
`docs/agent-configuration.md`, `docs/backup-restore.md`,
`architecture/deep-dive-database.md`, and `README.md` are untouched —
no other doc change is warranted by this evidence. No operator action is
authorized beyond the documented envelope; LAN browser passwords still
require the operator TLS reverse-proxy/VPN posture stated in prior docs.
Artifacts committed: `artifacts/qualification/on-prem-m012-pi5-functional.json`,
`on-prem-m012-pi5-benchmark-run-{1,2,3}.json`.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Batched/concurrent p95/max exceed the Sep-21-grounded predeclared gates (G3): concurrent p95 10–14 s, finite p95 up to 6.8 s, maxima to ~14 s vs gates 1000/3000 ms | Envelope restricted: solo/low-concurrency practical, 8-way exceeds Pi5/MMC; no concurrent capacity claim | Done in this closure (honest envelope in docs + §11, no false claim). Concurrent/storage optimization needs a separately approved bounded plan with architecture acceptance — never a second writer or checkpoint retune from this evidence. |
| low | Le Potato target class not measured | No Le Potato claim exists; docs promise Pi 4/Pi 5 only | None in M012; a Le Potato run needs its own provisioned target and bounded plan if pursued. |
| low | Separate ordinary-Linux reference host not measured | Paired same-Pi controls ground solo deltas; M010 already holds ordinary-host observations | None in M012; cross-host comparison needs provisioned reference hardware if pursued. |
| low | 16-way concurrency not attempted | No 16-way claim; 8-way already saturates so the plan conditional correctly skips it | None; higher concurrency needs bigger hardware and its own plan. |
| low | Diagnostic-only tail/storage/phase and M007/M008 topology modes still use the shared key and will 401 in managed mode | M012 does not invoke them; persistence milestones using them are already closed | Tooling follow-up under a separate bounded plan if those modes must run against managed code. |
| low | Q007-live tooling test fails on this Pi's stale debug `0.8.1` binary (no `users` subcommand) | Release SBC path here uses fresh `0.8.2` and passes; no production impact | Rebuild the debug candidate on-device under separate tooling maintenance; not M012 scope. |
| low | Installer harness exceeds its 180 s budget on loaded hosts | Tooling-suite timing only; harness passes directly | Deployment-plan scope (carried from M011), not M012. |

No critical or high findings. No OOM/swap, WAL divergence, hard-limit
bypass, backup-identity defect, token leak, visual-security failure, or
false capacity claim.

## 11. Roadmap disposition

M012 is closed. Its hard dependency (accepted M008–M011 closures) was
already satisfied and its operational requirement (provisioned physical
Pi-class target) is now met with the evidence above; the on-prem
user-access roadmap therefore moves `active` → `closed` with M012
`blocked` → `closed`. M001/M003/M005/M006/M007 conditional closures remain
immutable history — this record supplies the additive physical/small-team
proof and does not rewrite them. M008/M009/M010/M011/M013 closures stand.
No successor is registered and none is unblocked by this closure (the only
ready row, Persistence M011, is independent). When new hardware classes or
higher concurrency targets must be claimed, they require new bounded plans
with predeclared gates — never an edit to this record.

## 12. Registry updates

Changes required in `plans/registry.md` + source roadmap (applied in the same commit).

- `plans/registry.md`: on-prem roadmap row `active` → `closed` with M012
  closed as current milestone; blocked-work M012 row removed; recently-closed
  gains the M012 row (disposition + evidence); unblock-audit paragraph
  records this closure with no promotion.
- `plans/subsystems/on-prem-user-access-roadmap.md`: `Status: active` →
  `Status: closed`; M012 row `blocked` → `closed` with this closure record;
  blocker text replaced by the measured envelope reference.
- `plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md`:
  `blocked` → `implemented` (runner corrective + physical evidence landed).
