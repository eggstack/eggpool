# Plan 111 — Roadmap 103 Corrective Closure

Date: 2026-08-11
Status: complete
Planning baseline: `5da985f9f87eac1809ab62e5608f4a41700ccb39`
Related roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Related completed plans:

- `plans/104-local-exposure-and-log-redaction.md`
- `plans/106-provider-native-prompt-cache-translation.md`
- `plans/107-request-memory-and-body-limit-reduction.md`
- `plans/110-sbc-live-characterization-and-closure.md`

## Purpose

Perform one narrow corrective pass after the Roadmap 103 closure review.

The broader roadmap landed substantially as intended, but post-closure inspection found three concrete correctness gaps and one small configuration-accounting omission:

1. OpenAI → Anthropic explicit prompt-cache translation can leave the source-only `prompt_cache_breakpoint` field on the Anthropic provider payload after adding Anthropic `cache_control`.
2. Anthropic → OpenAI cache boundaries that were successfully translated can later be rediscovered by the legacy loss sweep and falsely reported as unsupported, including a possible false local rejection under `loss_policy = "reject"`.
3. The shipped SBC example is now safely loopback-only by default, but the implementation does not actually enforce the documented/accepted contract that non-loopback or wildcard binds require the existing server API key.
4. `server.max_request_body_bytes` is live-reloadable and behaviorally relevant but is missing from the runtime fingerprint projection.

This is a corrective closure plan, not a new optimization roadmap. Stop when these four defects are corrected and their focused contracts pass.

## Governing constraints

1. Do not reopen routing, retry/backoff, finalization, database durability, provider-pool sizing, compression architecture, request-memory ownership, or live-reload architecture except where a listed defect directly touches an existing seam.
2. Do not add a new runtime dependency, database migration/table, background task, cache store, auth subsystem, capability-discovery service, benchmark harness, telemetry framework, or CI job.
3. Keep the existing local/LAN SBC threat model. The required exposure correction is one small validation rule around the existing API-key mechanism, not production-grade authentication infrastructure.
4. Keep CI exactly as the current one-job Python 3.11 Ruff/Pyright/smoke gate.
5. Do not require the complete retained test corpus. Run focused capability tests plus the ordinary gate.
6. Do not create plan-numbered tests or broad replay/permutation matrices. Add the smallest regression tests at the owning module boundaries.
7. Do not change provider cache semantics beyond the two demonstrated translation defects. Preserve the conservative capability-gated design from Plan 106.
8. Do not invent cache TTL equivalence, synthesize cache keys, move Anthropic tool-definition cache boundaries to unrelated OpenAI message boundaries, or enable native controls merely from protocol-family strings.
9. Do not log or persist API keys, prompt-cache keys, raw prompt/tool content, or malformed payload bytes in new validation or regression evidence.
10. If implementation reveals an unrelated substantial issue, record it separately rather than expanding this plan.

## Current defect evidence

### Defect A — source OpenAI cache marker survives Anthropic translation

Current OpenAI → Anthropic content translation copies an OpenAI text part's `prompt_cache_breakpoint` into the target block and then calls `openai_breakpoint_to_anthropic()`. The helper adds Anthropic `cache_control = {"type": "ephemeral"}` for a verified capable target but does not consume/remove the source OpenAI field.

The target Anthropic body must never retain `prompt_cache_breakpoint`. A successful explicit mapping should emit only the target-native cache control at that placement. An unsupported/unverified mapping should drop the OpenAI-only marker and surface bounded loss metadata according to the existing policy.

Relevant files:

- `src/eggpool/transcoder/openai_to_anthropic.py`
- `src/eggpool/transcoder/cache_translation.py`
- `tests/unit/test_transcoder/test_openai_to_anthropic_body.py`

### Defect B — successfully translated Anthropic boundary can be classified as lost

Current Anthropic → OpenAI translation can correctly map an Anthropic message/system block `cache_control` to OpenAI `prompt_cache_breakpoint`. Later in the same encode path, a source-payload cache-boundary sweep can rediscover that original Anthropic boundary and emit `cache_control_unsupported_by_target_protocol` because the sweep does not exclude exact source paths already translated.

That warning belongs to `CACHE_CONTROL_LOSS_KINDS`; therefore `loss_policy = "reject"` can incorrectly reject a request whose boundary was represented successfully.

