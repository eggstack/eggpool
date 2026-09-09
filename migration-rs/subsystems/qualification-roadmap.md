# M10 Full Qualification, Portability, and SBC Characterization Roadmap

Status: active planning/implementation; Q005 dependency-ready

Repository baseline: `00dd27fa103e3c663968ecd95d9289c60fca0601` (accepted O010 / M9 closure).

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, `../003-planning-process.md`, ADR-0001 through ADR-0003, all closed M4-M9 subsystem roadmaps, and accepted O010 closure.

## Purpose

M10 is the final migration-wide qualification milestone before Rust cutover planning. It does not add another major runtime subsystem. It takes the closed Rust implementation from M4-M9 and proves that the accumulated compatibility claims hold together across the complete client/operator/database/dashboard surface and on representative deployment environments, especially ARM64 SBCs.

M10 is evidence-driven. When qualification finds a correctness defect, the owning implementation is corrected under the active Q-plan or a new corrective Q-plan; closure is not obtained by weakening fixtures or reclassifying a mismatch for convenience.

Python remains the behavioral oracle through M10. Rust remains side-by-side and must not replace the canonical public Python install/release path until M11.

## Current baseline

By O010, the Rust candidate has:

- complete public inference endpoints and provider routing/transcoding;
- crash-safe request/finalization/reconciliation behavior;
- generation-based reload/runtime/shutdown ownership;
- full operator CLI/control/backup/update/deploy surfaces;
- 440 passing Rust tests across 52 suites and a 102-test migration oracle at M9 closure;
- no migration-stage `NotImplemented` path for the 63 frozen command paths;
- one M8 task supervisor with all six business callbacks registered.

M9 intentionally did **not** claim:

- broad supported-OS/architecture qualification;
- real rootful Linux/systemd acceptance;
- live-provider interoperability beyond deterministic fixtures;
- dashboard visual review beyond SSR/static compatibility tests;
- representative ARM64 SBC resource characterization;
- migration-wide DB upgrade/rollback/recovery evidence under the completed Rust surface;
- sustained failure/reload/streaming/resource-stability characterization.

Those are M10.

## M10 invariants

1. **Qualification cannot redefine behavior.** Existing canonical contracts and accepted closures remain authoritative; mismatches are findings.
2. **Python remains the oracle.** Differential tests run genuinely distinct Python and Rust implementations until M11.
3. **Normalization stays narrow.** Dynamic IDs/timestamps/ports/path roots may be normalized only where already approved; semantic content, error class, status, ordering, terminal state, and durable effects are not normalized away.
4. **No automatic paid mirroring.** Live-provider qualification is opt-in, bounded, low-token, and never part of normal CI.
5. **Normal CI stays lean.** M10 may add reusable qualification scripts and a manual `workflow_dispatch` surface; it does not create a large always-on OS/provider matrix.
6. **Representative targets, not every target.** M10 proves the targets intended for M11 and explicitly records unsupported/non-qualified targets rather than implying universality.
7. **Linux ARM64 is mandatory.** Because EggPool is explicitly designed for SBC deployment, at least one real ARM64 Linux SBC must complete functional and resource characterization.
8. **Rootful tests are disposable.** Systemd/cron/logrotate/install/uninstall qualification runs only in a disposable Linux VM/host prepared for that purpose, never against an operator's production machine.
9. **No invented performance SLA.** Measure startup, RSS, CPU, latency, DB/WAL growth, file descriptors, task counts, and binary size; do not invent arbitrary pass/fail thresholds unsupported by product requirements.
10. **Resource correctness is still enforceable.** Leaked claims/jobs/generations/tasks/fds, unbounded state, crashes, deadlocks, or clearly monotonic growth under a bounded steady workload are correctness findings even without a latency/RSS SLA.
11. **Database compatibility remains bidirectional where safe.** Python-created DBs must open under Rust, and Rust-mutated DBs must remain readable by the final Python reference when schema compatibility says rollback is supported.
12. **Backup/recover protects the only good copy.** Qualification must fault restore/backup transitions without requiring DB reset.
13. **Dashboard design is not redesigned.** M10 reviews layout/DOM/escaping/assets/themes against Python; it does not use visual qualification as a reason to re-theme the product.
14. **Live-provider tests do not manufacture destructive failures.** Do not deliberately submit invalid credentials, abuse rate limits, or trigger account bans merely to observe errors; deterministic fixtures own destructive failure classes.
15. **No release cutover.** M10 may build/test candidate binaries but does not publish the canonical M11 release matrix, flip `install.sh`, or make Rust the public updater authority.
16. **No Python retirement.** Migration harnesses and oracle fixtures remain until M12.
17. **Findings are severity-triaged.** High/medium correctness, security, data-loss, compatibility, lifecycle, or resource findings block Q010 closure.
18. **Evidence is reproducible.** Environment metadata, exact commands, candidate commit, configuration class, and result hashes belong in closure records or bounded machine-readable artifacts.

