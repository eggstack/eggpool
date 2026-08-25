# Deep Dive: Core Application Modules

Back to [Overview](overview.md)

## Purpose

The core modules form the foundation of EggPool — CLI entry points, configuration management, error handling, constants, and the JSON backend abstraction.

## Modules

### `cli.py` — CLI Bootstrap (~74 lines)

Tiny entry point that dispatches fast-path commands before importing Click:

```
main() → fastcli.maybe_run_fast_command() → unrecognized → cli_full (Click)
```

- Fast-path commands: `croncheck`, `ensure-running` (stdlib-only, no Click import)
- Unrecognized commands fall through to `cli_full.py` (heavy Click CLI)
- Public symbols lazily forwarded via PEP 562 `__getattr__`

**Key invariant**: `fastcli` and `runtime_paths` must stay stdlib-only for the Raspberry Pi watchdog contract.

### `cli_full.py` — Click CLI Commands

Heavy Click-based CLI with all operational commands:
- `eggpool serve` — start daemon
- `eggpool stop` / `eggpool restart` — process management
- `eggpool rehash` — live config reload
- `eggpool connect` / `eggpool logout` — provider management
- `eggpool update` — PyPI update check
- `eggpool accounts explain` — routing diagnostics
- `eggpool modelinfo show/list/refresh` — model info inspection
- `eggpool stats` — statistics commands
- `eggpool configsetup` — integration config generation

### `cli_exit_codes.py` — Stable Exit Codes

Constants for CLI exit codes (e.g., `EXIT_RELOAD_BUSY = 4`). Used everywhere instead of magic numbers.

### `cli_rehash_format.py` / `cli_rehash_helper.py`

Shared rehash output formatting and validate-and-rehash logic used by multiple CLI commands.

### `config.py` — Config File Helpers

Small module (~57 lines) for ensuring config files exist and loading them.

### `config_validation.py` — Validation Contract

Reusable validation used by `check-config` and `rehash`. Returns typed `ConfigValidationError` subclasses:
- `ConfigFileAccessError`
- `ConfigParseError`
- `ConfigSchemaError`
- `ConfigStartupAuthError`
- `ConfigAccountCredentialError`
- `ConfigInternalError`

Never raises `SystemExit` — errors are returned as structured objects.

### `config_reload_policy.py` — Live Reload Classification

Typed configuration diff and reload policy:
- `LIVE` fields: provider/account/routing/model-override families, `[transcoder]`, subset of `[models]`, retention durations
- `RESTART_REQUIRED` fields: everything else
- `_FIELD_DISPOSITION` map is the single source of truth
- `eggpool rehash` JSON output pinned at 9 keys

### `config_utils.py` — Configuration Utilities

Shared helpers for CLI and integration config generation.

### `constants.py` — Project-Wide Constants

Ports, paths, limits, timeouts — all named constants instead of magic numbers.

### `errors.py` — Exception Hierarchy

```
AggregatorError (base)
├── ConfigError
├── ConfigValidationError
│   ├── ConfigFileAccessError
│   ├── ConfigParseError
│   ├── ConfigSchemaError
│   ├── ConfigStartupAuthError
│   ├── ConfigAccountCredentialError
│   └── ConfigInternalError
├── DatabaseError
├── UpstreamError (has status_code)
│   ├── TemporaryUpstreamError
│   ├── TransientUpstreamError
│   ├── AuthenticationError
│   ├── QuotaExhaustedError
│   ├── RateLimitError (has retry_after)
│   └── ModelUnavailableError
├── ProxyError
├── ModelNotFoundError (has model_id)
├── NoEligibleAccountError
├── CatalogUnavailableError
├── AuthenticationUnavailableError
├── UpstreamExhaustedError
├── AccountSuspendedError
├── RequestTooLargeError
├── ModelInfoSourceFetchError
├── ContextLimitExceededError
└── CapabilityError (has model_id, capability, requested_fields)
```

- `CapabilityError` is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)
- `BudgetResolutionError` is a subclass of `CapabilityError`
- Chain exceptions with `raise ... from err` or `raise ... from None`

### `jsonx.py` — JSON Backend Abstraction

Hot-path JSON serialization/parsing behind a small helper:
- **Preferred backend**: `orjson` (install with `uv pip install 'eggpool[fast]'`)
- **Fallback**: stdlib `json` (identical compact-separator wire behaviour)
- Override at runtime: `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`
- Active backend logged at startup (`json_backend=orjson|stdlib`)
- Used by: wire bodies, SSE frame helpers, shared streaming SSE framing, request-path body parses
- Tests parametrised across both backends

**Important**: Off the request path, stdlib `json` is allowed for deterministic hashing and persisted diagnostic metadata.

### `auth.py` — Local API Key Authentication

Constant-time API key comparison to prevent timing attacks. Used for local API authentication.

### `logging.py` — Structured Logging

Structured logging setup for the application.

### `onboard.py` — Interactive Onboarding

Interactive setup script for first-time configuration.

### `toml_edit.py` — Formatting-Preserving TOML Edits

Small helper for editing scalar TOML section values while preserving formatting.

### `deploy_user.py` — Deploy User Resolution

`DeployUser` and `resolve_config_path()` for resolving deployment user and configuration paths.

### `update_checker.py` — PyPI Update Checker

Two paths:
- **`UpdateChecker`**: background/periodic probe, caches latest `UpdateInfo`
- **`async_check_for_update()`**: bare CLI one-shot, performs the freshness-aware live latest lookup (never reads `UpdateChecker.snapshot()`)
- **`normalize_requested_version()` / `check_exact_release()`**: validate and verify an optional exact `VERSION` through PyPI's release endpoint; exact installs are pinned and verified before restart

CLI helper uses freshness-aware lookup with cache-bust tokens and double-fetch for stale CDN protection. Version comparison via `is_newer_version()` (PEP 440 compliant).

## Key Invariants

- `fastcli` and `runtime_paths` are stdlib-only — no transitive imports
- `ConfigValidationError` subclasses never raise `SystemExit`
- `reload_in_progress` exits with code 4 (`EXIT_RELOAD_BUSY`)
- `eggpool update` must make a live PyPI lookup, never consult `UpdateChecker.snapshot()`; explicit versions must not silently fall back to latest
- JSON backend tests are parametrised across both orjson and stdlib
