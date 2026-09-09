# O010 — Differential Qualification and M9 Closure

Status: dependency-ready; O009 closure accepted

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: invariant/polish

Hard dependency: accepted O009, with O001-O008 accepted serially.

## Objective

Qualify M9 as a complete operational Rust command surface against the O001 Python oracle, with particular emphasis on failure isolation, process races, data-loss prevention, secret handling, bounded background tasks, and deployment/update recovery. O010 is the only plan allowed to close M9 and promote M10.

O010 should add qualification/fixes required by evidence, not a second implementation architecture. Any newly discovered medium/high defect receives either an O010-local bounded fix if clearly within an already-owned contract or a new corrective O011+ plan under append-only governance if closure cannot be accepted cleanly.

## Gate 1 — command implementation coverage

Machine-check the current `rust/src/cli.rs` command tree against dispatch coverage.

Closure blocker if any documented supported command/subcommand still reaches migration-stage `NotImplemented`.

The matrix must cover:

- lifecycle/control commands;
- config/key/provider/onboarding;
- every `configsetup` target;
- migrations/DB/backup/recover;
- accounts/models/modelinfo/stats/dashboard;
- update/version;
- every deploy subcommand and uninstall.

Parser-only groups are acceptable where their subcommands are fully implemented.

## Gate 2 — Python/Rust differential CLI corpus

Run the O001 corpus two-sided and compare:

- exit code/category;
- stdout/stderr ownership;
- JSON fields/types;
- filesystem effects;
- DB effects;
- process state transitions;
- archive metadata/member facts;
- deployment file contents/facts;
- update target/result facts.

Exact parity is required where O001 marked exact. Semantic normalization is limited to approved framework/process details such as Rust not reproducing Granian's worker count.

Every normalization must be explicit and narrow; no broad “ignore output differences” rule.

## Gate 3 — lifecycle/race qualification

Exercise the real Rust executable/subprocesses under isolated roots:

- foreground and detached startup;
- simultaneous daemon starts;
- simultaneous ensure-running;
- stale PID/socket/log/lock files;
- foreign/reused PID safety;
- stop during finite request;
- stop during stream;
- restart during retained finalization;
- restart with slow drain/forced timeout;
- rehash racing stop/shutdown;
- rehash Busy/caller disconnect;
- daemon parent exit after child spawn;
- child crash and next-start recovery.

Assert no leaked admission gate, generation, finalization job, reservation, provider client, socket, lock, or unreaped child after convergence.

## Gate 4 — mutation/concurrency qualification

Run concurrent/failed config operations:

- set/newkey/connect/logout/dashboard mutations against the same file;
- invalid candidate after temp write;
- permission/full-disk/rename failures;
- rehash/control unavailable/restart fallback races;
- interrupted onboarding;
- explicit secret-display commands vs normal logs/output.

Require no lost unrelated config section and no zero-length/partially-written canonical config.

## Gate 5 — backup/recovery data-loss matrix

Use realistic SQLite WAL activity and synthetic data. Cover:

- backup while requests/metrics write;
- automatic/manual overlap;
- corrupt/malicious archives;
- restore while server running;
- restore replacement fault at every critical file;
- crash/subprocess termination during staging/commit where practical;
- recovery after a failed restore attempt;
- fresh/current migration compatibility after restore;
- Python reads Rust-restored DB/config and Rust reads Python-created backup where contract requires cross-implementation compatibility.

Closure blocker if any ordinary fault can destroy both old and staged valid state or requires DB reset as the only recovery.

## Gate 6 — background task closure

R008 listed exactly three deferred M9 callbacks. O010 must prove all are real and no longer deferred:

- `metrics_flush`;
- `update_checker`;
- `automatic_backup`.

For each assert:

- correct enable/schedule spec;
- exactly one registered task;
- non-overlap;
- reload-safe reconfiguration;
- bounded diagnostics/history;
- failure isolation;
- shutdown convergence;
- no captured retired generation across ticks where generation context is needed.

Also rerun catalog/retention/checkpoint R008 tasks to prove M9 did not duplicate or destabilize the original supervisor inventory.

## Gate 7 — update fault/security matrix

Using a local fake release service and temp executable:

- latest/exact/v-prefixed targets;
- missing/invalid versions;
- slow/oversized/malformed metadata;
- wrong platform/arch;
- bad/missing digest;
- interrupted download;
- replacement failure at each stage;
- version verification failure;
- restart failure;
- concurrent update;
- symlink/read-only/unsupported install path.

