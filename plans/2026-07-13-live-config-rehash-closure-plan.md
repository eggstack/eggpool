# Live Configuration Rehash Closure Plan

## Status

**Implementation status: closure-pass Phases 1-5 complete; Phase 6 (docs) complete.**

Closure and corrective implementation plan for completing the live configuration rehash roadmap after Milestones A-C.

The repository now contains the core validation, diffing, runtime-generation, control-socket, transactional publication, lease, retirement, diagnostics, and failure-handling infrastructure. The remaining gap is functional rather than architectural: every configuration field is still classified as `RESTART_REQUIRED`, so `eggpool rehash` validates and evaluates changes but does not yet apply a useful configuration change to the running process.

This plan closes that gap conservatively. It enables a first deliberately bounded set of `LIVE` fields, removes duplicated startup/candidate task-registration paths, proves true non-disruptive streaming behavior end to end, tightens repeated-reload semantics, and aligns `connect`/`logout` with the new runtime refresh path only after the required field families are genuinely reloadable.

## Primary objective

Deliver a production-credible `eggpool rehash` implementation that can apply provider, account, model-contract, routing, quota, transcoding, compression, and selected runtime-policy changes without restarting EggPool, while preserving these invariants:

1. `check-config` remains a mandatory fail-closed filter.
2. Invalid candidate configuration never changes the active runtime.
3. Process-bound changes never partially apply.
4. In-flight requests and streams continue on their original generation.
5. Requests accepted after publication use the new generation.
6. Old generation resources close only after all leases drain or an explicit bounded retirement policy is reached.
7. Database history and account identity remain stable across reloads.
8. Repeated or concurrent reloads cannot publish stale candidates.
9. Background tasks cannot drift between startup and reload construction paths.
10. Operator output clearly distinguishes applied, no-op, restart-required, validation-rejected, preparation-failed, and retirement-pending outcomes.

## Non-goals

This closure pass must not attempt live replacement of process-bound resources. The following remain restart-required unless a later roadmap explicitly redesigns them:

- server bind host;
- server port;
- Granian worker count;
- Granian runtime thread count;
- process-level access-log construction;
- database path;
- database worker-thread topology;
- middleware topology installed during `create_app()`, including CORS and trusted-host middleware;
- ASGI application construction options;
- deployment user, state directory, PID path, or control-socket path;
- Python interpreter, optional dependency, or JSON backend changes.

Do not weaken this boundary to make the first live reload appear broader than it is.

## Current state to preserve

The existing implementation already provides:

- `config_validation.py` as the shared Click-free validation contract;
- exact content digests for time-of-check/time-of-use protection;
- secret-safe runtime fingerprints;
- typed configuration diffs and reload dispositions;
- a fail-closed default policy;
- `RuntimeManager` and immutable runtime generations;
- request and streaming leases;
- Unix-domain control socket transport;
- candidate preparation before publication;
- stale-candidate publication guards;
- old-generation retirement;
- operational-event and runtime diagnostics;
- candidate cleanup after partial construction failure;
- restart-required and ignored-only handling;
- dedicated validation, policy, control, reload-manager, and runtime-manager tests.

The closure work should extend these seams rather than creating a second reload path.

---

# Workstream 1: Define and enable the first safe `LIVE` field inventory

## 1.1 Audit the complete field-disposition map

Review every entry in `_FIELD_DISPOSITION` against the actual ownership graph established by `ProcessRuntime`, `RuntimeGeneration`, the candidate builder, and request-time leases.

For each field, document:

- current disposition;
- owning object;
- whether the object is process-owned or generation-owned;
- candidate-construction path;
- persistence side effects;
- retirement requirements;
- test proving safe replacement;
- rationale for `LIVE`, `RESTART_REQUIRED`, or `IGNORED`.

Add a machine-readable or test-visible coverage assertion so newly added `AppConfig` fields cannot silently fall through to an unreviewed state. Fail closed by default.

## 1.2 First `LIVE` field family: providers and accounts

Enable fields whose values are fully consumed by generation-owned provider/account services and whose persistence can be reconciled transactionally.

Candidate fields include:

