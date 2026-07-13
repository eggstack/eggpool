# Live Configuration Rehash — Milestone A

## Validation, Diffing, and Fail-Closed CLI Foundation

## Objective

Build the validation and policy foundation required for safe live configuration reloads without yet changing the running application's object graph. At the end of this milestone, `check-config` and `rehash` share one reusable validation implementation, every rehash attempt is filtered through that validation contract, configuration changes are explicitly classified as reloadable or restart-required, and failures are represented by typed results rather than Click exits or ad hoc strings.

This milestone must not introduce a partially functional live reload. Until milestone C activates the runtime control path, `rehash` should clearly report that live reload infrastructure is not yet available or remain gated behind an internal feature switch. It must not silently restart the process; `eggpool restart` is the explicit hard-restart command.

## Safety contract

The following behavior is mandatory:

- local `check-config` validation runs before `rehash` contacts any live process;
- a validation failure exits nonzero and performs no control-plane call;
- invalid configuration cannot trigger stop, restart, database mutation, client construction, or app-state mutation;
- warnings do not block reload unless they are promoted to errors by a future explicit strict mode;
- server-side code can invoke the same validation API without importing Click or receiving `SystemExit`;
- secrets are not included in digests, diff output, exceptions, logs, or serialized results;
- fields not explicitly classified as live-reloadable default to restart-required.

## Workstream A1 — Extract reusable configuration validation

Create a dedicated module, for example `eggpool/config_validation.py`, that owns the complete validation contract currently embedded in `cli_full.check_config`.

Define typed outputs:

```python
@dataclass(frozen=True)
class ConfigValidationWarning:
    code: str
    message: str
    section: str | None = None


@dataclass(frozen=True)
class ConfigValidationResult:
    config: AppConfig
    source_path: Path
    content_digest: str
    warnings: tuple[ConfigValidationWarning, ...]
```

Define typed failures, either under the existing `AggregatorError` hierarchy or a dedicated validation hierarchy:

- file access/read failure;
- TOML parse failure;
- model/schema validation failure;
- startup authentication failure;
- account credential failure;
- inconsistent path/read failure;
- internal validation failure.

The reusable function should accept a resolved path and avoid printing:

```python
def validate_config_file(path: str | Path) -> ConfigValidationResult:
    ...
```

Implementation requirements:

1. Resolve the path consistently with existing CLI behavior.
2. Read bytes once for digesting and parsing where practical.
3. Compute SHA-256 over the exact candidate bytes.
4. Parse `AppConfig` using an API that guarantees the validated model corresponds to those exact bytes. If `AppConfig.from_toml(path)` must remain, guard against path mutation and document the residual race until server digest confirmation is implemented.
5. Invoke `require_auth_at_startup(config.server.resolved_api_key)`.
6. Invoke `config.validate_account_credentials()`.
7. Move stale contract checks to a reusable non-CLI helper that returns typed warnings.
8. Return a fully validated immutable result.

Do not broaden `check-config` into network credential verification unless that already exists. Preserve current semantic scope and add stricter validation only through separately documented work.

## Workstream A2 — Refactor the `check-config` command

Change `cli_full.check_config` into a thin presentation adapter around `validate_config_file()`.

Preserve useful existing output:

- loaded path;
- server host/port;
- account count;
- database path;
- warnings and warning count.

Preserve nonzero exit behavior on failure, but centralize formatting so tests can assert stable operator messages.

Recommended error prefix:

```text
Error: configuration validation failed: <specific cause>
```

The function should not duplicate any parse, auth, credential, or warning logic.

## Workstream A3 — Add normalized configuration fingerprinting

The raw file digest is needed for time-of-check/time-of-use protection. A normalized, secret-safe fingerprint is useful for diagnostics and no-op detection.

Add two distinct concepts:

- `content_digest`: SHA-256 of exact file bytes, used only to ensure the server applies what the CLI validated;
- `runtime_fingerprint`: deterministic hash of a redacted/canonical representation, used for diagnostics and semantic no-op detection.

Requirements:

- never serialize raw credentials into logs or result payloads;
- do not use Python's process-randomized `hash()`;
- canonicalize ordering for mappings and account/provider collections where order is not semantically meaningful;
- document fields intentionally omitted from the runtime fingerprint;
- test deterministic output across repeated parses.

If a safe canonical redaction mechanism is not straightforward, defer the normalized fingerprint and rely on the exact content digest for milestone A. Do not create a misleading fingerprint that can collide because it omits behaviorally relevant values.

## Workstream A4 — Typed configuration diff and reload policy

Create a module such as `eggpool/config_reload_policy.py` containing:

```python
class ReloadDisposition(Enum):
    LIVE = "live"
    RESTART_REQUIRED = "restart_required"
    IGNORED = "ignored"


@dataclass(frozen=True)
class ConfigChange:
    path: str
    disposition: ReloadDisposition
    old_display: str
    new_display: str
    section: str
    secret: bool = False


@dataclass(frozen=True)
class ConfigDiff:
    changes: tuple[ConfigChange, ...]

    @property
    def restart_required(self) -> tuple[ConfigChange, ...]: ...
```

