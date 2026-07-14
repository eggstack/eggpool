# Live Configuration Rehash Polish Pass

## Status

Proposed corrective and release-polish pass following completion of the provider/account/routing live-rehash closure work.

The live configuration infrastructure is now operational and the first bounded `LIVE` field inventory is enabled. This pass does not redesign the generation model. It tightens the evidence, operator semantics, and fallback behavior around the implementation before expanding the final remaining runtime-policy milestone.

## Objective

Close the remaining correctness and observability gaps identified after the closure implementation:

1. Strengthen process-level tests so they prove the new configuration is actually consumed rather than merely proving the process survives.
2. Make concurrent-reload tests deterministic and assert the documented busy result.
3. Prevent `connect` and `logout` from silently restarting a healthy process when the control plane is unavailable.
4. Clarify and test exact CLI/JSON contracts.
5. Align test-count and release documentation with the actual validation matrix.

## Non-goals

- Do not broaden the `LIVE` inventory in this polish pass.
- Do not alter process-bound classifications.
- Do not add a second reload transport.
- Do not weaken local and server-side `check-config` validation.
- Do not replace the immutable generation or lease architecture.

## Workstream 1: Strengthen observational E2E tests

### Provider addition

Update the provider-addition integration test so it proves provider B is functional after publication:

- query `/v1/models` and assert the newly added model appears;
- issue a request addressed exclusively to provider B's model;
- assert mock upstream B receives the request;
- assert the request uses provider B's account and configured protocol contract;
- assert provider A does not receive the provider-B-only request;
- retain the unchanged PID/listener assertions.

A successful rehash plus continued operation of provider A is insufficient evidence that provider B was installed.

### Credential rotation

Extend the mock upstream to capture relevant authentication headers and request metadata. After rotating a credential:

- run `rehash`;
- send a new request;
- assert the new credential is present;
- assert the old credential is absent;
- verify an in-flight old-generation request may still complete using its original client state;
- verify the old client pool closes after drain.

Secrets must never be printed in assertion failures, logs, diagnostics, or snapshots. Tests should compare secret fingerprints or booleans where practical.

### Routing changes

Make routing-policy tests observe the new router state directly and behaviorally:

- expose or consume a secret-safe runtime snapshot containing account weights/priorities;
- assert the active generation snapshot reflects the new value;
- use two deterministic accounts/providers and a controlled selection policy where possible;
- verify requests admitted after publication use the new routing decision;
- verify an existing leased request retains the previous decision.

Avoid probabilistic assertions based on small request counts.

### Provider removal

Verify removal behavior end to end:

- start a slow stream through the provider being removed;
- rehash to remove it;
- assert the old stream completes;
- assert new model/routing discovery excludes the removed provider;
- assert a new request cannot select it;
- assert the persistent provider/account identity remains queryable for historical rows;
- assert resources close after generation drain.

## Workstream 2: Deterministic concurrency and publication conflict tests

The current process-level concurrent test may allow both commands to succeed when they do not overlap. Add a deterministic test seam:

- introduce a test-only candidate-preparation barrier or injected hook;
- hold reload transaction A after lock acquisition;
- invoke reload transaction B while A is definitely active;
- assert B receives exit code `4` and structured stage `busy`/`reload_in_progress`;
- release A and assert it succeeds;
- assert one and only one generation increment occurs;
- assert a `reload_publication_conflict` or `reload_busy` operational event is recorded without secrets.

Also test the stale expected-generation guard independently from the transaction lock.

## Workstream 3: Safe `connect` and `logout` fallback policy

Replace broad “control socket unavailable means restart” behavior with explicit process-state resolution.

Required decision tree:

1. Write and validate the candidate config.
2. If no server PID/health endpoint is active, start or restart according to the command's existing lifecycle contract.
3. If the server is healthy and the control socket succeeds, apply live rehash.
4. If the server is healthy but the control socket is unavailable, return `CONTROL_UNAVAILABLE`; do not restart implicitly.
5. If the control socket rejects restart-required changes, print the exact fields and require explicit `eggpool restart`.
6. If validation or preparation fails, preserve the active process and return nonzero.

Add tests for stale PID files, healthy server with missing socket, permission-denied socket, dead server, and explicitly requested restart behavior.

## Workstream 4: CLI and JSON contract tightening

Pin the human and machine-readable output contract:

- every outcome has a stable exit code;
- JSON output always contains `ok`, `stage`, `exit_code`, `generation`, `changed_sections`, `warnings`, `restart_required`, `retirement_pending`, and `message`;
- absent values use `null` or empty arrays consistently;
- secret-bearing old/new values remain redacted;
- human output distinguishes applied, no-op, draining, busy, restart-required, validation failure, digest mismatch, control unavailable, and preparation failure;
- `--json` emits no unrelated prose on stdout;
- diagnostic prose goes to stderr where appropriate.

Add snapshot-style tests for each outcome.

## Workstream 5: Diagnostics and release documentation

Ensure runtime diagnostics expose:

- active generation;
- active config fingerprint;
- retiring generation count and lease counts;
- last reload stage/outcome;
- changed sections;
- warnings count;
- restart-required fields;
- retirement pending/timeout status;
- stage durations;
- cleanup errors.

Update `docs/live-config-rehash.md`, deployment docs, changelog, architecture documentation, and CLI help with:

- exact current `LIVE` inventory;
- explicit process-bound inventory;
- no implicit restart guarantee for `rehash`;
- revised `connect`/`logout` fallback behavior;
- exit code and JSON schema tables;
- operational troubleshooting for stale or inaccessible control sockets.

Use consistent test totals: distinguish unit, integration, and full-suite counts.

## Required tests

- provider addition proves traffic reaches the new provider;
- credential rotation proves the next request uses the new credential;
- routing change proves deterministic new routing behavior;
- provider removal excludes new traffic while old streams finish;
- concurrent reload returns busy deterministically;
- stale candidate publication is rejected;
- healthy server plus unavailable socket never triggers implicit restart;
- dead server follows the documented start/restart path;
- every exit code and JSON outcome is pinned;
- diagnostics remain secret-safe;
- Ruff, Pyright, unit, integration, and full-suite checks pass.

## Acceptance criteria

The polish pass is complete when:

- process-level tests prove configuration consumption, not only process survival;
- reload lock and publication-conflict paths are deterministic and tested;
- `connect`/`logout` cannot unexpectedly interrupt a healthy server;
- human and JSON output contracts are stable;
- diagnostics and documentation accurately describe the bounded live feature;
- no `LIVE` field classification is broadened by this pass;
- all pre-existing live-rehash invariants remain intact.
