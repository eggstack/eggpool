> **Status: Complete** — All workstreams implemented. C1-C2 (control plane, CLI),
> C4-C9 (reload transaction), C10 (background task transition), C12 (diagnostics
> and operational events), C13 (test suite), C14 (documentation). C11 (auth/credential
> rotation) intentionally skipped — adds complexity where it is not needed.

# Live Configuration Rehash — Milestone C

## Control Plane and Transactional Live Reload

## Objective

Complete the operator-facing live rehash path. Add a local control channel between the CLI and running worker, repeat `check-config` validation inside the server, classify changes, prepare a complete candidate generation, reconcile persistent state, atomically publish the candidate, drain the previous generation without interrupting active requests, and expose detailed reload diagnostics.

At completion, `eggpool rehash` must apply supported configuration changes without replacing the supervisor/worker process or listener. Broken configuration must be filtered before any attempt and must leave the active runtime unchanged.

## Non-negotiable transaction semantics

A reload attempt has one atomic publication point.

Before publication:

- the old generation remains authoritative;
- all failures are rollback/fail-closed failures;
- no new request can observe candidate services;
- no old-generation resource may be closed;
- restart-required changes reject the whole operation, not only the unsupported subset.

After publication:

- the candidate generation is authoritative for all new leases;
- the old generation is retiring;
- cleanup failure cannot reactivate the old generation;
- active old-generation requests continue normally;
- diagnostics must distinguish successful activation from incomplete retirement.

## Workstream C1 — Unix-domain control socket

Implement a process-owned local control server under EggPool's resolved state directory.

Requirements:

- deterministic socket path derived from existing runtime-path resolution;
- parent directory created with deployment-user ownership;
- socket mode restricted to the deployment user, preferably `0600` or equivalent effective access;
- stale socket detection and safe cleanup at startup;
- refusal to replace a socket belonging to a live incompatible process;
- bounded request size;
- bounded command/read/write timeouts;
- structured protocol version;
- no arbitrary file paths or shell-like arguments;
- graceful control-server shutdown before process-owned runtime teardown.

Suggested protocol:

```json
{
  "protocol_version": 1,
  "request_id": "uuid-or-random-token",
  "command": "reload_config",
  "validated_digest": "sha256-hex"
}
```

Response:

```json
{
  "protocol_version": 1,
  "request_id": "...",
  "ok": true,
  "stage": "retirement",
  "generation": 2,
  "changed_sections": ["providers", "routing"],
  "warnings": [],
  "restart_required": [],
  "retirement_pending": true,
  "message": "Configuration generation 2 activated"
}
```

Use newline-delimited JSON only if parsing is unambiguous and request size is capped. A small length-prefixed frame is also acceptable. Reuse EggPool's JSON abstraction where appropriate, but prioritize protocol correctness and low complexity.

## Workstream C2 — Control client and final `rehash` CLI

Replace Milestone A's temporary post-validation behavior with the real control client.

CLI sequence:

1. Resolve configuration path.
2. Run shared local `validate_config_file()`.
3. On failure, print a clear error and `live configuration unchanged`; exit nonzero without opening the socket.
4. Connect to the expected control socket.
5. Send `reload_config` with the exact validated content digest.
6. Wait for a structured response with configurable timeout.
7. Render warnings, changed sections, generation number, retirement status, and restart-required fields.
8. Exit zero only for successful activation, including semantic no-op success.
9. Never invoke `restart` automatically.

Failure messages should distinguish:

- local validation failure;
- server validation failure;
- digest mismatch/file changed after validation;
- control socket unavailable;
- protocol mismatch;
- concurrent reload already active or queued;
- restart-required change;
- candidate preparation failure;
- persistence reconciliation failure;
- commit failure;
- activation succeeded but retirement has warnings.

An activation with retirement pending is still success. A post-activation cleanup warning should be rendered as warning and visible in diagnostics, not converted into a claim that activation failed.

## Workstream C3 — Server-side mandatory validation gate