The corrected path must distinguish:

- boundary successfully mapped;
- boundary observed but unsupported/unrepresentable;
- malformed boundary;
- tool-definition boundary that remains intentionally unrepresentable on the OpenAI Chat Completions target.

A mapped source path must never be reclassified as dropped later in the same request.

Relevant files:

- `src/eggpool/transcoder/anthropic_to_openai.py`
- `src/eggpool/transcoder/cache_translation.py`
- `src/eggpool/transcoder/cache_stability.py` only if a tiny existing-helper adjustment is genuinely needed
- `src/eggpool/transcoder/errors.py` only if warning classification itself is proven incorrect; do not broaden the loss taxonomy unnecessarily
- `tests/unit/test_transcoder/test_anthropic_to_openai_body.py`

### Defect C — non-loopback/no-auth policy is documented but not enforced

Plan 104 and active documentation state that LAN/wildcard binding is an explicit operator action that requires the existing server API key. The bundled SBC profile now uses `127.0.0.1`, but both startup and `check-config` still accept a non-loopback host with no resolved API key because the existing startup auth validator only validates a key when one is present.

The shared validation contract must enforce the documented policy consistently at both `check-config`/rehash preflight and production startup.

The correction should reuse the existing API-key mechanism and stdlib only. Do not add users, roles, sessions, TLS, OAuth, secret storage, or network ACL machinery.

Relevant files:

- `src/eggpool/auth.py`
- `src/eggpool/config_validation.py`
- `src/eggpool/app.py`
- `tests/unit/test_auth.py`
- existing config-validation/startup tests if they own the shared contract
- active deployment/SBC docs only if wording must be reconciled with the final rule

### Defect D — live body limit omitted from runtime fingerprint

`server.max_request_body_bytes` is classified as `LIVE`, is consumed by request-body enforcement, and changes runtime behavior after rehash. The secret-safe runtime fingerprint's server projection does not include the field.

Add it to the existing projection and pin the behavioral fingerprint contract with one focused test. Do not redesign the fingerprint mechanism.

Relevant files:

- `src/eggpool/config_validation.py`
- existing config fingerprint/reload validation tests

## Workstream A — Correct OpenAI → Anthropic cache-marker consumption

### Required behavior

1. A recognized OpenAI explicit content breakpoint is a source-protocol instruction and must not survive on an Anthropic provider payload.
2. When the target model capability explicitly supports Anthropic prompt-cache breakpoints:
   - translate the source marker to Anthropic `cache_control` at the semantically corresponding block;
   - remove/consume `prompt_cache_breakpoint` from the provider-bound block;
   - record one successful `preserved_relocated` boundary event;
   - do not emit a loss warning for that boundary.
3. When target capability is absent/unverified:
   - remove the OpenAI-only marker from the Anthropic provider payload;
   - emit the existing bounded unsupported-target loss warning;
   - preserve existing `warn` versus `reject` policy semantics.
4. Do not silently carry malformed or unsupported source marker shapes into the Anthropic wire body merely because they are unknown to the translator. Use existing bounded warning/loss conventions where applicable; do not create a generic extension framework.
5. Do not alter message text, ordering, tool content, or unrelated cache controls.
6. Preserve the four-breakpoint target bound.

### Preferred implementation shape

Keep the correction local to the existing cache-translation helper/body translator. The target body should be constructed in target-protocol shape rather than retaining source-only fields and relying on an upstream provider to ignore them.

Avoid another whole-payload cleanup pass. Consuming the source marker at the point where it is interpreted is simpler and cheaper.

### Focused tests

At minimum pin:

- supported explicit breakpoint → `cache_control` present and `prompt_cache_breakpoint` absent;
- unsupported/unverified target → source marker absent from Anthropic output and loss warning emitted;
- `loss_policy = "reject"` still rejects an actually unrepresentable explicit breakpoint;
- multiple supported breakpoints remain bounded and do not leak source fields;
- request input object is not mutated.

Assertions should inspect exact relevant target blocks, not only the presence of `cache_control`.

## Workstream B — Prevent false Anthropic → OpenAI cache-loss classification

### Required behavior

1. Track exact source boundary locations that successfully map to OpenAI explicit breakpoints during the current request.
2. A successfully mapped boundary must not later produce:
   - `cache_control_unsupported_by_target_protocol`;
   - another dropped-boundary tracker annotation;
   - a `TranscodeLossError` under `loss_policy = "reject"`.