## Implementation sequence

```text
M9 O010 accepted
  |
  v
Q001 qualification contract, target matrix, and evidence schema freeze
 -> Q002 migration-wide deterministic differential qualification runner
 -> Q003 database upgrade/rollback/backup/recovery compatibility
 -> Q004 dashboard SSR/DOM/static/visual parity review (accepted)
 -> Q005 supported-target build and non-root runtime portability (ready)
 -> Q006 disposable rootful Linux operational acceptance
 -> Q007 bounded live-provider interoperability smoke
 -> Q008 ARM64 SBC functional/resource characterization
 -> Q009 sustained failure/reload/stream/resource-stability qualification
 -> Q010 aggregate M10 closure and M11 readiness report
  |
  v
M11 planning eligibility only after accepted Q010 closure
```

Only `../registry.md` authorizes implementation. Q005 is the sole dependency-ready M10 plan after accepted Q004 closure.

## Qualification environment classes

Q001 must freeze the exact matrix from current product/document/repository evidence. The starting expectation is:

- **Deterministic local oracle environment** — mandatory on the development host; Python and Rust side-by-side, loopback providers/proxies, temp DB/config/runtime roots.
- **Linux x86_64 disposable environment** — mandatory non-root runtime plus rootful O009 deployment acceptance.
- **Linux aarch64 physical SBC** — mandatory functional/resource characterization.
- **macOS arm64** — current development/runtime path; qualify build and non-root runtime if the repository continues to claim/implicitly support it.
- **Other platforms** — Q001 explicitly classifies as supported, build-only, unsupported, or not qualified. Windows must not be silently claimed merely because some dependencies compile there; the Unix-control/runtime-path contract is reviewed first.
- **Live-provider environment** — opt-in only; credentials supplied outside the repository and redacted from all artifacts.

M10 does not require every supported platform to run every rootful deployment test. Rootful systemd behavior is a Linux contract.

## Q001 — Qualification contract, target matrix, and evidence schema freeze

Primary class: invariant/infrastructure

Freeze the complete M10 qualification matrix before expensive or environment-specific runs begin. Inventory every closed migration contract that needs aggregate proof, determine exact target support from docs/code, classify exact-vs-semantic comparisons, define environment metadata, and define machine-readable result/evidence formats.

Q001 also identifies which existing tests already satisfy cells so M10 does not duplicate hundreds of focused unit tests merely to create a new suite.

Exit: one versioned M10 manifest maps each mandatory compatibility/target/resource cell to an existing test, a new deterministic test, or a named later Q-plan.

## Q002 — Migration-wide deterministic differential qualification

Primary class: invariant/polish

Create one reproducible aggregate runner/report over the existing Python/Rust oracle surfaces. It should compose, not replace, F002 and the M4-M9 focused suites. Cover config/CLI, HTTP finite/streaming, routing/failure, rehash, SSR, operations, and durable observations using deterministic local providers and isolated state.

The runner must report missing cells and semantic mismatches distinctly from infrastructure failures. Normal CI may keep a small smoke subset; the complete runner is a deliberate qualification command.

Exit: all mandatory deterministic cells in the Q001 manifest pass or have explicit blocker findings. **Accepted; see `../closure/qualification/002-status.md`.**

## Q003 — Database upgrade, rollback, backup, and recovery compatibility

Primary class: invariant

Exercise the final Rust schema/repository/operations surface against Python-created databases and vice versa where rollback is supported. Cover old migration snapshots, latest Python DB, Rust startup/migrate, ordinary Rust request/metrics/catalog/operator writes, backup, restore, interrupted/faulted restore, vacuum/checkpoint, and final Python reopen.

