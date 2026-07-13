# Live Configuration Rehash Roadmap

## Status

Proposed implementation roadmap for replacing the current `eggpool rehash` hard restart with a validated, transactional, zero-interruption configuration refresh.

## Problem statement

`eggpool rehash` currently delegates directly to `restart`, which terminates the Granian supervisor and worker before starting a replacement process. This applies configuration changes, but it also interrupts active requests and streams, clears process-local state, performs crash recovery on startup, and creates avoidable downtime.

The desired behavior is a true runtime configuration refresh:

1. Read the configured TOML file.
2. Apply the complete `check-config` validation contract as a mandatory preflight filter.
3. Refuse the refresh if validation fails, print a useful error, and leave the current runtime untouched.
4. Determine whether the proposed changes are live-reloadable or require a process restart.
5. Prepare a complete candidate runtime generation without exposing it to traffic.
6. Atomically direct new requests to the candidate generation.
7. Allow in-flight requests and streams on the previous generation to finish normally.
8. Retire the previous generation and close its resources only after it drains.

A broken configuration must never be able to replace or destabilize the active configuration. Validation failure, candidate construction failure, persistence reconciliation failure, or restart-required changes must all fail closed.

## Current-state observations

The current CLI command is only an alias:

```python
@cli.command()
@click.pass_context
def rehash(ctx: click.Context) -> None:
    """Restart the server to apply configuration changes."""
    ctx.invoke(restart, timeout=10.0)
```

`check-config` already defines the operator-visible validation contract:

- parse and model validation through `AppConfig.from_toml()`;
- startup authentication validation through `require_auth_at_startup()`;
- account credential validation through `config.validate_account_credentials()`;
- stale provider-contract warning generation.

The application currently loads configuration once during app construction and stores it on `app.state.config`. Lifespan startup then constructs long-lived services and background-task closures that capture that startup configuration. Assigning a new `AppConfig` object alone would therefore not reload routing, provider clients, quota behavior, background schedules, middleware, or other derived runtime state.

The implementation must introduce explicit process-lifetime and runtime-generation boundaries rather than mutating the current object graph in place.

## Core safety invariants

The implementation is not complete unless all of the following remain true:

- `check-config` semantics are the mandatory first gate for every rehash attempt.
- The live server independently repeats validation before preparing a candidate, even when the CLI already validated the file.
- Validation failure performs no database reconciliation, client construction, task replacement, or active-state mutation.
- A candidate configuration is never partially applied.
- Process-bound changes are reported as restart-required and do not cause a partial live reload.
- Only one reload transaction may run at a time.
- Requests admitted before the commit continue on their original generation.
- Requests admitted after the commit use the new generation.
- Active streams are not cancelled and their HTTP clients are not closed during generation replacement.
- The database remains process-owned in the initial implementation; changing its path or worker topology requires restart.
- Any failure before atomic commit leaves the old generation active.
- Any cleanup failure after atomic commit is logged and surfaced, but does not roll back a successfully activated generation.
- The PID and listening socket remain unchanged during successful rehash.

## Architectural target

### Shared validation service

Extract the validation logic currently embedded in the Click command into a reusable application-layer function. Both `check-config` and reload orchestration must call this function.

Suggested shape:

```python
@dataclass(frozen=True)
class ConfigValidationResult:
    config: AppConfig
    warnings: tuple[str, ...]
    source_path: Path
    content_digest: str


def validate_config_file(path: str | Path) -> ConfigValidationResult:
    ...
```

The function should:

- resolve and read the exact file;
- compute a SHA-256 digest of the bytes being validated;
- parse `AppConfig` from those bytes or from a controlled temporary representation;
- apply startup auth validation;
- apply account credential validation;
- produce stale-contract warnings;
- return a typed result without printing or exiting.

The Click command remains responsible for rendering output and exit codes. Runtime code must not invoke Click commands or depend on `SystemExit` for control flow.

### Runtime generation model

Introduce a `RuntimeManager` that owns an immutable active `RuntimeGeneration` reference. A generation contains configuration-derived services used by the request path, such as:

- validated `AppConfig`;
- account registry;
- provider client pool;
- outbound client manager or generation-specific outbound configuration;
- router and quota estimator;
- request coordinator;
- health manager;
- catalog and model-info configuration/services where generation ownership is appropriate;
- generation-owned background-task supervisor;
- generation identifier and lifecycle metadata.

Process-owned resources should remain outside the generation:

- FastAPI/ASGI application and bound listener;
- primary database and statistics database connections;
- migrations and repositories whose connection identity must remain stable;
- control channel;
- runtime manager and reload lock;
- process-wide observability primitives that must survive generation swaps.

Each request obtains a generation lease at request entry. The lease increments the generation's active request count and decrements it in `finally`. Streaming responses must hold the lease until the response iterator is closed, not merely until the route handler returns.