Hash config/database before and after every scenario. They must remain unchanged.

Run security review for redirect trust, artifact name/path, executable mode/owner, temp files, rollback binary, and hostile metadata terminal injection.

## Gate 8 — deployment/uninstall fault matrix

Using fake command/root environments plus one bounded disposable Linux acceptance environment:

- personal systemd rendering/install;
- production rendering and user/path facts;
- each mandatory systemctl failure;
- cron install/reinstall/uninstall;
- backup cron;
- logrotate;
- deploy all;
- uninstall all keep-flag combinations;
- symlink/ambiguous path refusal;
- interrupted/partial external command sequence;
- repeated install/uninstall convergence.

No test may mutate the CI host's real `/etc`, systemd, cron, users, or production HOME.

## Gate 9 — resource/SBC-scope sanity

M10 owns measured SBC characterization, but M9 must still reject obvious operational leaks:

- repeated CLI invocation does not retain daemon-side connections/tasks;
- control accept loop remains bounded under malformed local clients;
- metrics buffer and task histories remain capped;
- backup/update staging files are cleaned/retained intentionally;
- repeated rehash/status/update-check does not grow process maps/history unboundedly;
- daemon/watchdog does not spawn duplicate processes.

Do not invent performance thresholds. Record qualitative/bounded state counts and gross regressions only.

## Gate 10 — dependency/schema/security review

Review the complete M9 diff.

Block closure on:

- second runtime manager/scheduler/HTTP stack;
- public management listener;
- ORM/schema fork;
- general workflow/daemon/deployment framework;
- unbounded local IPC or diagnostics;
- raw secret logging;
- unsafe archive extraction;
- unsafe binary replacement;
- broad path deletion;
- Python fallback execution from supported Rust commands.

Any new dependency must have a direct M9 need, small scope, and no simpler existing-stack solution.

## Gate 11 — documentation and boundary review

Update migration/user documentation needed for implemented behavior, but maintain phase boundaries:

- Rust candidate operational commands are documented for M10 qualification;
- Python remains canonical production/default install until M11;
- M10 owns full differential/live-provider/dashboard visual/SBC characterization;
- M11 owns Rust-default release/install/update availability;
- M12 owns Python retirement.

Do not claim migration completion at M9.

## Required regression suites

At minimum rerun affected closed boundaries:

- F003/F004/F006;
- M4 provider transport where update/model refresh reuses HTTP;
- M5 routing/catalog/health;
- M7 coordinator finite/stream/finalization/recovery;
- M8 R006-R013 task/reload/shutdown/authority;
- O001-O009 focused suites;
- full Rust all-targets;
- full migration Python oracle;
- targeted Python CLI/control/runtime/backup/update/deploy/integration suites;
- smoke tests;
- Ruff/Pyright/shellcheck where relevant.

No paid/live provider is a mandatory M9 closure prerequisite.

## Closure record

Write `migration-rs/closure/operations/010-status.md` containing:

- implementation commits for O001-O010;
- complete command implementation coverage;
- differential pass counts/approved differences;
- lifecycle/race matrix;
- config mutation fault matrix;
- backup/recover malicious/fault matrix;
- background task inventory before/after;
- update replacement/security matrix;
- deploy/uninstall matrix and bounded Linux acceptance evidence;
- dependency/schema/security/resource review;
- exact verification commands/results;
- unresolved findings;
- registry/roadmap transition.

## Acceptance criteria

O010 closes M9 only when:

- all documented F003/current commands have real supported Rust behavior;
- all exact/semantic O001 observations pass or have an explicit accepted architectural difference;
- process lifecycle/reload/watchdog races converge without provider replay, DB reset, stale authority, or duplicate server;
- config/provider mutations are atomic/recoverable and secret-safe;
- backup/recover cannot escape reviewed paths or destroy the sole valid state under tested faults;
- all three deferred R008 callbacks are real bounded singleton tasks;
- inspection/stats/model/configsetup commands are complete;
- update replacement is verified/rollback-capable and never touches config/DB;
- deployment/uninstall are bounded, idempotent where contractual, and path-safe;
- no unresolved high/medium correctness, security, resource, compatibility, data-loss, lifecycle, or packaging issue remains;
- M10 is the sole next eligible milestone.

Accepted O010 updates `registry.md` to mark M9 closed and makes M10 eligible for its own planning/implementation review; it does not auto-create M10 work.