No migration reset or Rust-only convenience schema is allowed.

Exit: every supported transition preserves schema checksums, required data, and rollback readability; faulted restore retains a usable prior state. **Accepted; see `../closure/qualification/003-status.md`.**

## Q004 — Dashboard SSR, DOM, static asset, and visual parity review

Primary class: invariant/polish

Qualify the completed Rust dashboard as a rendered product rather than only a route response. Freeze page/state fixtures, compare DOM/escaping/links/forms/static assets semantically, and perform bounded screenshot review at representative desktop/mobile viewports and theme classes.

Do not add a heavyweight browser dependency to the Rust runtime. Browser tooling, if needed, is qualification-only and should be existing/local or isolated developer tooling.

Exit: no material layout, missing-content, escaping, static-asset, or navigation regression remains; intentional differences are recorded explicitly.

## Q005 — Supported-target build and non-root runtime portability

Primary class: invariant/polish

Prove build and ordinary non-root runtime behavior on the target classes frozen by Q001. At minimum cover Linux x86_64 and Linux aarch64; qualify macOS arm64 if retained as supported. Exercise version/help/config, foreground serve/health, finite+stream loopback inference, rehash, runtime-status, backup/recover, and graceful stop using temporary roots.

Prefer a reusable script plus optional manual `workflow_dispatch` over a permanent full CI matrix.

Exit: each declared supported target has reproducible build/runtime evidence or is explicitly downgraded through a planning/product decision before closure.

## Q006 — Disposable rootful Linux operational acceptance

Primary class: invariant/capability

Run the O009 deployment surface against a disposable Linux system with real systemd/process/permissions behavior. Qualify personal/production systemd install, start, health, restart, rehash, log ownership, cron/logrotate rendering/install where available, backup cron, stale state recovery, uninstall keep flags, and repeated install/uninstall idempotence.

This is the place to catch differences that fake command runners cannot expose: unit permissions, service user paths, environment loading, process ownership, signal timing, and filesystem modes.

Exit: a disposable Linux host can install, operate, restart, back up, and remove the Rust candidate without manual database repair or leftover unsafe service state.

## Q007 — Bounded live-provider interoperability smoke

Primary class: invariant/polish

Run a deliberately small real-network smoke against available provider accounts to validate assumptions deterministic mocks cannot: TLS/API edge behavior, auth header shape, endpoint paths, real SSE framing, request IDs, catalog/model identifiers, and provider-specific success envelopes.

Use at least two structurally distinct upstream surfaces when credentials are available for closure (for example an OpenAI-compatible surface plus Anthropic/Gemini/native alternate surface), with tiny prompts/output caps. Do not mirror traffic, soak paid providers, deliberately rate-limit accounts, or test invalid credentials live.

Exit: mandatory live cells succeed with bounded cost and secret-free evidence; provider-specific mismatches become findings rather than new silent normalization.

## Q008 — ARM64 SBC functional and resource characterization

Primary class: invariant/polish

Run the Rust candidate on a real Linux aarch64 SBC representative of the project's deployment goal. Record board/SoC/RAM/storage/OS/kernel/Rust build metadata, binary size, startup time, idle RSS/CPU, finite/stream loopback request behavior, rehash, SQLite/WAL behavior, background tasks, backup/recover, and shutdown.

Where practical run the final Python reference on the same board for contextual comparison, but do not require Rust to beat Python by an invented percentage.

Exit: no functional or resource-stability blocker exists on representative ARM64 hardware, and measured characteristics are documented for M11 release decisions.

## Q009 — Sustained failure, reload, streaming, and resource-stability qualification

Primary class: invariant/polish

Exercise the integrated process long enough to expose ownership/resource defects that short unit tests miss. Use deterministic local providers/proxies with bounded mixes of finite requests, streams, client cancellation, provider disconnects/timeouts/5xx/429 fixtures, account failover, repeated live reload, catalog/background ticks, backup/metrics activity, and graceful/restart cycles.

Record RSS, fd count, thread/task proxies where observable, DB/WAL growth, active claims/reservations/finalization jobs, generations, wire flights/gates, task counts, and error convergence. The goal is bounded stable ownership, not synthetic high-load benchmarking.