- provider definitions and enabled state;
- provider base URLs;
- provider authentication mode, header, and configured credentials;
- provider protocol declarations;
- static model declarations;
- model endpoint contracts;
- provider headers;
- provider proxy selection where the outbound manager is generation-owned;
- account names, enabled state, credentials, quota metadata, and provider association;
- configured account priority or weight values;
- account model allow/deny configuration;
- provider-specific timeout and retry policy where represented in generation-owned clients.

Before marking any field `LIVE`, prove that the candidate generation builds a replacement `AccountRegistry`, `ProviderClientPool`, `OutboundClientManager`, router-facing account state, and related model/catalog state without mutating the active generation.

## 1.3 Second `LIVE` field family: routing and eligibility policy

Enable generation-owned routing fields, including where applicable:

- routing priority;
- weights;
- round-robin or selection policy;
- model collapse/exposure behavior;
- provider/account preference ordering;
- fallback policy;
- local quota advisory mode;
- retry/attempt policy used by `RequestCoordinator`;
- routing trace policy if dynamically consumed;
- model overrides and aliases;
- account/model eligibility rules.

Verify that new requests observe the new policy immediately after publication and that requests already holding a lease continue using the prior router and coordinator.

## 1.4 Third `LIVE` field family: transcoding, compression, and cache policy

Enable only settings whose implementation is generation-owned or immutable request policy carried by the candidate coordinator.

Candidate fields include:

- transcoder enabled state;
- transcoder loss policy;
- native-protocol preference;
- compression profile and thresholds;
- cache/compression advisory policy;
- deterministic compression controls;
- request-level payload transformation limits;
- provider protocol compatibility policy.

Do not mark shared mutable cache storage configuration `LIVE` until ownership and migration semantics are explicit. Policy can be live while storage topology remains restart-required.

## 1.5 Fourth `LIVE` field family: selected background and observability policy

After task-registration unification is complete, consider:

- catalog refresh interval;
- model-info refresh interval and enabled state;
- retention durations;
- backup cadence and enabled state;
- metrics flush cadence;
- stale-request finalizer threshold derived from upstream timeout;
- model ping retention and refresh cadence;
- update-check cadence if supported by the supervisor abstraction.

These fields must not be marked live until candidate task registration and task retirement are proven to use one authoritative registration function.

## 1.6 Keep explicit restart-required coverage

Add tests that representative process-bound changes are rejected without candidate publication:

- host;
- port;
- server threads;
- database path;
- database worker threads;
- CORS origins;
- trusted hosts;
- body-limit middleware construction settings;
- control-socket path or state directory.

The response must list exact dotted field names and old/new values with secrets redacted.

---

# Workstream 2: Unify startup and candidate runtime construction

## 2.1 Remove duplicated background-task registration

The current code has startup registrations in the lifespan path and a mirrored `register_candidate_tasks()` path. Replace this duplication with one authoritative function used by both initial startup and candidate generation construction.

Recommended shape:

```python
@dataclass(frozen=True)
class TaskRegistrationContext:
    process: ProcessRuntime
    runtime_manager: RuntimeManager
    config: AppConfig
    generation: RuntimeGeneration | None
    startup: bool


def register_runtime_tasks(
    supervisor: TaskSupervisor,
    context: TaskRegistrationContext,
) -> None:
    ...
```

The initial generation and every candidate must pass through the same registration table. Startup-only behavior should be explicit through parameters rather than separate copied code.

## 2.2 Decide task ownership consistently

Classify each task as either:

- **process-owned/current-generation task**: one stable schedule that leases the current generation on each tick; or
- **generation-owned task**: schedule and callback belong to a specific generation and stop during its retirement.

Do not mix both models accidentally.

Suggested classification:

Process-owned/current-generation:

- database checkpoint;
- retention cleanup;
- update check;
- automatic backup where it targets the process database/config path;
- globally persisted metrics flush if the coalescer is process-owned.

Generation-owned or current-generation leased:

- catalog refresh;
- model-info refresh/backfill;
- usage-window refresh;
- health disabled-model pruning;
- stale-request finalizer logic that depends on generation router/quota state;
- finalization retry drain if the queue is generation-owned.

Where a process-owned schedule uses generation state, acquire one lease per tick and hold it for the entire operation.

## 2.3 Add task registration parity tests

Create tests that compare initial-startup and candidate registration output:

- identical task-name sets for equivalent configs;
- identical interval and delay values;
- identical enabled/disabled decisions;
- no duplicate task names;
- every configurable task field affects both startup and reload paths;
- adding a new task to the registration table automatically covers both paths.

