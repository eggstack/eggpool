# Deep Dive: Deployment & Operations

Back to [Architecture](README.md)

## Purpose

Production deployment, systemd integration, operational scripts, and the tools needed to run the native EggPool executable.

## Deployment Artifacts

### `deploy/`

| File | Purpose |
|------|---------|
| `eggpool.service` | Systemd unit file |
| `eggpool-logrotate.conf` | Logrotate configuration |
| `env.example` | Production env example |

### `rust/src/operations/deploy.rs`

Rust-owned systemd/logrotate/cron snippets for CLI output
(`render_personal_systemd`, `render_production_systemd`, `render_logrotate`,
`render_watchdog_cron`, `render_backup_cron`, `render_backup_script`,
`write_atomic`, `install_systemd`/`install_logrotate`/cron-block helpers,
`uninstall`, `remove_artifact`). Deployment
rendering and process commands are implemented in `rust/src/operations/`.

### `rust/src/operations/lifecycle.rs`

Detached startup, safe stop/restart, independent EggPool identity proof, and
the watchdog start workflow are composed here from `process.rs`, `paths.rs`,
and `control.rs`. Restart-after-mutation is also owned here, so operations do
not depend on the CLI runtime adapter. The CLI adapter in
`rust/src/runtime.rs` retains prompts, human output, and exit-code mapping.

## Installation

### `scripts/install.sh`

One-shot installer for the native Rust wheel. It recognizes existing package
manager and standalone installations, refuses ambiguous ownership, checks the
supported OS/architecture before mutation, and preserves configuration.
Native releases start at `NATIVE_RELEASE_VERSION = "0.8.0"`; historical
Python versions are catalogued exact-only targets and never the default.

## Operational tooling

The `scripts/` directory contains release, package-boundary, installer,
portability, and qualification tooling. The most relevant commands are
`qualify_quick_installer.py`, `validate_runtime_package_boundary.py`,
`validate_release_workflow.py`, `build_release_artifacts.py`,
`build_connect_artifacts.py`, `inspect_connect_artifact.py`,
`create_release_manifest.py`,
`validate_release_artifacts.py`, and `verify_published_release.py`.

Release footprint qualification keeps the reviewed Maturin 1.14.1 setting
in `packaging/pypi/pyproject.toml`
`strip = false` / `--strip false` unless an otherwise equivalent stripped
artifact passes executable, wheel, runtime, manifest, and target qualification.
ThinLTO is an ephemeral comparison only; it is not enabled in the Cargo release
profile without reproducible size/runtime benefit and complete supported-target
qualification. SBC characterization is descriptive, not hardware-CI or a
release threshold.

## Desktop helper release pipeline

The release workflow builds the proxy wheel/raw pairs (Linux x86_64/aarch64,
macOS arm64; exactly three raw artifacts) plus four `eggpool-connect` helper binaries (same three targets
plus Windows x86_64) from the same clean tag commit. Helpers build with plain
Cargo (`scripts/build_connect_artifacts.py`, never Maturin; target classes in
`scripts/inspect_connect_artifact.py::CONNECT_TARGETS`) and upload as
`connect-*` CI artifacts so they cannot mix with the wheel pipeline; the
aggregate job stages the reviewed `packaging/connect/` bootstraps next to
them, binds everything into the manifest `connect_artifacts` section
(`scripts/create_release_manifest.py --connect-artifact-dir`), validates
digests (`scripts/validate_release_artifacts.py --connect-artifact-dir`),
and publishes the exact bytes under `dist/publish/connect/` with
`SHA256SUMS`. An absent or empty `connect_artifacts` list is valid for a
proxy-only bundle: the checked-in `packaging/release/release-manifest.json`
example carries exactly three proxy artifacts and no `connect_artifacts` key.
Nothing is rebuilt in a publish job. The Windows helper is a
desktop-only asset and never implies Windows proxy support; the proxy matrix and its validators are unchanged, and
`validate_release_workflow.py` scopes the word “windows” to the single
helper build job so a helper binary can never read as proxy support.
macOS x86_64 was evaluated and deferred: no Intel runner exists to
execute-qualify that binary, and publishing an unexecuted binary would
violate the per-target qualification rule (Intel Mac operators build the
helper from source with `cargo build --bin eggpool-connect --release`).
See `docs/releasing.md` for the release/rollback operator boundary.

