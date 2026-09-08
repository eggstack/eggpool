# O001 Closure — Operational CLI Contract and Deterministic Oracle Freeze

Status: closed

Recommendation: closed; O002 is dependency-ready.

Implementation commit: [`db3a110`](https://github.com/eggstack/eggpool/commit/db3a11085689f608a64c95079c5481b45b1d9911)

Plan: [O001 — Operational CLI contract and deterministic oracle freeze](../../implementation/operations/001-operational-cli-contract-and-oracle-freeze.md)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| Current Python/Rust source audit and F003 delta | `operations-contract.md` records the audited modules, current package layout, and the three parser deltas (`dashboard public --off`, paired stats `--apply`) without rewriting F003 history | Pass |
| Complete CLI parser/presentation ownership | `fixtures/operations/o001-fixture-matrix.json` has 63 current Click paths, exact option lists, one owner for every path, and the global `--config` rule; `test_current_python_commands_have_one_owner_and_exact_options` derives the Python tree from Click | Pass |
| Rust parser compatibility boundary | `rust/tests/cli_contract.rs` compares every frozen path/option list and verifies current delta flags and mutual exclusion | Pass |
| Runtime paths/process state | Contract and `test_runtime_paths_and_config_precedence_stay_isolated` cover XDG/explicit path precedence, PID/log/socket names, PID content, root isolation, and no host state | Pass |
| Local control bytes/effects | Contract records protocol 1, fields, framing, limits, timeout, stale-socket safety, and negative cases; `test_control_contract_rejects_bad_frames_without_calling_reload` proves invalid frames do not call the handler | Pass |
| Config/provider mutation and integration boundaries | Contract freezes env-backed key preservation, explicit secret surfaces, atomic logical mutation, restart/live-apply outcomes, all 11 integration targets, overwrite/clipboard/model rules; executable env-key and safety observations cover the mutation seam | Pass |
| Database/backup/recovery safety | Contract and `test_backup_archive_is_allowlisted_and_fixed_targeted` freeze ZIP format, META, stored members, fixed-target traversal classification, SQLite snapshot semantics, private restore modes, atomic publication, and retention | Pass |
| Operator inspection and deployment | Ownership table assigns all inspection, stats, dashboard, deploy, and uninstall paths; deterministic systemd/watchdog renderer and fake-command evidence cover deployment without host mutation | Pass |
| Update behavior and backend boundary | Contract and local fake-release test cover version normalization, latest/exact/check/failure semantics, no config/DB overwrite, restart condition, and the permitted Rust artifact backend difference | Pass |
| R008 deferred task contract | Observation fixture records exactly `metrics_flush`, `update_checker`, and `automatic_backup`; inventory test proves uniqueness, process ownership, single M8 supervisor, and no fourth capability | Pass |
| Secret/redaction/temp-root/destructive-command guards | Fixture scans reject secret-shaped and host-specific values; observations contain symbolic paths only; deployment evidence uses builders/fakes and no real systemctl/cron/logrotate mutation | Pass |

## Frozen fixture counts

- 63 command/group paths with 100% ownership coverage.
- 2 checked-in O001 JSON fixtures: one command/safety matrix and one Python
  observation projection.
- 8 matrix safety cases.
- 11 focused O001 Python tests and 3 Rust `cli_contract` tests.
- 3 deferred R008 capabilities, all unique; 6 total current Python runtime
  inventory entries, with the other 3 already owned by R008/M8.
- 11 integration target observations.

## Verification commands actually run

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1 PASS (3 tests)
uv run pytest tests/migration_rs -q --tb=short --maxfail=1                   PASS (103 tests)
uv run pytest tests/unit/test_cli*.py tests/unit/test_runtime*.py tests/unit/test_update*.py -q --tb=short --maxfail=1 PASS (450 tests)
uv run ruff format --check src/ tests/ scripts/                             PASS (732 files)
uv run ruff check src/ tests/ scripts/                                       PASS
uv run pyright src/ scripts/                                                 PASS
git diff --check                                                             PASS
```

The full Rust workspace suite was also launched during qualification; its
completed portions were green, including the 19 library tests and the
compiled O001 target. It was not used as the O001 acceptance gate because the
required Rust command-contract target is the scoped `cli_contract` test.

## Security and redaction review

No real credentials, proxy URLs, HOME paths, temporary paths, provider
addresses, archive payload secrets, or unbounded subprocess output are stored
in the fixtures. Runtime paths are asserted only inside pytest temporary roots
and are never serialized. Backup traversal-looking names are classified as
fixed-target aliases; no archive member is extracted as an arbitrary path.
Deployment observations render snippets and fake command inputs only. No paid
provider, network release service, root service manager, or production data was
used.

## Backend-difference decisions

The Python oracle's current package-manager command construction remains
historical evidence for O008. Rust is permitted to use a staged, verified
native artifact backend because M11 changes distribution authority. Rust may
also replace Python string helpers with typed renderers. These differences do
not change command names/options, user-visible result categories, secret
handling, config/database preservation, atomicity, or restart semantics.

## Unresolved findings

None. The three F003 parser deltas were explicit and resolved in the Rust
parser and fixture. No architecture ambiguity remains that would require
O003-O009 to redesign a shared boundary. M10 platform/SBC qualification and
M11 public Rust distribution cutover remain intentionally out of scope.

## Planning transition

O001 is removed from the dependency-ready section and recorded in the
completed-plan table. O002 is the only future plan promoted: its hard
dependency, accepted O001, is now closed. O003-O010 remain queued behind their
direct predecessors; O001 does not independently unblock them. M9 remains
active and M10 remains gated on the later accepted O010 closure.