## 2.4 Validate task transition ordering

During publication:

1. Candidate tasks are fully registered but not allowed to race against unpublished state.
2. Candidate generation is published.
3. Candidate/current-generation task execution becomes eligible.
4. Old generation task scheduling stops according to ownership model.
5. Any in-progress old tick completes under its original lease or is cancelled according to a documented policy.
6. Old supervisor/resources retire only after task and request leases drain.

Add deterministic tests around this ordering.

---

# Workstream 3: Harden transactional persistence reconciliation

## 3.1 Define account/provider reconciliation semantics

Document and implement exact behavior for:

- newly added provider;
- newly added account;
- credential rotation;
- account disablement;
- account removal from config;
- provider disablement;
- provider removal;
- renamed account;
- changed provider association;
- changed static model declarations;
- restored previously removed account.

Historical request, attempt, event, usage, and cost rows must remain valid.

Prefer soft retirement/disablement over destructive deletion.

## 3.2 Preserve stable identity

Ensure account/provider reconciliation uses stable natural keys or explicit IDs so reload does not create duplicate rows for unchanged entities.

Add tests proving:

- repeated no-op reloads create no rows;
- credential-only updates preserve account IDs;
- disable/enable cycles preserve history and identity;
- removed accounts are excluded from new routing but remain queryable historically;
- renamed accounts follow an explicit, documented policy rather than accidental duplicate creation.

## 3.3 Transaction boundary

All persistence changes required for a candidate must complete in a transaction before publication.

If persistence reconciliation fails:

- roll back all changes;
- close the candidate generation;
- leave the active generation unchanged;
- record a structured operational event;
- return a nonzero CLI result with a redacted diagnostic.

If publication fails after reconciliation because of the generation guard, either:

- roll back candidate-only persistence changes where safe; or
- make reconciliation idempotent and harmless for a subsequent retry.

The chosen behavior must be documented and tested.

## 3.4 Active-request removal semantics

If an account or provider is removed while an old-generation request is active:

- the existing request must retain its account/client references and complete normally;
- new requests must not select the removed account after publication;
- the database row must not be deleted while historical or active references exist;
- old client resources close only after generation drain.

Add an E2E test for this case.

---

# Workstream 4: True end-to-end non-disruptive reload validation

## 4.1 Build a process-level test harness

Create an integration harness that launches EggPool as an actual server process with:

- a temporary config file;
- a temporary state directory and control socket;
- a temporary SQLite database;
- deterministic fake upstream providers;
- a slow streaming endpoint;
- observable provider/account identity in responses;
- PID, socket, and health probes.

Do not rely only on direct `RuntimeManager` invocation for closure acceptance.

## 4.2 Canonical streaming generation-swap test

Required scenario:

1. Start EggPool with provider/account A.
2. Begin a slow streamed response through A.
3. Confirm at least one chunk has arrived and the stream remains open.
4. Rewrite config to select provider/account B using only `LIVE` fields.
5. Run `eggpool rehash` through the Unix control socket.
6. Assert validation succeeds and generation increments.
7. Assert EggPool supervisor and worker PIDs do not change.
8. Assert the listening socket remains continuously available.
9. Send a new request and verify it routes through B.
10. Allow the original stream to finish and verify all remaining chunks came from A.
11. Assert old-generation retirement remains pending while the stream lease is active.
12. Assert A's client pool closes after stream completion.
13. Assert no request, attempt, or reservation remains leaked or pending.

This is the release-defining acceptance test.

## 4.3 Additional E2E scenarios

Add process-level tests for:

- provider addition;
- provider removal during active stream;
- credential rotation;
- routing-weight change;
- model alias or override change;
- transcoder policy change;
- compression policy change;
- no-op rehash;
- ignored-only change;
- restart-required mixed with live changes;
- invalid TOML;
- schema failure;
- missing credential;
- digest mismatch between CLI preflight and server read;
- candidate construction failure;
- persistence reconciliation failure;
- control-socket unavailable;
- control-socket permission rejection;
- retirement timeout;
- two simultaneous rehash commands;
- three sequential rehashes while an old stream remains active.

## 4.4 Mixed live and restart-required policy

Choose and enforce one clear behavior. Recommended behavior:

- if any changed field is `RESTART_REQUIRED`, reject the entire reload;
- do not apply the live subset;
- return the complete restart-required list;
- leave the active generation unchanged.

Partial application would make the file on disk diverge from the active runtime and should remain prohibited.

## 4.5 Availability assertions

During all successful live reload tests, sample health continuously and assert:

- no refused connection;
- no non-2xx health response;
- no PID change;
- no control-socket disappearance;
- no interruption of active SSE/stream response;
- no unexpected increase in pending/leaked request state.

---

# Workstream 5: Repeated reload, concurrency, and retirement hardening

## 5.1 Reload serialization

Prove that concurrent reload commands are serialized or one is rejected with a clear busy result.

The implementation must prevent:

- two candidates publishing against the same base generation;
- stale candidate publication;
- double retirement of one generation;
- shared candidate resource closure by the wrong reload operation;
- inconsistent diagnostics ordering.

## 5.2 Multi-generation retirement

Support and test the case where generation 1 still has a long stream, generation 2 is published, then generation 3 is published before generation 1 drains.

Expected behavior:

- generation 3 is active;
- generation 2 retires when its leases drain;
- generation 1 remains independently pending until its stream completes;
- retirement state tracks more than one old generation;
- resources close exactly once;
- diagnostics expose all pending retired generations, not only one boolean.

If the current manager only supports one retirement slot, extend it to a bounded retirement registry keyed by generation ID.

## 5.3 Retirement timeout semantics

Clarify whether timeout means:

- stop waiting synchronously but continue retirement in the background; or
- forcibly close generation resources.

For active user streams, prefer the first behavior. A reload command may return `retirement_pending=true`, while the manager continues safe retirement asynchronously.

Do not force-close live streams merely because the CLI timeout elapsed.

Add diagnostics for:

- generation ID;
- active lease count;
- retirement start time;
- elapsed retirement time;
- last cleanup error;
- resources closed state.

## 5.4 Shutdown interaction

Test process shutdown while one or more generations are retiring.

Shutdown must:

1. stop accepting new requests;
2. stop control-socket commands;
3. prevent new reload publication;
4. stop task scheduling;
5. allow configured graceful request drain;
6. close active and retired generation resources exactly once;
7. flush process-owned metrics;
8. close database connections last.

---

# Workstream 6: CLI and operator behavior

## 6.1 Final `rehash` result contract

Standardize human-readable and structured output for:

- applied reload;
- no-op;
- ignored-only change;
- validation rejection;
- digest mismatch;
- restart required;
- candidate preparation failure;
- persistence failure;
- publication conflict;
- retirement pending;
- control socket unavailable.

Successful example:

```text
Configuration refreshed successfully.
  Generation: 4 -> 5
  Applied sections: providers, accounts, routing
  Active requests on previous generation: 2
  Previous generation retirement: pending
```

Validation failure example:

```text
Configuration refresh rejected; active configuration is unchanged.
  Stage: validation
  Error: account 'minimax-primary' is missing required credential 'api_key'
```

Restart-required example:

```text
Configuration is valid but cannot be applied live; active configuration is unchanged.
  Restart required:
    server.port: 8000 -> 9000
    database.path: var/eggpool.sqlite3 -> /srv/eggpool.sqlite3
Run `eggpool restart` to apply these fields.
```

## 6.2 Exit codes

Define stable exit codes, for example:

- `0`: applied, no-op, or ignored-only success;
- `1`: validation/configuration failure;
- `2`: restart required;
- `3`: control-plane unavailable;
- `4`: reload conflict/busy;
- `5`: candidate preparation or publication failure.

Document these for scripts and deployment tooling.

## 6.3 Optional machine-readable output

Add `--json` to `rehash` if consistent with CLI conventions. Output the existing typed result without secrets.

This is useful for systemd hooks, configuration-management tooling, and future `connect`/`logout` integration.

## 6.4 Control-socket discovery

Ensure CLI and server use one shared path resolver for the Unix socket, respecting deployment user/state directory rules.

Avoid hardcoded `~/.local/state/eggpool/eggpool.sock` assumptions in code paths used by system-wide deployments.

Add tests for personal and system deployment layouts.

---

# Workstream 7: Integrate `connect` and `logout` safely

## 7.1 Preconditions

Do not change `connect` or `logout` to assume live application until provider/account field families are classified `LIVE` and pass the process-level E2E suite.