Exit: no leak/deadlock/replay/stranded durable state or unexplained monotonic resource growth remains under the reviewed workload.

## Q010 — Aggregate M10 closure and M11 readiness report

Primary class: invariant/polish

Re-run the mandatory deterministic gates, aggregate Q001-Q009 evidence, resolve or explicitly block on findings, and write the final compatibility/target/resource delta report consumed by M11 planning.

Q010 does not itself flip installation/release authority. It answers whether the Rust candidate is qualified enough for M11 to plan public cutover.

Exit: no unresolved high/medium migration correctness, security, data-loss, compatibility, lifecycle, resource, target-support, or dashboard finding remains; required live/Linux/SBC evidence exists; M11 is eligible for a separate cutover planning review.

## Evidence model

Qualification evidence should be compact and reviewable. Prefer JSON/TOML/Markdown summaries containing:

- candidate commit SHA and Python package/source identity;
- OS/architecture/kernel/board/runtime versions;
- test/fixture manifest version;
- exact command and exit result;
- pass/fail/skip/block classification with reason;
- normalized observation hash or bounded scalar measurements;
- elapsed time and resource sample summaries;
- redacted provider/surface identifiers where needed;
- links/paths to closure records.

Never store API keys, proxy credentials, raw sensitive request bodies, full environment dumps, or arbitrary provider response bodies in evidence artifacts.

## CI posture

The normal `.github/workflows/ci.yml` is intentionally small. M10 may:

- add a manual `workflow_dispatch` qualification workflow;
- add scripts that run locally/on disposable targets;
- add a small deterministic Rust compile/oracle smoke to normal CI only if Q001/Q002 demonstrates that its absence creates unacceptable regression risk.

M10 should **not** create a broad always-on OS × architecture × provider matrix. Physical SBC and live-provider qualification remain explicit/manual evidence.

## Performance/resource posture

M10 records facts rather than inventing an SLA. At minimum measure:

- release binary size;
- cold startup-to-ready time;
- idle RSS and CPU after warmup;
- request latency overhead against a loopback upstream for finite and streaming paths;
- DB/WAL growth for a bounded workload;
- backup duration/size;
- file descriptor count and thread/process count;
- retained generations/finalization jobs/claims/reservations/tasks after convergence.

A result is a blocker when it reflects a correctness failure (leak, unbounded state, crash, deadlock, starvation, data loss) or a clearly impractical regression for the documented lightweight/SBC goal. Mere percentage differences are reported, not automatically failed, unless an earlier contract already defines a threshold.

## Non-goals

M10 does not:

- publish the canonical Rust release matrix or release assets;
- change `scripts/install.sh`/README quick start to Rust-default installation;
- remove Python packaging/runtime;
- introduce production telemetry or benchmarking services;
- run destructive tests against live provider accounts;
- add fleet/orchestration infrastructure;
- redesign the dashboard;
- create a large permanent CI matrix;
- add a new database schema for qualification metadata;
- optimize code solely to improve a benchmark without a demonstrated product/resource problem.

## Dependency posture

No Rust runtime dependency is expected for M10. Qualification-only Python/dev tooling may be used only when already available or narrowly justified. Browser screenshot tooling must not enter `rust/Cargo.toml`.

Any runtime dependency introduced to fix an M10 finding belongs to the owning corrective implementation and must be justified in that closure.

## M10 closure

Q010 may close M10 only when:

- the Q001 matrix has no unowned mandatory cell;
- migration-wide deterministic Python/Rust qualification passes;
- DB upgrade/rollback/backup/recovery transitions are accepted;
- dashboard DOM/static/visual review is accepted;
- every declared M11 target has build/runtime evidence appropriate to that target;
- disposable rootful Linux operational acceptance passes;
- bounded live-provider smoke passes for the required structurally distinct surfaces;
- at least one representative Linux ARM64 SBC completes functional/resource characterization;
- sustained local failure/reload/stream/resource qualification converges without ownership leaks or durable corruption;
- any intentional compatibility difference is explicitly documented and does not contradict levels 1-4 of planning authority;
- no unresolved high/medium finding remains;
- M11 is made eligible for its own planning review, with no automatic cutover implementation promoted.