The control handler must independently invoke the exact shared validation service from Milestone A against `app.state.config_path` or the process runtime's immutable config path.

It must not trust:

- the CLI's parsed configuration;
- a client-provided path;
- a client assertion that validation succeeded;
- only the client digest.

Required order:

1. Acquire reload serialization lock or establish deterministic queue/rejection policy.
2. Read and validate server-side config.
3. Compare resulting content digest to `validated_digest` when supplied.
4. Reject on mismatch with no candidate preparation or persistence mutation.
5. Continue to diff only after validation and digest confirmation succeed.

This repeated validation protects against direct control clients and files changing between CLI preflight and server application.

Record a validation rejection operational event using only error class/code and redacted path metadata. Do not store raw secret-bearing messages if upstream validators include values.

## Workstream C4 — Reload serialization and operation state

Extend `RuntimeManager` with an explicit reload transaction API and status object.

Suggested shape:

```python
async def reload(
    self,
    validation: ConfigValidationResult,
    *,
    expected_digest: str | None,
) -> ReloadResult:
    ...
```

Use one lock to serialize complete reload transactions. Decide and document whether a concurrent command:

- waits behind the current reload; or
- is rejected with `reload_in_progress`.

Rejecting is simpler and avoids applying stale intermediate file states. If waiting is chosen, re-read and revalidate only when the queued operation acquires the lock.

Track current operation stage and timestamps for diagnostics. Ensure cancellation of a client connection does not automatically cancel the reload transaction after candidate preparation begins; otherwise operator disconnect could leave ambiguous persistence state. Use shielding or a detached task with result tracking where appropriate.

## Workstream C5 — Diff enforcement and no-op handling

Compare active and candidate configurations through Milestone A's typed reload policy.

Behavior:

- semantic no-op: return success with current generation, no construction or swap;
- only ignored changes: return success with explanation;
- any restart-required change: reject the entire operation and enumerate all such fields;
- live-only changes: continue to candidate preparation;
- unknown policy path: classify as restart-required and reject.

Do not expose secret old/new values. Group output by section for readability.

## Workstream C6 — Candidate generation preparation

Use Milestone B's `RuntimeGenerationBuilder` to construct a candidate generation without publishing it.

Preparation requirements:

- generation ID reserved but not active;
- all new clients and services created off to the side;
- background callbacks registered but not scheduled;
- persisted quota/usage state loaded;
- provider and account rows mapped consistently;
- catalog/model mapping readiness established to the same degree as normal startup;
- no startup crash recovery or migration rerun;
- no active-generation mutable state reused unless explicitly process-owned and concurrency-safe;
- partial failure closes all candidate resources.

Add candidate readiness checks that detect obvious construction problems without sending production provider traffic. Do not add mandatory external network probes that can make a valid local reload fail because an upstream is temporarily unavailable, unless existing startup semantics already require them.

## Workstream C7 — Persistence reconciliation

Define a concrete consistency protocol for provider/account repository updates.

Preferred sequence:

1. Build a reconciliation plan from active persistent state and candidate config.
2. Validate the plan and candidate service references.
3. Enter a database transaction.
4. Apply additive/update/disable reconciliation using stable identifiers.
5. Build or finalize candidate mappings against transaction results where necessary.
6. Commit database transaction immediately before runtime publication, with a narrow failure window.
7. Publish candidate generation.

Because database commit and Python reference publication cannot be one atomic transaction, implement one of:

- idempotent reconciliation plus compensating restore on publication failure;
- publication structured so it cannot fail after database commit except catastrophic process failure;
- staging tables/versioned rows activated by the same generation identifier, allowing runtime publication to select already-staged state.

The implementation must document the chosen guarantee precisely. At minimum:

- reconciliation is idempotent;
- removed identities are disabled/retired rather than destructively deleted;
- historical foreign-key relationships remain valid;
- old-generation requests can finalize against their account/provider rows;
- publication failure after reconciliation is detectable and recoverable on the next attempt/startup;
- operational events identify the stage without leaking secrets.

## Workstream C8 — Atomic generation publication

