# O004 Closure — Config, Key, Provider Onboarding, and Live-Apply Mutations

Status: closed

Implementation commit: [`3d1b63c`](https://github.com/eggstack/eggpool/commit/3d1b63c)

Plan: [O004 — config, key, provider onboarding, and live-apply mutations](../../implementation/operations/004-config-key-provider-onboarding-and-live-apply.md)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| Narrow mutation boundary | `rust/src/operations/config_mutation.rs` owns bounded reads, line-preserving edits, provider templates, key handling, account selection/removal, typed apply outcomes, and no CLI/runtime duplicate mutation engine | Pass |
| `init-config` | Bundled `config.example.toml` is validated before atomic replacement; overwrite requires `--force`; new files receive restrictive creation mode | Pass |
| `edit` | `$EDITOR`, `$VISUAL`, then `hx`/`vim`/`vi`/`nano` resolution uses direct process execution with the config path as an argument; missing editor and non-zero editor exit are distinct errors | Pass |
| `set` | `host` and `port` retain the frozen Python surface, preserve unrelated text/comments, reject invalid values before replacement, and use the existing restart authority | Pass |
| Server key commands | `getkey` is the only ordinary read surface; `newkey` uses OS cryptographic entropy, preserves `api_key_env`, redacts old keys by default, and supports explicit `--show-old` output | Pass |
| Provider templates/connect | Bundled and custom TOML templates load structurally, the default provider remains available for custom files, local instances can choose an id/base URL, existing providers append accounts, and new providers retain wire/proxy/template fields | Pass |
| `connect list` | Provider status, recommendation, notes, and configured/default priorities are rendered without credentials or network probes | Pass |
| `logout` | Matching supports provider id (including normalized id), account name, env identifier, and exact configured key; ambiguous selection is interactive and removed secrets are never printed | Pass |
| `onboard` | Onboarding composes config creation, server-key readiness, safe loopback binding, connect, validation, live-apply reporting, already-running handling, and final server start | Pass |
| `dashboard public` | Toggle and explicit on/off semantics edit only `[dashboard].public`; the command never constructs or prints a secret-bearing URL and uses the mutation restart policy | Pass |
| Atomicity and preservation | Candidate bytes are validated before temp-file sync/rename; original mode is retained; temp cleanup occurs on write failure; invalid-port and unrelated-content tests prove no replacement on rejected input | Pass |
| Live apply | `ApplyOutcome` distinguishes stopped server, applied/no-op rehash, restart-required, healthy control-unavailable, rehash failure, and restart success; live changes call the O002 control client, while explicit restart commands use O003 process authority | Pass |
| Concurrency and secret redaction | A process-local mutation lock serializes read/modify/validate/replace; `AccountMatch` has a custom redacted `Debug` implementation; terminal API-key entry disables echo on Unix | Pass |

## Mutation and output matrix

| Surface | Durable effect | Runtime effect | Secret output |
|---|---|---|---|
| `init-config` | Atomic bundled template write | None | None |
| `edit` | Editor-owned file change | None | Editor controls its own display |
| `set host/port` | Narrow `[server]` assignment | Restart only when a PID-owned server exists | None |
| `getkey` | None | None | Current key only on explicit command surface |
| `newkey` | Inline key replacement, or no inline write when env-owned | Restart only when a PID-owned server exists | New key; old key redacted unless `--show-old` |
| `connect` / `logout` | Provider/account fragment mutation | Live rehash or explicit control-unavailable/stopped outcome | No key material |
| `onboard` | Composition of the above | Live apply per provider and final start if stopped | No key material beyond explicit provider prompt input |
| `dashboard public` | `[dashboard].public` toggle | Restart only when a PID-owned server exists | None |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml                                  PASS
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings   PASS
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1 PASS (5)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1 PASS (7)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1 PASS (2)
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1 PASS (403 across 46 suites)
rtk proxy uv run pytest tests/unit/test_connect.py tests/unit/test_onboard.py tests/unit/test_init_config.py tests/unit/test_cli_dashboard_config.py tests/unit/test_connect_apply_outcome.py tests/integration/test_e2e_key_flow_and_config.py tests/integration/test_connect_logout_fallback.py -q --tb=short --maxfail=1 PASS (221)
rtk proxy uv run pytest tests/migration_rs -q --tb=short --maxfail=1 PASS (100 passed, 3 skipped)
rtk proxy uv run ruff format --check src/ tests/ scripts/ PASS (732 files)
rtk proxy uv run ruff check src/ tests/ scripts/ PASS
rtk proxy uv run pyright src/ scripts/ PASS (0 errors, 0 warnings)
rtk proxy uv run pytest tests/smoke/ -q --tb=short --maxfail=1 PASS (14)
rtk git diff --check PASS
```

The direct command smoke pass also verified fresh template creation, 64-character
hex key generation/readback, env-owned key preservation, stopped-server
mutation behavior without an implicit start, provider connect/check-config,
dashboard mutation, and final-account logout/check-config convergence in
isolated temporary roots.

## Failing-before/passing-after evidence

Before O004, the Rust dispatcher returned migration-stage `NotImplemented` for
all O004-owned command handlers. After O004, those commands dispatch to Rust
services and the focused Rust target covers atomic edits, dashboard insertion,
key entropy/env ownership, custom/bundled template loading, account matching,
and secret-safe debug output. The first manual pass exposed an unsafe
mutation-restart behavior that could start a server with no PID-owned process;
the implementation was corrected before the final verification and now returns
the frozen stopped-server outcome without spawning.

## Dependency, schema, and security review

O004 adds no database schema, migration, network/provider probe, RPC surface, or
Python fallback. It extends the already direct `nix 0.31.3` dependency with
its `term` feature solely for safe Unix terminal echo suppression and promotes
the already locked `getrandom 0.3.4` crate to a direct dependency for API-key
entropy. All other mutation, atomic-write, prompt, and restart work uses
existing standard-library/O002/O003 facilities. The implementation remains
under the crate's `unsafe_code = "forbid"` boundary.

Credentials are bounded to explicit command output or the interactive input
boundary. They are not included in errors, control requests, apply outcomes,
template metadata, debug output, or logs. Inline/env ownership is preserved,
temporary files are private, original files are never truncated first, and
failed validation leaves the original bytes untouched.

## Unresolved findings and non-goals

None for O004 acceptance. `configsetup`, database/backup/recovery, operator
inspection, update, deployment, and M9-wide qualification remain owned by
O005-O010. Broad OS/SBC qualification, Rust-default distribution, and Python
retirement remain M10-M12 work. Later defects must use a new corrective plan;
this closure record is append-only.

## Planning transition

O004 is removed from the dependency-ready section and recorded as completed in
the implementation index, registry, roadmap, handoff sequence, and this
accepted closure record. O005 is promoted to the sole dependency-ready plan
because its hard dependency is now accepted. O006-O010 remain queued behind
their direct predecessors. M9 remains active, and M10 remains blocked on
accepted O010 closure plus its separate planning review.