Policy requirements:

- classify every current `AppConfig` field;
- make the classification reviewable in one place;
- default unknown/new fields to restart-required;
- redact secret values as `changed` rather than showing old/new content;
- avoid comparing derived or runtime-only fields twice;
- distinguish semantically irrelevant ordering changes from actual changes;
- produce deterministic ordering for messages and tests.

Initial restart-required fields must include at least:

- `server.host`;
- `server.port`;
- `server.threads`;
- server options passed into Granian construction;
- database path and worker-thread settings;
- CORS origins and trusted hosts while middleware is constructor-owned;
- body-size settings while middleware captures them at app construction;
- process/deployment path settings;
- any unclassified field.

Initial live-reloadable candidates should be explicitly listed and reviewed against actual consumers. Do not mark a field live merely because it appears easy to mutate. It is live only if milestone B/C will replace every derived consumer coherently.

## Workstream A5 — Structured reload result and error taxonomy

Define protocol-neutral types that milestone C can serialize over the control socket:

```python
class ReloadStage(Enum):
    VALIDATION = "validation"
    DIGEST_CHECK = "digest_check"
    DIFF = "diff"
    PREPARATION = "preparation"
    RECONCILIATION = "reconciliation"
    COMMIT = "commit"
    RETIREMENT = "retirement"


@dataclass(frozen=True)
class ReloadResult:
    ok: bool
    stage: ReloadStage
    generation: int | None
    changed_sections: tuple[str, ...]
    warnings: tuple[ConfigValidationWarning, ...]
    restart_required: tuple[ConfigChange, ...]
    message: str
```

Ensure result construction cannot accidentally include config models or secret values.

## Workstream A6 — Change `rehash` preflight semantics

Replace the current unconditional delegation to `restart`.

Required CLI flow:

1. Resolve config path.
2. Run `validate_config_file()`.
3. If validation fails, print a clear failure stating the live configuration is unchanged and exit nonzero.
4. If validation succeeds, preserve the result and digest for the future control-plane request.
5. Until the control channel exists, return an explicit message such as `Live rehash control is not yet available; run eggpool restart to apply process-bound changes.` or keep the new implementation behind an internal feature flag used only by tests.

Do not invoke `restart` automatically. This is important because the roadmap's user-facing distinction is that `rehash` is safe and fail-closed, while `restart` is the deliberate disruptive operation.

The final Milestone C implementation will replace the temporary unavailable path with the control client.

## Workstream A7 — Tests

Add focused unit and CLI tests.

### Validation tests

- valid configuration produces typed result;
- malformed TOML;
- schema/type failure;
- startup auth failure;
- account credential failure;
- missing/unreadable file;
- warning-only config succeeds;
- digest equals exact bytes read;
- validation helper never raises `SystemExit`;
- CLI renders helper failures and exits nonzero.

### Rehash preflight tests

- invalid config does not invoke restart;
- invalid config does not invoke future control-client seam;
- valid config reaches the post-validation seam with the correct digest;
- warning output is retained;
- no automatic hard-restart fallback occurs.

### Diff-policy tests

- no changes;
- each restart-required field is classified correctly;
- representative live fields are classified correctly;
- unknown synthetic field or policy gap fails closed;
- secrets are redacted;
- collection ordering does not create false changes where order is irrelevant;
- deterministic result ordering.

### Regression tests

- existing `check-config` tests continue to pass or are updated for intentionally improved messages;
- `restart` retains its current explicit behavior;
- CLI lazy import/fast-path constraints remain intact;
- strict Pyright and Ruff pass.

## Deliverables

- shared validation module;
- reusable stale-contract warning helper;
- refactored `check-config` command;
- exact candidate digest support;
- typed reload policy and diff output;
- structured reload result/error types;
- fail-closed `rehash` preflight with no implicit restart;
- complete tests and operator-facing messages;
- short developer documentation describing field-classification maintenance.

## Acceptance criteria

- There is exactly one implementation of parse/auth/account validation used by both `check-config` and `rehash`.
- Running `eggpool rehash` against malformed or invalid configuration cannot stop or restart the running process.
- Invalid rehash preflight explicitly states that the live configuration remains unchanged.
- No Click dependency or `SystemExit` exists in reusable validation code.
- Every current configuration field has an explicit reload disposition or is caught by a fail-closed default.
- Secret-bearing changes are never printed.
- `rehash` no longer delegates directly to `restart`.
- Unit, CLI, type, and lint suites pass.

## Handoff notes

Milestone B should consume `ConfigValidationResult`, `ConfigDiff`, and `ReloadResult` directly. Avoid redesigning these types inside the runtime manager unless implementation reveals a concrete ownership issue. Keep the validation API synchronous unless parsing becomes materially expensive; runtime orchestration can move it to a worker thread if necessary without changing semantics.