3. Unsupported/unverified message/system boundaries must continue to produce bounded loss handling.
4. Anthropic tool-definition `cache_control` remains explicitly unrepresentable on the current OpenAI Chat Completions translation surface and must still be reported as a loss; do not relocate it to an unrelated message block.
5. TTL mismatch behavior remains explicit. A successfully representable placement with a non-equivalent TTL may still be a genuine loss according to the existing policy; fixing the duplicate unsupported warning must not suppress real TTL loss.
6. Cache-key behavior is unchanged: no source key value is logged/persisted and no target cache key is synthesized.
7. Preserve the four-target-breakpoint limit.

### Preferred implementation shape

Use one small per-request set/collection of successfully translated **concrete source paths**, or an equivalent exact signal already available from the current tracker. Do not add a generalized cache-planning graph.

If concrete path identity is currently lost because loops use wildcard-style strings such as `messages[].content[].cache_control`, enumerate message/block indices at the existing translation loop and record concrete paths that match `extract_cache_boundaries()` output.

Then make the final unsupported-boundary sweep skip only paths proven successfully mapped. Do not broadly disable the sweep, because it still owns genuinely unrepresentable placements such as tool-definition boundaries.

### Focused tests

At minimum pin:

- supported Anthropic message boundary → OpenAI breakpoint, no unsupported-target warning;
- same request with `loss_policy = "reject"` succeeds when that boundary is the only cache annotation and carries no incompatible TTL;
- supported system boundary behaves the same;
- mixed request: one mapped message boundary plus one unrepresentable tool-definition boundary reports/rejects only the genuine tool loss;
- unsupported target capability continues to warn/reject as configured;
- TTL mismatch remains independently visible and rejectable when configured;
- more than four source boundaries still surfaces overflow without silently pretending all were mapped.

## Workstream C — Enforce the existing non-loopback API-key policy

### Final policy

Keep loopback/no-auth available for local-only EggPool use. Require the existing server API key for any bind that is not clearly loopback.

Accepted no-auth loopback values should stay intentionally small and deterministic:

- IPv4 loopback addresses (`127.0.0.0/8`);
- IPv6 loopback `::1`;
- `localhost` if the current config surface already permits it.

Treat wildcard and other host values as non-loopback, including at least:

- `0.0.0.0`;
- `::`;
- LAN/private addresses;
- public addresses;
- arbitrary hostnames that cannot be proven loopback without DNS resolution.

Do not perform DNS/network lookups during validation. Use stdlib `ipaddress` plus a small explicit `localhost` case if helpful.

### Shared validation contract

1. Add one small shared helper/validator that receives both the configured bind host and resolved API key.
2. `check-config`/rehash preflight and production startup must call the same rule so they cannot disagree.
3. Existing API-key shape/placeholder validation remains authoritative once a key is present.
4. Non-loopback + valid key remains supported.
5. Loopback + no key remains supported.
6. Non-loopback + no key fails before request admission with a concise operator-facing error that does not contain secret values.
7. Rehash must fail closed if a candidate configuration attempts to move from an authenticated/loopback-safe state to non-loopback/no-auth; the active generation remains unchanged.
8. Do not add an escape hatch unless one already exists and is clearly part of project policy. The post-Plan-104 contract is that LAN/wildcard use requires the server key.

### Focused tests

At minimum cover:

- `127.0.0.1` + no key → accepted;
- another `127.x.x.x` loopback + no key → accepted if using stdlib loopback semantics;
- `::1` + no key → accepted;
- `localhost` + no key → accepted only if intentionally supported by final helper;
- `0.0.0.0` + no key → rejected;
- `::` + no key → rejected;
- representative LAN address + no key → rejected;
- non-loopback + valid key → accepted;
- `check-config` and startup call the same rule/result;
- invalid/placeholder key behavior remains unchanged;
- no error/log output contains key material.

Do not add socket-binding or external-network integration tests; this is deterministic config/startup validation.

## Workstream D — Include the body limit in runtime fingerprinting