### Transactional reload pipeline

The server-side reload transaction should use explicit stages:

1. **Read and validate**: run the shared `check-config` validation contract against the server's configured path.
2. **Digest confirmation**: optionally compare a CLI-supplied digest to eliminate a validation/application time-of-check/time-of-use race.
3. **Diff and classify**: compare candidate and active configurations using a typed field policy.
4. **Reject restart-required changes**: return a structured list without changing live state.
5. **Prepare candidate**: construct all generation-owned services, reconcile persistence transactionally, and run readiness checks.
6. **Atomic commit**: swap the active generation under the reload lock.
7. **Activate scheduling**: start candidate generation background tasks and prevent the retiring generation from scheduling new ticks.
8. **Drain**: allow old generation request leases to reach zero.
9. **Retire**: close old pools, clients, and generation-owned services.
10. **Record result**: expose generation number, duration, changed sections, warnings, retirement status, and errors.

The implementation must define a narrow atomic commit point. Before it, all failures preserve the old generation. After it, the new generation is authoritative and old-generation cleanup is best effort with explicit diagnostics.

## Reloadability policy

Create a typed registry or explicit comparator that classifies every configuration field. Avoid an unstructured dictionary diff whose behavior can silently drift as `AppConfig` grows.

### Initially live-reloadable

Expected candidates include:

- provider definitions, URLs, protocols, headers, authentication material, and model endpoint contracts;
- provider accounts, credentials, quotas, and enabled state;
- routing weights, policy, stale thresholds, and retry selection behavior;
- upstream request timeout and retry/backoff policy where consumed by generation-owned request services;
- transcoding policy;
- compression and semantic cache policy;
- model mappings and catalog behavior;
- health policy and account backoff thresholds;
- model-info refresh behavior;
- background-task intervals after task ownership is moved into generations;
- dashboard/runtime presentation settings that are read dynamically rather than installed as middleware;
- backup cadence and retention policy.

### Initially restart-required

Reject these changes without partial application:

- `[server].host`;
- `[server].port`;
- `[server].threads`;
- Granian logging/access-log options fixed at server construction;
- database path;
- database worker-thread count;
- middleware topology or constructor-time values, including CORS origins, trusted hosts, and body-size middleware until they are made runtime-aware;
- deployment paths, PID/control socket locations, or other process bootstrap settings;
- any field not explicitly classified as live-reloadable.

The default for newly added configuration fields should be restart-required until deliberately classified and tested.

## Control plane

The CLI and live ASGI worker are separate processes. `rehash` therefore needs an explicit local control channel.

Preferred design: a Unix-domain socket in EggPool's resolved state directory, owned by the deployment user and created with restrictive permissions. The socket protocol can be newline-delimited JSON or length-prefixed JSON with a small typed command set.

Initial command:

```json
{"command":"reload_config","validated_digest":"..."}
```

Structured response:

```json
{
  "ok": true,
  "generation": 4,
  "changed_sections": ["providers", "accounts", "routing"],
  "warnings": [],
  "restart_required": [],
  "retirement_pending": true
}
```

The server must never accept an arbitrary config path from the client. It must reload the path with which the worker was started. Filesystem ownership and socket permissions are the primary authorization boundary. The design should leave room for authenticated loopback HTTP fallback on platforms without Unix sockets, but that fallback is not required for the first Linux-focused implementation.

## CLI behavior

`eggpool rehash` should:

1. Resolve the configured path exactly as other CLI commands do.
2. Run the shared validation service locally before contacting the server.
3. On failure, print `Configuration validation failed; live configuration unchanged`, render the underlying validation error, return nonzero, and do not contact the server.
4. Connect to the local control socket.
5. Send the validated digest with the reload command.
6. Render warnings and the structured server result.
7. Return nonzero for validation failure, digest mismatch, restart-required changes, preparation failure, commit failure, or unavailable control channel.
8. Never silently fall back to a hard restart.

The server repeats the same validation because the file can change after CLI preflight and because another local client could invoke the control protocol directly.

`check-config` itself should preserve existing output compatibility where practical, but use the shared validation result rather than maintaining separate validation code.

## Persistence and reconciliation

The primary database should remain process-owned for this roadmap. Candidate preparation may reconcile providers/accounts using existing repositories, but the reconciliation must be atomic and coordinated with generation activation.

Required behavior:

- additions and updates are prepared consistently with the candidate generation;
- removed accounts/providers are disabled or retired rather than destructively deleting historical identities;
- reconciliation failure rolls back and prevents activation;
- active requests on retiring generations retain valid account/provider references;
- the implementation documents whether persistence reconciliation occurs immediately before commit or as a staged transaction whose commit is coordinated with runtime activation;
- tests cover failure between persistence work and runtime commit.