## Systemd Integration

The deployed unit lives at `deploy/eggpool.service`. CLI-rendered snippets
are owned by `rust/src/operations/deploy.rs` (`render_production_systemd` and
related renderers for personal/production layouts, logrotate, and cron). The
checked-in unit and the renderer output are related but not byte-identical
artifacts: the deployed file carries the production hardening set
(`Environment=HOME/PATH/PIPX_HOME/PIPX_BIN_DIR/EGGPOOL_LOG_FILE`,
`ProtectHome`, extended `ReadWritePaths`, `StartLimitInterval`/`StartLimitBurst`,
and the SIGHUP/reload comment), while renderers cover personal and production
variants with their own `ExecStart`/`ReadWritePaths` shapes. Do not copy a unit
from this document; read `deploy/eggpool.service` or regenerate via the CLI.

## Configuration

### `config.toml`

Runtime configuration. Selected sections (see `config.example.toml` and
`rust/src/config.rs` for the full contract):
- `[server]` — host (canonical default `0.0.0.0` for LAN access; the SBC
  profile deliberately overrides to loopback-only), port, and
  compatibility/diagnostic settings
- `[upstream]` — default upstream settings
- `[database]` — SQLite path, WAL mode
- `[routing]` — fairness mode/epsilon/scope, plus `[routing.wire_negotiation]` and `[routing.trace]`
- `[models]` — collapse_models, catalog withdrawal
- `[providers.<id>]` — per-provider config
- `[transcoder]` — protocol transcoding
- `[limits]` — request/media/token bounds
- `[pricing]` — accounting-only pricing catalogs
- `[readiness_probe]` — readiness gating
- `[model_info]` — source enablement
- `[update_checker]` — background freshness probes
- `[dashboard]` — theme, auth policy (public read-only by default; inference,
  integration, and runtime/update/status routes stay authenticated)
- `[metrics]` — buffering, flush modes
- `[security]` — header redaction and exact trusted reverse-proxy peers
- `[backup]` — automatic backup schedule
- `[model_overrides]` / `[model_capabilities]` / `[model_routers]` — per-model and semantic routing policy

### `.env`

API key storage. Never committed.

## Live Reload

`eggpool rehash` applies supported changes without restart:
- Control socket at `<runtime_dir>/eggpool.sock`, resolved by the native path helpers from `$EGGPOOL_RUNTIME_DIR`, suitable `$XDG_RUNTIME_DIR`, private state, and UID-scoped `/tmp` fallbacks. The server requires the runtime directory to be an owner-only `0o700` directory and the socket to be an owner-only `0o600` socket.
- Live vs. restart-required is owned solely by `rust/src/config_reload_policy.rs::classify_transition`. Mutation paths classify before atomic replacement and `rust/src/reload.rs` reclassifies server-side before generation publication; mixed transitions are wholly restart-required. Do not maintain a separate field list here.
- JSON output pinned at 9 keys

## Monitoring

### Dashboard

Self-updating HTML dashboard with 50 named theme files plus a built-in default (51 choices in `THEME_NAMES`):
- `/` — Overview
- `/models` — Model catalog
- `/runtime` — Live metrics
- `/cache` — Request shaping

### JSON API

Comprehensive stats endpoints under `/api/stats/`.

### CLI Diagnostics

- `eggpool accounts explain` — routing eligibility
- `eggpool modelinfo show/list/refresh` — model info
- `eggpool stats` — statistics commands
- `eggpool status` — concise proxy/provider health (one row per provider)
- `eggpool runtime-status` — detailed process/runtime diagnostics

### Manual SBC characterization