## 7.2 `connect` flow

After writing the new account/provider configuration:

1. run the shared validation contract against the exact file bytes;
2. if validation fails, report the error and preserve or roll back the file edit according to existing command semantics;
3. if the server is running, invoke the control-plane reload;
4. if reload applies, report the new generation;
5. if restart-required fields are also present, do not partially apply;
6. if the server is not running, report that the configuration is valid and will apply on next start;
7. never perform an implicit hard restart.

## 7.3 `logout` flow

After disabling/removing the selected account configuration:

- validate first;
- live-reload when the server is running;
- preserve active old-generation streams using that account;
- prevent new selections immediately after publication;
- retain historical database identity;
- do not delete persistent rows destructively;
- report retirement pending if active streams still reference the account.

## 7.4 Other commands to review

Audit configuration-mutating commands and classify whether they should invoke the same path:

- onboarding completion;
- provider account edits;
- API-key rotation commands;
- dashboard-public toggles;
- cache/compression configuration helpers;
- model/provider configuration writers;
- any future `config set` command.

Centralize this behavior in one CLI helper rather than duplicating validate-and-rehash logic.

---

# Workstream 8: Diagnostics, documentation, and release semantics

## 8.1 Runtime diagnostics

Expose:

- active generation ID;
- active runtime fingerprint prefix;
- last successful reload time;
- last reload status/stage;
- changed sections;
- validation warning count;
- restart-required fields;
- candidate build duration;
- publication duration;
- pending retired generations;
- lease count per retired generation;
- oldest retirement age;
- cleanup failures;
- control-socket status.

Keep secrets and credential-derived values out of logs and API responses.

## 8.2 Operational events

Record structured events for:

- reload requested;
- validation rejected;
- digest mismatch;
- restart required;
- candidate preparation failed;
- persistence reconciliation failed;
- candidate published;
- old generation retirement pending;
- old generation retired;
- cleanup failed;
- publication conflict.

Include generation IDs, durations, and changed section names, but not raw secret values.

## 8.3 Documentation accuracy

Update all wording that currently presents the control plane as equivalent to fully functional live rehash.

Once the first live field set ships, document:

- exactly which fields are live;
- exactly which fields require restart;
- mixed-change rejection behavior;
- stream-drain semantics;
- control-socket location and permissions;
- CLI exit codes;
- `connect`/`logout` behavior;
- troubleshooting steps;
- how to inspect pending generation retirement.

Generate the live/restart field table from the policy map if practical to prevent documentation drift.

## 8.4 Changelog and release gate

Do not describe the feature as fully complete until the canonical process-level streaming generation-swap test passes with at least one meaningful provider/account/routing change.

The release note should distinguish:

- validation/control-plane infrastructure;
- first supported live field families;
- process-bound fields that still require restart.

---

# Implementation sequence

## Phase 1: Ownership and policy closure

1. Audit `_FIELD_DISPOSITION` against runtime ownership.
2. Add exhaustive disposition coverage tests.
3. Define first supported live field families.
4. Keep all uncertain fields restart-required.
5. Add generated/documented field inventory.

Exit criterion: every config field has a reviewed ownership rationale and no newly added field can bypass classification.

## Phase 2: Construction and task unification

1. Extract one authoritative runtime-generation builder.
2. Extract one authoritative task-registration function.
3. Classify task ownership.
4. Route initial startup and candidate startup through the same code.
5. Add task parity and transition-order tests.

Exit criterion: there is no duplicated startup-versus-reload registration table.

## Phase 3: Provider/account/routing live enablement

1. Complete transactional provider/account reconciliation.
2. Prove stable identity and historical preservation.
3. Enable provider/account/routing field dispositions.
4. Add unit and integration coverage for each newly live field group.
5. Keep mixed live/restart changes fail-closed.

Exit criterion: a provider/account/routing change can publish a new generation without restart.

## Phase 4: Streaming and multi-generation E2E

1. Build the real-process fake-upstream harness.
2. Implement the canonical slow-stream generation-swap test.
3. Test removal and credential rotation during active streams.
4. Test concurrent and sequential reloads.
5. Support multiple pending retired generations if required.
6. Verify shutdown while retirement is pending.

Exit criterion: PID, listener, control socket, and active stream remain stable while new requests use the new generation.

## Phase 5: Additional policy families and command integration