1. Add `server.max_request_body_bytes` to the existing server fingerprint projection.
2. Confirm two otherwise-identical configs with different body limits produce different runtime fingerprints.
3. Confirm secret redaction and deterministic ordering behavior remain unchanged.
4. Do not use this task to redesign semantic no-op detection or reload diffing.
5. Keep `server.max_request_body_bytes` classified `LIVE`; the existing dynamic middleware/generation behavior remains unchanged unless a focused test proves a direct regression.

### Focused tests

Pin only:

- changing `max_request_body_bytes` changes the runtime fingerprint;
- identical value/config remains stable across repeated fingerprint calculation;
- current secret-field redaction tests continue to pass.

## Workstream E — Documentation/status reconciliation

Update only active material made inaccurate by the corrective implementation.

Required reconciliation:

- Plan 111 status and closure record;
- active transcode documentation if it currently implies source cache-marker preservation on the target wire;
- active security/deployment/SBC wording only if needed to state that the non-loopback API-key requirement is now enforced rather than advisory;
- `AGENTS.md`/architecture invariants only if the implementation changes their current factual wording.

Do not rewrite completed Plans 103–110 to erase their historical closure evidence. Plan 111 should serve as the explicit post-closure corrective record. A tiny note may be added to Roadmap 103 only if repository convention requires linking this corrective follow-up, but do not reopen or expand the roadmap checklist.

## Verification

Run the smallest owning tests first.

Suggested focused commands should use existing files/test selectors after implementation, for example:

```bash
uv run pytest tests/unit/test_transcoder/test_openai_to_anthropic_body.py -q
uv run pytest tests/unit/test_transcoder/test_anthropic_to_openai_body.py -q
uv run pytest tests/unit/test_auth.py -q
uv run pytest tests/unit/test_config_reload_policy.py -q
```

Also run any existing config-validation/startup/rehash-focused file that owns the shared exposure rule and runtime fingerprint contract. Do not create a broad new integration suite merely to satisfy this plan.

Then run the ordinary repository gate exactly as currently documented:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

For exposure validation, also exercise temporary config files for:

- loopback/no-auth success;
- non-loopback/no-auth rejection;
- non-loopback/valid-key success without printing the key.

No live provider credentials, Raspberry Pi workload replay, full-suite run, benchmark, soak test, hardware test, or CI expansion is required.

## Acceptance criteria

### Cache translation

- [x] A supported OpenAI explicit `prompt_cache_breakpoint` maps to Anthropic `cache_control` and the source-only `prompt_cache_breakpoint` is absent from the Anthropic provider payload.
- [x] An unsupported/unverified OpenAI breakpoint is removed from the Anthropic target payload and follows existing bounded loss-policy behavior.
- [x] OpenAI → Anthropic translation does not mutate the caller's source payload while consuming source-only fields from the target representation.
- [x] Successfully translated Anthropic message/system cache boundaries are not later reported as `cache_control_unsupported_by_target_protocol`.
- [x] `loss_policy = "reject"` does not reject an otherwise representable Anthropic → OpenAI cache boundary merely because the legacy source sweep sees it again.
- [x] Exact successfully mapped source paths are distinguished from genuinely unrepresentable paths.
- [x] Anthropic tool-definition cache boundaries remain explicit losses on the current OpenAI Chat Completions target and are never relocated to unrelated content.
- [x] TTL mismatches remain visible/rejectable when genuinely non-equivalent; the duplicate-loss fix does not hide real TTL loss.
- [x] Four-breakpoint bounds remain enforced in both directions.
- [x] No cache key is synthesized, and no source cache-key value or prompt/tool content is added to logs/persistence.
- [x] Generic compatible providers still require explicit capability facts before native cache controls are emitted.

### Exposure/auth validation

- [x] Loopback/no-auth remains supported for local-only use.
- [x] `0.0.0.0`, `::`, LAN/public addresses, and other non-provably-loopback binds without a resolved API key are rejected before request admission.
- [x] Non-loopback + valid existing server API key remains supported.
- [x] `check-config`/rehash validation and production startup use the same non-loopback/auth rule and cannot disagree.
- [x] A failed rehash candidate cannot publish a non-loopback/no-auth generation.
- [x] Existing placeholder/key-shape validation remains intact.
- [x] No new auth service, credential storage mechanism, network lookup, role system, or runtime dependency is introduced.
- [x] Shipped source/bundled SBC configs remain loopback-safe and pass `check-config`.

### Fingerprint/config