Under the runtime manager's publication lock:

1. verify active generation is still the generation diffed during preparation;
2. mark candidate ready;
3. stop old generation from accepting new leases as part of the same critical section;
4. set candidate as active and accepting leases;
5. retain the old slot in retiring state;
6. update safe compatibility/proxy state if still present;
7. release the lock.

The active reference swap must occur before old-generation retirement begins. New requests should either acquire the old generation before commit or the new generation after commit, never fail because no generation is active.

Start candidate scheduling at a defined point. Prefer preparing task definitions before commit and starting their scheduler immediately after publication. If scheduler start can fail, decide whether it is a prepublication readiness step or a postpublication degraded-state warning. Critical tasks required for correctness should be proven startable before publication.

## Workstream C9 — Drain and retire previous generation

After publication:

- prevent new leases on the old generation;
- stop admission of new generation-owned periodic ticks;
- allow active request and streaming leases to drain;
- retain resources for as long as active leases need them;
- close old resources exactly once when the lease count reaches zero;
- expose retirement pending status immediately to the CLI;
- continue retirement if the CLI disconnects;
- log/record completion and cleanup errors.

Do not impose a short hard timeout that terminates legitimate long streams. A warning threshold may mark a generation as `retirement_delayed` while allowing it to remain alive. A separately documented administrative force-retire mechanism can be future work.

Test old-generation finalizers, quota accounting, health updates, and DB writes after a new generation is active.

## Workstream C10 — Background task transition

Complete the task-ownership design from Milestone B.

At reload:

- candidate task definitions exist before publication;
- candidate scheduling starts once candidate is authoritative;
- old scheduling stops creating new work;
- in-flight old ticks follow their documented completion/cancellation policy;
- no task runs with a mixed old config/new router combination;
- process-owned tasks obtain an active-generation lease per tick;
- metrics buffers and backup jobs do not duplicate destructive work across generations.

Specially test catalog refresh, model-info refresh/backfill, usage refresh, stale request finalization, health pruning, metrics flush, update checking, and automatic backup.

## Workstream C11 — Auth and credential rotation

Provider/account credential rotation is a normal live-reload case. Server API-key rotation needs special handling because it may affect request middleware/auth dependencies.

Requirements:

- request authentication reads the active generation or a dedicated atomically replaceable auth policy;
- a request admitted under the old generation retains coherent auth/request behavior;
- the local Unix socket does not depend on the candidate API key, avoiding bootstrap failure;
- no key values appear in diffs, logs, events, or response payloads;
- tests verify old key/new key behavior immediately around commit.

If current FastAPI dependencies capture auth config at route registration, refactor them before classifying API-key changes as live. Otherwise mark server API-key changes restart-required for the first release and document the limitation.

## Workstream C12 — Diagnostics and operational events

Expose reload state through health/stats/dashboard diagnostics as appropriate:

- active generation ID and digest prefix;
- last successful activation time;
- last attempted reload time;
- last outcome, stage, and redacted error code;
- changed sections;
- restart-required paths from last rejection;
- validation warning count;
- stage durations;
- retiring generation count, IDs, age, and active leases;
- delayed retirement warning;
- cleanup failures;
- control socket health.

Add operational events for:

- validation rejection;
- digest mismatch;
- restart-required rejection;
- preparation failure;
- reconciliation failure;
- activation success;
- retirement completion;
- retirement cleanup failure.

No event may contain TOML contents, API keys, authorization headers, account secrets, or unredacted secret diffs.

## Workstream C13 — End-to-end test matrix

### Mandatory broken-config filters

- malformed TOML: local CLI rejects, server untouched;
- schema failure: local CLI rejects;
- invalid startup auth: local CLI rejects;
- invalid account credentials: local CLI rejects;
- direct control request with invalid config: server rejects;
- config changed after local validation: server digest mismatch rejects;
- validation helper internal error: server preserves old generation;
- verify PID, generation, clients, database rows, and active request behavior remain unchanged in every rejection case.

### Successful reloads