1. Enable transcoding/compression/request-policy fields proven generation-owned.
2. Enable selected task cadence fields after registration unification.
3. Integrate `connect` and `logout` through shared validate-and-reload helper.
4. Audit other config writers.
5. Add stable exit codes and optional JSON output.

Exit criterion: common account-management operations apply without restart and never bypass validation.

## Phase 6: Documentation and release closure

1. Finalize live/restart field documentation.
2. Tighten diagnostics and operational events.
3. Update changelog and deployment docs.
4. Run full lint, type, unit, integration, performance, and process-level E2E suites.
5. Perform manual systemd and personal-daemon smoke tests.

Exit criterion: the feature can be accurately described as live configuration rehash for the documented field families.

---

# Test matrix

## Validation and fail-closed behavior

- malformed TOML;
- schema error;
- startup-auth error;
- missing account credential;
- unreadable config;
- config path replaced with directory/symlink edge cases according to policy;
- stale-contract warning propagation;
- CLI/server digest mismatch;
- unexpected validator exception;
- active generation unchanged after every failure.

## Reload policy

- exhaustive field coverage;
- secret redaction;
- process-bound rejection;
- mixed live/restart rejection;
- ignored-only no-op;
- unchanged config no-op;
- nested provider/account list diffs;
- added and removed list entries;
- deterministic ordering of changes.

## Runtime generation

- lease acquired before publication stays on old generation;
- lease acquired after publication uses new generation;
- client pools close after final lease;
- cleanup called exactly once;
- partial candidate cleanup;
- publication guard;
- multiple pending retirements;
- retirement timeout continues safely;
- shutdown with active and retired generations.

## Persistence

- add provider/account;
- update credential;
- disable/enable;
- remove from routing without destructive deletion;
- stable IDs across reload;
- no duplicate rows on repeated reload;
- rollback on failure;
- historical queries remain valid.

## Background tasks

- startup/reload task parity;
- cadence change;
- enable/disable transition;
- task tick holds one generation lease;
- no tick starts against unpublished candidate;
- no duplicate schedules after repeated reload;
- in-progress tick completion/cancellation policy;
- task cleanup on retirement.

## Control plane and CLI

- socket permissions;
- stale socket cleanup;
- personal/system path resolution;
- unavailable server;
- malformed request;
- protocol version mismatch;
- timeout;
- concurrent rehash;
- stable exit codes;
- JSON output redaction.

## E2E availability

- slow stream A while new requests route B;
- provider removal during stream;
- credential rotation;
- continuous health polling;
- unchanged PID;
- unchanged listening socket;
- no leaked pending requests/reservations;
- old resource closure after drain;
- three-generation overlap.

---

# Acceptance criteria

The closure pass is complete only when all of the following are true:

1. `eggpool rehash` applies at least provider/account/routing changes without process restart.
2. `check-config` validation is executed before every reload attempt and repeated server-side against the exact candidate bytes.
3. Any validation failure leaves the active generation and persistence state unchanged.
4. Any restart-required change rejects the entire reload without partial application.
5. Initial startup and candidate generation use the same runtime and background-task construction paths.
6. A process-level slow-stream test proves old-generation stream continuity and new-generation routing.
7. Supervisor and worker PIDs remain unchanged during successful reload.
8. The listening socket and health endpoint remain continuously available.
9. Old provider clients remain open until all old-generation leases drain.
10. Multiple retired generations are tracked and cleaned safely.
11. Account/provider reconciliation preserves stable IDs and historical rows.
12. Concurrent reloads cannot publish stale candidates.
13. `connect` and `logout` use the shared validate-and-reload path once their changed fields are live-capable.
14. Process-bound field documentation is explicit and tested.
15. Full Ruff, Pyright, unit, integration, and process-level E2E suites pass.
16. No secret appears in diffs, logs, control responses, diagnostics, or test snapshots.
17. Changelog and README wording accurately describe the supported live field families rather than merely the infrastructure.

## Handoff note

The implementation agent should resist broadening the first `LIVE` set prematurely. The safest closure path is to make provider/account/routing replacement demonstrably correct, prove stream-safe generation swapping in a real process, and only then expand into transcoding, compression, task cadence, and command integrations. The existing fail-closed policy is an asset and should remain the default for every field that lacks a complete ownership, preparation, publication, and retirement story.