- [x] `server.max_request_body_bytes` participates in the runtime fingerprint.
- [x] Changing only `server.max_request_body_bytes` changes the fingerprint deterministically.
- [x] Existing secret-safe fingerprint behavior remains intact.
- [x] `server.max_request_body_bytes` remains live-reloadable and its existing body-limit behavior is not regressed.

### Scope and verification

- [x] No new core/runtime dependency is added.
- [x] No DB migration/table, background task, cache store, benchmark, soak harness, telemetry subsystem, or CI job is added.
- [x] Current one-job Python 3.11 CI shape remains unchanged.
- [x] Focused transcode/cache/auth/config/rehash tests pass.
- [x] Ruff format/check, Pyright, smoke tests, and both shipped config checks pass.
- [x] Plan 111 records the implementation commit SHA, exact verification results, and any intentionally retained lossy cache semantics.
- [x] No unrelated Roadmap 103 subsystem is redesigned during this pass.

## Rejection conditions

Do not close Plan 111 if any of the following is true:

- Anthropic provider payloads still carry OpenAI `prompt_cache_breakpoint` fields after translation;
- a successfully mapped Anthropic cache boundary can still produce the legacy unsupported-target warning/rejection for the same source path;
- the fix suppresses genuine TTL/tool-definition/overflow loss handling;
- non-loopback/no-auth remains accepted while active docs continue to state that an API key is required;
- `check-config` accepts an exposure configuration that startup rejects, or vice versa;
- the body-limit fingerprint omission remains;
- the implementation introduces a new generic capability/cache/auth framework;
- CI or routine verification grows beyond the current lightweight project policy;
- closure claims live-provider or target-device evidence that was not actually run.

## Closure record

Implementation commit: `74b4266c2273bcb09bafd4443116a45e8e781f53`

All four corrective workstreams are complete. The implementation consumes
OpenAI-only breakpoint markers at the Anthropic target boundary, tracks exact
successfully mapped Anthropic source paths so the later loss sweep does not
reclassify them, enforces the non-loopback server-key rule through the shared
startup/`check-config` validator, and includes
`server.max_request_body_bytes` in the secret-safe runtime fingerprint.

Verification completed locally on 2026-08-11:

- Focused transcode/auth/config/reload/startup/API-key tests: `203 passed`.
- `uv sync --frozen --extra ci`: passed.
- `uv run ruff format --check src/ tests/ scripts/`: passed.
- `uv run ruff check src/ tests/ scripts/`: passed.
- `uv run pyright src/ scripts/`: `0 errors, 0 warnings, 0 informations`.
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1`:
  `14 passed`.
- `uv run eggpool --config config.example.toml check-config`: passed.
- `uv run eggpool --config config.sbc.example.toml check-config`: passed.

No live-provider, Raspberry Pi, workload replay, benchmark, or full-suite
verification was claimed or required. The intentionally retained lossy
semantics are provider-specific TTL mismatch reporting, four-breakpoint
overflow handling, and Anthropic tool-definition cache boundaries on the
OpenAI Chat Completions surface; none is relocated or silently suppressed.

## GPT-5.6 Luna implementation sequence

1. Read this plan and the current implementations in `cache_translation.py`, both body transcoders, `auth.py`, `config_validation.py`, `app.py`, and the runtime fingerprint/reload policy.
2. Reproduce Defect A with the existing OpenAI → Anthropic unit test shape and strengthen the assertion to inspect exact target fields.
3. Correct source-marker consumption at the existing translation boundary; run the focused OpenAI → Anthropic tests.
4. Reproduce Defect B under both `warn` and `reject` using one representable Anthropic message boundary.
5. Track exact successfully mapped source paths and exclude only those from the final unsupported-boundary sweep; retain genuine tool-definition/TTL/overflow losses.
6. Run focused Anthropic → OpenAI tests including a mixed mapped + unrepresentable boundary case.
7. Add the smallest shared non-loopback/auth validation rule and call it from both config preflight and startup. Run auth/config/startup/rehash-focused tests.
8. Add `server.max_request_body_bytes` to the existing runtime fingerprint projection and one deterministic regression test.
9. Run Ruff/Pyright/smoke/both `check-config` commands and temporary loopback/non-loopback config checks.
10. Reconcile only active docs/invariants made stale by these changes.
11. Record the implementation SHA and exact verification results in this plan, mark it complete, and stop.
