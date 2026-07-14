# Live Configuration Rehash Final Milestone D1 — Request-Policy Expansion

## Status

Detailed handoff plan for the first part of the final remaining live-rehash milestone.

Milestones A-C and the closure pass established validated transactional reload, runtime generations, request/stream leases, provider/account/routing live replacement, and process-level E2E coverage. D1 expands the bounded `LIVE` inventory to request-policy families that are already represented by generation-owned immutable policy objects or candidate request services.

## Objective

Enable safe live application of transcoding, compression, cache-policy, upstream request-policy, and related protocol-compatibility settings without changing process-owned storage topology or server construction.

## Scope

Candidate field families:

- transcoder enabled state;
- transcoder loss policy;
- native-protocol preference;
- protocol compatibility and capability policy consumed by the coordinator;
- compression profile selection;
- compression thresholds and deterministic limits;
- cache-synthesis/request annotation policy;
- semantic/prompt cache policy where represented as immutable request policy;
- upstream read/connect/write/pool timeout values where clients are generation-owned;
- retry and attempt policy consumed by generation-owned coordinators/clients;
- health/backoff thresholds that are generation-owned;
- model exposure/collapse behavior not already covered by the closure pass.

Explicitly out of scope:

- persistent cache storage location or schema;
- process-wide disk cache migration;
- database path or worker topology;
- Granian/server timeouts fixed at process construction;
- optional dependency/backend selection;
- middleware construction values.

## Phase 1: Ownership and consumption audit

For every candidate field, record:

- dotted config path;
- current disposition;
- runtime consumer;
- process-owned versus generation-owned status;
- whether the candidate builder reconstructs that consumer;
- whether any shared mutable object survives generation swap;
- retirement requirement;
- test proving the field takes effect after publication.

Do not classify a field `LIVE` merely because its value exists in `RuntimeGeneration`. Confirm the request hot path reads the generation's replacement object rather than a mirrored stale `app.state` reference or module-level singleton.

Add a field-consumption coverage test that fails when a proposed `LIVE` field has no registered consumer proof.

## Phase 2: Candidate construction completeness

Ensure `RuntimeGenerationBuilder` reconstructs, from the candidate config:

- transcoder policy/resolver;
- compression policy and tuning registry;
- cache-synthesis policy;
- request coordinator retry/attempt configuration;
- provider client timeout configuration;
- protocol/capability resolver state;
- health/backoff policy objects;
- any request transformation limits.

Candidate construction must not mutate active generation objects. Add identity assertions proving candidate policy/client objects are distinct where replacement is required.

Shared immutable stateless helpers may be reused only when configuration-independent.

## Phase 3: Reload classification

Move only audited fields to `ReloadDisposition.LIVE`.

Requirements:

- exact parent and expanded child paths are classified consistently;
- unknown children still fail closed to `RESTART_REQUIRED`;
- storage-topology fields remain restart-required;
- mixed diffs containing one restart-required field reject the entire transaction;
- secret-bearing values remain redacted in diffs and diagnostics.

Update the pinned expected live inventory test in the same commit as each classification change.

## Phase 4: Behavioral E2E tests

### Transcoding enable/disable

Run a real server with a client protocol that requires transcoding to reach the configured upstream.

- prove request succeeds while transcoding is enabled;
- rehash with transcoding disabled;
- prove a new request receives the expected protocol mismatch/capability failure;
- prove an old in-flight stream continues under its original policy;
- re-enable and prove new requests succeed without PID change.

### Loss policy and native preference

Use payload fixtures containing fields whose handling differs by loss policy.

- assert old generation follows policy A;
- rehash to policy B;
- assert new requests follow policy B;
- verify no cross-generation policy contamination.

### Compression policy

Use a deterministic payload above configured thresholds.

- assert baseline request is compressed or not compressed according to policy A;
- rehash threshold/profile;
- assert the next request follows policy B;
- verify payload equivalence and no capability regression;
- assert generation diagnostics expose only non-content policy metadata.

### Cache-synthesis policy

- enable/disable request annotation live;
- verify exact protected-prefix behavior;
- preserve native cache controls byte-for-byte;
- ensure storage topology is unchanged;
- verify old streams retain original annotation behavior.

### Upstream timeout policy

Use a delayed mock upstream:

- baseline request times out under a short generation-owned timeout;
- rehash to a longer timeout;
- new request succeeds;
- process-bound server timeout fields remain rejected.

## Phase 5: Performance and resource validation

Measure reload and steady-state impact:

- no additional lock acquisition on the hot path beyond the existing generation lease;
- no per-request config parsing;
- no policy reconstruction per request;
- old clients/policies retire after leases drain;
- repeated policy reloads do not leak HTTP clients, tasks, or tuning registries;
- memory returns near baseline after retirement;
- dispatch overhead remains within the existing regression threshold.

Add a bounded repeated-reload soak test covering at least 20 alternating policy generations.

## Phase 6: Documentation and operator output

Update the live-field inventory with a table separating:

- provider/account/routing live fields;
- request-policy live fields added by D1;
- storage/process fields still requiring restart.

CLI output should list changed sections such as `transcoder`, `compression`, `cache`, and `upstream` without exposing payloads or credentials.

## Tests

Required unit tests:

- complete disposition inventory;
- field-consumer ownership mapping;
- candidate object replacement identities;
- mixed LIVE/restart-required rejection;
- unknown-field fail-closed behavior;
- secret-safe diffs.

Required integration tests:

- transcoding enable/disable;
- loss-policy change;
- native preference change;
- compression threshold/profile change;
- cache-synthesis policy change;
- generation-owned timeout change;
- old-stream/new-request split semantics;
- repeated reload resource cleanup.

## Acceptance criteria

D1 is complete when:

- every newly `LIVE` field has a proven generation-owned consumer and candidate replacement path;
- request behavior changes immediately for requests admitted after publication;
- old in-flight requests retain their original policies;
- no persistent storage or process topology is changed live;
- repeated policy reloads leak no clients or tasks;
- process-bound mixed diffs reject atomically;
- all correctness, type, lint, integration, and performance checks pass.