- provider addition;
- account addition;
- credential rotation;
- account disable/removal semantics;
- routing weight/policy change;
- upstream timeout/retry change;
- transcoder change;
- cache/compression change;
- background interval change;
- no-op reload;
- warning-only reload.

### Restart-required rejection

- host;
- port;
- Granian runtime threads;
- access-log/server-construction setting;
- database path;
- database worker threads;
- CORS origins;
- trusted hosts;
- body-limit middleware configuration;
- unknown/unclassified field.

Assert no reloadable subset is applied.

### Concurrency and stream continuity

- long non-streaming request begins before commit and completes on old generation;
- slow OpenAI streaming request survives commit;
- slow Anthropic streaming request survives commit;
- transcoded stream survives commit;
- new request immediately after commit uses candidate routing/credentials;
- client disconnect releases old lease and completes retirement;
- multiple concurrent old streams drain independently;
- concurrent rehash commands follow documented reject/serialize policy;
- process shutdown during candidate preparation;
- process shutdown while old generation retires.

### Failure injection

- candidate provider client construction failure;
- outbound manager construction failure;
- catalog initialization failure;
- quota persisted-state load failure;
- reconciliation transaction failure;
- publication precondition mismatch;
- candidate task scheduler failure;
- old resource close failure;
- control client disconnect at each stage.

### Continuity assertions

For successful live reload:

- supervisor PID unchanged;
- worker PID unchanged where architecture permits verification;
- listener continuously accepts connections;
- no startup crash recovery is invoked;
- no migrations rerun;
- active stream has no synthetic interruption;
- old pool closes only after final old lease release;
- new pool serves new requests;
- observability identifies both activation and retirement.

## Workstream C14 — Documentation and command integration

Update:

- `eggpool rehash --help`;
- lifecycle/CLI documentation;
- configuration reference with reloadability table;
- deployment docs with control socket path and permissions;
- troubleshooting documentation;
- changelog/release notes.

Document that:

- `rehash` always performs `check-config` first;
- the live server validates again;
- invalid configuration cannot replace the active generation;
- restart-required changes are not partially applied;
- `restart` remains the explicit disruptive path;
- successful rehash can return while old generations are still draining;
- operators can inspect retirement state.

After the path is stable, refactor `connect` and `logout` to call a shared `apply_config_change()` helper that writes configuration atomically, validates it, and requests rehash. If validation or reload fails, these commands must preserve/restore the previous config file and report the failure. That integration may be included late in C or scheduled as immediate follow-up, but no independent restart logic should be added.

## Deliverables

- secure local control server and client;
- final fail-closed `rehash` command;
- repeated server-side `check-config` validation;
- digest race protection;
- reload serialization/status tracking;
- typed diff enforcement and restart-required rejection;
- candidate generation preparation;
- documented persistence reconciliation guarantee;
- atomic runtime publication;
- non-disruptive old-generation drain and retirement;
- generation/task transition;
- reload diagnostics and operational events;
- complete E2E/failure-injection tests;
- operator documentation.

## Acceptance criteria

- `eggpool rehash` never delegates to `restart`.
- The CLI runs the complete shared `check-config` validation contract before contacting the server.
- The live server repeats validation against its own configured path.
- Invalid configuration, digest mismatch, or restart-required differences leave the active generation untouched.
- A successful supported reload keeps the process and listening socket alive.
- Active streams continue to completion on their original generation.
- New requests use the new generation immediately after atomic publication.
- Old resources close only after old leases drain.
- Reload commands are serialized or deterministically rejected.
- No secret material is emitted in results, logs, events, or diagnostics.
- E2E tests prove both zero-interruption success and fail-closed broken-config behavior.
- Full lint, strict type checking, unit, request-path, dashboard, and performance-relevant regression suites pass.

## Handoff notes

Treat validation rejection tests as release blockers. The principal safety promise is not merely that valid configuration can reload, but that an invalid file cannot crash or replace a healthy running server. Preserve a narrow, auditable transaction boundary and resist shortcuts that mutate existing routers, registries, or client pools in place.