The existing `scripts/qualification_sbc.py` runner is the sole tooling
boundary for target-class evidence. It refuses non-Linux/non-aarch64 hosts or
hosts without a device-tree board model, uses a private temporary root and a
loopback-only provider, and records no request/response content or
credentials. Ordinary qualification (default, no `--benchmark-samples`) keeps
the Q008 `runtime-q008.v1` contract with the aggressive lifecycle fixture
(`tests/tooling/fixtures/qualification/sbc.toml`). The optional
`--benchmark-samples 30` mode must use the benchmark-only fixture
(`tests/tooling/fixtures/qualification/sbc-benchmark.toml`), which mirrors the
low-wear steady-state profile; it runs bounded native finite, native Responses
streaming, translated Responses-client streaming requiring downstream
`response.completed` plus fixture Messages-path proof, and fixed
client-concurrency-4 finite observations, with aggregate timing, process CPU,
RSS/VmHWM, database/WAL, cadence facts, and ownership-state snapshots under
the extended `runtime-q008.v2` contract. Run it three times
from fresh roots on the same physical board; it is descriptive and non-gating.
The diagnostic-only `--diagnose-finite-tail 10..=200` flag (default off,
requires benchmark mode) appends 60 sequential native finite requests with
monotonic provider-boundary phase timing plus a 30-request direct-provider
control, scalar-only with no p99. On Pi 5 it localized the finite tail to the
pre-provider durable publication / SQLite / storage path (Outcome 1): the
slowest request was pre-provider dominated in all three runs with a stable
direct control, and one RAM-backed temporary-root run removed the tail
entirely. A narrow database/publication follow-up is the only justified next
step; Tokio, routing-lock, and streaming keeps stand.

Plan 238 provides that follow-up's diagnostic tooling without changing the
runtime. `--diagnose-publication-storage 20..=200` uses the benchmark fixture
without the standard benchmark corpus, waits for task quiescence, records
bounded WAL-header/file-size and task-tick scalars, and preserves the same
provider-boundary phases plus direct-provider control. Supplying
`--diagnostic-database-dir DIR` places only the database, WAL, and SHM files
in a temporary child of `DIR`; the runner cleans it up and does not publish
the path. Run three fresh-root MMC passes first, then three database-only
temporary-filesystem passes if the tail reproduces. Treat missing physical
SBC or tmpfs dimensions as `not measured`; any runtime change requires a new
plan.

Plan 239's publication-phase mode is also qualification-only. Build a separate
candidate with the non-default `qualification-db-diagnostics` feature, run
`--diagnose-publication-phases` for the 60-request H0 baseline, and use
`--qualification-wal-autocheckpoint-pages 0` for H1. H2 at `256` is conditional
on the observed H0/H1 result. The feature is never enabled by release/package
workflows; its in-memory records and authenticated runtime projection are not
operator capabilities. Preserve the ordinary release candidate separately and
record unavailable physical dimensions as `not measured`.

Hosted ARM VMs, emulation, Rosetta/translation, and cloud ARM instances are
not physical SBC evidence. If the translated fixture route is not accepted or
a measurement dimension is unavailable, record `not measured`; do not weaken
the runtime capability contract. See [Plan 235](../plans/235-physical-sbc-target-class-benchmark-pass.md)
for the fixed corpus and historical numbers, and [Plan 236](../plans/236-physical-sbc-benchmark-evidence-corrective-pass.md)
for the corrected fixture/terminal/schema pass that supersedes Plan 235's
timing and translated-stream interpretation while preserving its
resource-convergence evidence. See [Plan 126](../plans/126-provider-backed-sbc-characterization.md)
for the earlier provider-backed closure.

## Backup

Automatic backup task (zip archives):
- Config files
- Database
- Scheduled via `[backup]` config
- Disabled in the copyable low-wear SBC profile unless explicitly enabled
- Runtime snapshot/archive work uses bounded native task scheduling

## Key Invariants

- `eggpool rehash` serializes reload transactions (one at a time)
- `reload_in_progress` uses the stable reload-busy exit code
- `eggpool connect`/`logout` don't silently restart
- Daemon mode is default for `eggpool serve`
- `--verbose` for foreground mode