Where strict cross-resource atomicity is impossible, prefer idempotent reconciliation plus a compensating rollback strategy and record an operational event. The plan must not claim atomicity stronger than the database/runtime boundary can actually provide.

## Background tasks

Existing lifespan callbacks capture startup objects and configuration. Move configuration-derived periodic scheduling under generation ownership or refactor callbacks to obtain a generation lease at each tick.

Generation-owned supervisors are preferred because they preserve a coherent configuration across each task invocation. On reload:

- candidate tasks are registered during preparation but do not run;
- after runtime commit, candidate scheduling starts;
- retiring scheduling stops admitting new ticks;
- an in-progress old tick either completes or is cancelled according to a task-specific policy;
- metrics flush and resource cleanup occur in a defined retirement order.

Tasks touching process-owned resources must remain safe when an old generation drains concurrently with a new generation.

## Observability

Expose at least:

- active generation number;
- active config digest without secret content;
- active config path;
- last reload start/completion time;
- last reload outcome and failure stage;
- changed sections;
- warnings;
- restart-required fields;
- number and age of retiring generations;
- active lease counts by generation;
- retirement cleanup errors;
- total reload duration and stage durations.

Record operational events for validation rejection, restart-required rejection, candidate preparation failure, successful activation, and retirement completion/failure. Never include credentials or raw secret-bearing diffs.

## Milestones

### Milestone A — Validation, diffing, and fail-closed CLI foundation

Extract reusable `check-config` validation; add content digests; implement typed config diff/classification; make `rehash` perform mandatory local validation; define structured results and error taxonomy; preserve current restart behavior only behind the explicit `restart` command.

Deliverable: validation and policy infrastructure is complete and fully tested, but live reload is not yet enabled.

### Milestone B — Runtime generation and request-lease infrastructure

Introduce process-owned `RuntimeManager`, immutable runtime generations, request/stream leases, generation construction and teardown, and startup through generation zero. Refactor request paths and resource ownership without changing operator-visible behavior.

Deliverable: EggPool still starts normally, but all configuration-derived request services are behind a generation boundary and can be replaced safely.

### Milestone C — Control plane and transactional live rehash

Add the local control socket, candidate preparation, server-side validation, diff enforcement, atomic generation swap, old-generation draining, persistence reconciliation, generation-owned background task transition, CLI integration, diagnostics, and end-to-end tests.

Deliverable: `eggpool rehash` validates and applies supported changes without PID replacement, listener interruption, or active-stream termination.

### Future follow-up

After milestones A-C are stable, configuration-mutating commands such as `connect` and `logout` should invoke the same control-plane reload transaction after writing configuration. They must not maintain independent restart/reload logic.

## Cross-milestone acceptance criteria

- Invalid TOML cannot terminate or destabilize the running server.
- Invalid startup auth or account credentials cannot reach candidate preparation.
- The CLI does not contact the server when local preflight fails.
- The server independently rejects invalid configuration presented after a stale or bypassed CLI preflight.
- A digest mismatch is rejected with no live mutation.
- Restart-required differences are enumerated and no reloadable subset is applied.
- Successful reload keeps the same supervisor PID, worker PID where applicable, and listener socket.
- A streaming request started before commit completes using its original generation.
- A request started after commit uses the new generation.
- Old provider clients remain open until their generation drains, then close exactly once.
- Concurrent reload attempts serialize and produce deterministic outcomes.
- Candidate preparation and reconciliation failures leave the active generation and request behavior unchanged.
- Reload diagnostics contain no secrets.
- `eggpool restart` remains available as the explicit mechanism for process-bound changes.

## Validation matrix

At minimum, automated tests must cover:

- malformed TOML;
- Pydantic/model validation failure;
- missing or invalid server API key configuration;
- invalid account credentials;
- stale-contract warnings;
- no-op reload;
- provider/account add, update, disable, credential rotation, and removal;
- routing and timeout changes;
- transcoder/cache/compression changes;
- host, port, thread, database, CORS, trusted-host, and body-limit restart-required changes;
- file mutation between CLI validation and server validation;
- concurrent reload commands;
- candidate client construction failure;
- repository/database reconciliation failure;
- active non-streaming request across commit;
- active streaming request across commit;
- client disconnect during old-generation drain;
- background task transition;
- drain timeout and delayed retirement;
- control socket permission and stale-socket recovery;
- process shutdown while a generation is retiring.

## Documentation requirements

Update CLI help, operator documentation, configuration documentation, and deployment notes to explain:

- `rehash` is a validated live reload, not a restart alias;
- `check-config` is always applied first;
- invalid configurations leave the active server unchanged;
- which fields are reloadable and which require `restart`;
- how no-op reloads, warnings, and retirement status are reported;
- control socket location and permissions;
- troubleshooting for unavailable control sockets and stuck retiring generations.
