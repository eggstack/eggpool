# F003 Closure — Configuration and CLI Compatibility Foundation

Status: closed

Recommendation: closed

Implementation commit: [`5afbbdd`](https://github.com/eggstack/eggpool/commit/5afbbdd)

Plan: [Foundation Milestone F003](../../implementation/foundation/003-config-and-cli-compatibility.md)

Repository baseline inspected: `cdd57992` (`Close Rust migration foundation F002`)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| A — typed config contract | `rust/src/config.rs` defines Serde/TOML models for all inventoried top-level sections, nested provider/auth/proxy/wire/model-router/transcoder/model-info forms, and Python-compatible defaults; shipped `config.example.toml` and `config.sbc.example.toml` both load successfully | Pass |
| B — validation and redaction | Explicit validation covers fail-closed unknown/type errors, production ports, provider URLs and IDs, timeouts, proxy exclusivity, account credentials, auth/static-header conflicts, wire paths, static-model limits, model routers, cross-field limits, and forbidden request-content persistence; errors never include credential values | Pass |
| C — path/environment compatibility | `resolve_config_path` and its pure test form implement `--config` > `$EGGPOOL_CONFIG` > existing XDG config > CWD fallback; XDG config/data/state helpers and `.env` lookup are present; credentials resolve only at validation/dispatch-stage access | Pass |
| D — complete CLI parser | `rust/src/cli.rs` represents the reviewed Click command tree, nested subcommands, positional arguments, flags, defaults, and option placement; differential tests check root commands and option inventory against Python help | Pass |
| E — staged command behavior | `version`, `--help`, and `check-config` execute in Rust; every deferred command parses and returns typed `not implemented in Rust candidate` with exit 1, never silently succeeds or falls through to Python | Pass |
| F — differential corpus | `tests/migration_rs/test_f003_config_cli.py` covers version, representative valid/invalid configs, precedence, secret-safe failures, command inventory, option inventory, unknown command, missing argument, and deferred behavior | Pass |
| G — dependency and production boundary | Serde, TOML, and SHA-256 were added only for this config/CLI boundary; no Python entry point, installed executable, database, provider network, server, or downstream command implementation was changed | Pass |

## Differential and compatibility results

The F002 black-box harness ran both explicit implementations by separate
executable identities. Results included:

- Python and Rust `version` matched at `0.7.4` with exit 0 and identical output.
- A representative configuration containing provider auth, static headers,
  wire surfaces, a legacy-safe proxy declaration, an account, a model router,
  and transcoder/model-info settings was accepted by both implementations.
- The invalid port corpus was rejected by both with exit 1 and the Rust result
  classified as `schema`.
- Missing provider credentials returned a typed non-zero failure without
  printing the environment variable name or secret value.
- All inventoried root commands and representative nested option groups were
  present in Rust help and Python help. Unknown commands and missing option
  arguments retained exit code 2.
- The shipped standard and SBC configuration templates both passed Rust
  `check-config`.

## Verification commands actually run

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings    PASS
cargo test --manifest-path rust/Cargo.toml                                   PASS (6 tests)
cargo build --manifest-path rust/Cargo.toml                                  PASS
uv sync --frozen --extra ci                                                   PASS
uv run ruff format --check src/ tests/ scripts/                              PASS
uv run ruff check src/ tests/ scripts/                                        PASS
uv run pyright src/ scripts/                                                  PASS (0 errors)
uv run pytest tests/migration_rs -q --tb=short --maxfail=1                    PASS (20 tests)
uv run pytest tests/unit/test_config.py tests/unit/test_config_validation_extended.py tests/unit/test_contract.py tests/unit/test_contract_urls.py tests/unit/test_deploy_user.py tests/smoke/ -q --tb=short --maxfail=1 PASS (309 tests)
```

## Security, restart, and contention evidence

Config reads and validation are local and synchronous. They do not write
config files, start servers, contact providers, or invoke deferred commands.
Errors are deliberately generic at the deserialization boundary and redact
credential values. No runtime rehash, database transaction, restart, or
contention behavior was pulled into F003; those remain owned by later plans.
The Rust process retains `#![forbid(unsafe_code)]`.

## Known limitations and unresolved findings

The Rust candidate does not yet implement deferred command internals, database
access, provider networking, rehash, or the service runtime. Those are
explicitly staged for later milestones and are represented in the parser only.
The migration candidate remains development-only; Python remains the
production implementation. No unresolved findings by severity.

## Planning follow-through

F003 is removed from the dependency-ready section of `migration-rs/registry.md`
and recorded in the completed section. F004 remains dependency-ready. F005's
config dependency is now satisfied, but F005 remains blocked on the F004
read-interface required for DB-backed pages; no future plan can be fully
unblocked by F003 alone.
