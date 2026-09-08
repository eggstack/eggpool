# O001 — Operational CLI Contract and Deterministic Oracle Freeze

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Repository baseline: `e3edd5bc61b0718bc4559b85d30c27819e708350`

Primary class: invariant/infrastructure

Hard dependency: accepted R013 / M8 closure.

## Objective

Freeze the complete M9 operator-facing contract before replacing migration-stage `NotImplemented` handlers. O001 must turn the broad F003 parser inventory into executable Python observations covering command effects, filesystem/process conventions, local control framing, backup/recovery, background capability ownership, update behavior, deployment snippets, and failure/exit semantics.

O001 adds no production command capability beyond narrow test observation helpers.

## Required source audit

Audit current Python and Rust, not only historical plans. At minimum inspect:

- `src/eggpool/cli.py`, `cli_full.py`, `cli_exit_codes.py`, `fastcli.py`;
- `runtime.py`, `runtime_paths.py`, `deploy.py`, `deploy_user.py`;
- `control/client.py`, `control/server.py`;
- `lifecycle/`, including backup/recovery/uninstall helpers;
- `update_checker.py`;
- provider connect/onboarding/template code;
- integration renderers under `src/eggpool/integrations/`;
- metrics/background/task inventory;
- `scripts/install.sh`, deployment docs/templates;
- `rust/src/cli.rs`, `runtime.rs`, `server.rs`, `reload.rs`, `runtime_lifecycle.rs`, `task_supervisor.rs`;
- F003 contract inventory/closure and R008/R013 closure records.

Record any current Python command-tree delta from F003 explicitly. Do not silently edit F003 history.

## Contract corpus

### CLI parser/presentation

For every root command/subcommand freeze:

- arguments/options/defaults and global `--config` placement;
- help/usage descriptions where EggPool-owned text matters;
- stdout vs stderr;
- exit code/category;
- interactive/non-interactive behavior;
- JSON output schemas where present;
- success/no-op/already-running/not-running cases.

The corpus must cover the entire current F003 inventory, including all `configsetup`, `deploy`, `stats`, `modelinfo`, `accounts`, and lifecycle commands.

### Runtime paths/process state

Freeze:

- config/data/state/log/PID/control-socket resolution with XDG and deploy-user cases;
- PID file content/permissions/stale cleanup;
- duplicate-instance rules using PID plus health probe;
- foreground vs detached serve behavior;
- root refusal/`--as-root` behavior;
- stop/restart timeout semantics;
- croncheck/ensure-running fast-path results;
- systemd-managed foreground assumptions.

Use isolated temp HOME/XDG roots. Do not touch the host's real service manager.

### Local control

Freeze protocol version, socket path/mode, line framing, request/response fields, request-id echo, maximum request size, timeout/error classes, malformed/oversized/multi-line behavior, stale socket handling, and `reload_config` response projection.

Do not freeze Python class names; freeze bytes/fields/effects.

### Config/provider mutation

Freeze mutations for `set`, `newkey`, `init-config`, `connect`, `logout`, `onboard`, and dashboard/public toggles:

- exact TOML sections/values changed;
- preservation of unrelated content/comments where contractual;
- provider-template selection;
- env-secret behavior;
- live rehash vs restart/control-unavailable outcome;
- interactive cancellation;
- output redaction.

### Integration generation

For every `configsetup` target freeze deterministic input context and normalized output facts/bytes, secret display rules, write targets, overwrite/force semantics, clipboard fallback, model requirements, and any config mutation/restart side effect.

### Database/data operations

Freeze:

- migration command outcomes on fresh/current/failed checksum DBs;
- vacuum success/failure presentation;
- backup archive naming, format, members, metadata, exclusions, permissions and retention behavior;
- recover source selection, validation, running-server handling, destination replacement and failure safety.

The fixture must use synthetic secrets/data only.

### Operator inspection

Freeze stable projections for accounts list/status/explain, models refresh, all modelinfo commands, all stats commands, dashboard public output, and runtime-status. Normalize timestamps/terminal widths only when clearly incidental.

### Update

Freeze user-level semantics independently of Python packaging internals:

- installed/current version display;
- latest/no-update/update-available;
- exact version with/without `v`;
- invalid/missing release;
- `--check`;
- failed fetch/install/verification;
- whether a previously-running server is restarted;
- explicit guarantee that config/database are untouched.

Also record Python's current pip/pipx/uv/source command construction as historical distribution evidence. M9 Rust implementation may use a Rust binary artifact backend because M11 will change distribution authority; such a backend difference must preserve the frozen user-level semantics.

### Deploy/uninstall

Freeze generated systemd personal/production units, cron watchdog, backup cron, logrotate, `deploy all`, path/user/mode ownership rules, root/sudo refusal, confirmations, idempotence, fake-systemctl command sequence, uninstall keep flags, deploy-artifact removal, and data/config preservation.

## R008 deferred task contract

Capture Python-equivalent task specifications and callback semantics for:

- `metrics_flush`;
- `update_checker`;
- `automatic_backup`.

For each record enable predicate, interval/day/time semantics, overlap policy, failure isolation, diagnostics, reload behavior, and shutdown ownership. O001 must prove no fourth deferred R008 capability exists at current main.

## Fixture design

Add a bounded M9 fixture set under `migration-rs/fixtures/operations/` and Python observation helpers under `tests/migration_rs/`.

Prefer scalar/structured observations over giant snapshots. Separate exact fields from semantic fields. Never persist real API keys, proxy credentials, HOME paths, temporary paths, network addresses, archive payload secrets, or unbounded subprocess stderr.

Use deterministic fakes for:

- process existence/signals;
- health HTTP;
- Unix socket peers;
- provider templates/input prompts;
- release metadata/artifacts;
- `systemctl`, cron and logrotate filesystem roots.

No paid/live provider or root host mutation is allowed.

## Deliverables

- `migration-rs/operations-contract.md` describing M9-owned behavior and M10/M11 boundaries;
- deterministic fixture matrix and Python observations;
- test helper(s) that can invoke Python and later Rust commands in isolated roots;
- command-to-plan ownership table covering 100% of F003 commands;
- deferred-task oracle observations;
- any documented intentional distribution/backend differences with user-visible invariants preserved.

## Tests

O001 tests must fail if:

- a parser command has no M9 owner;
- a command/effect fixture contains an unredacted secret;
- an observation escapes its temp root;
- a background capability is missing/duplicated;
- archive fixture contains traversal/symlink surprises not explicitly classified;
- update/deploy fake invokes a real external destructive command.

## Verification

Run at minimum:

```text
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/unit/test_cli*.py tests/unit/test_runtime*.py tests/unit/test_update*.py -q --tb=short --maxfail=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

Adjust exact Python test globs to current repository layout and record commands/results in closure.

## Non-goals

- no Rust control socket;
- no daemon implementation;
- no backup/update/deploy production behavior;
- no M10 platform matrix;
- no public install cutover;
- no schema migration.

## Closure evidence

Write `migration-rs/closure/operations/001-status.md` with command coverage, exact fixture counts, R008 deferred-task inventory, security/redaction review, backend-difference decisions, verification results, and unresolved findings.

## Acceptance criteria

O001 closes only when every current documented/parser command and every M9 deferred background capability has an explicit frozen observable contract and owner, the fixtures are deterministic/secret-free/local, and no unresolved contract ambiguity could force O002-O009 to redesign shared architecture.

Accepted O001 promotes only O002.