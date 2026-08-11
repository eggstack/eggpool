# Plan 112 — Final Cache Translation Corrective Closure

Date: 2026-08-11
Status: planned
Planning baseline: `69210fc9fa70f9610f8c6f5a8beee369438af995`
Related roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Related corrective plan: `plans/111-roadmap-103-corrective-closure.md`

## Purpose

Perform one final, tightly scoped corrective pass for the two prompt-cache translation edge cases found after Plan 111 closed.

Plan 111 successfully landed the non-loopback/API-key validation rule, runtime-fingerprint correction, source-marker consumption for actual OpenAI cache breakpoints, and exact-path suppression for successfully mapped Anthropic cache boundaries. The remaining defects are both local control-flow/return-semantics issues in the existing cache translators:

1. OpenAI → Anthropic currently invokes the cache-breakpoint helper for ordinary text parts that have **no** `prompt_cache_breakpoint`. The helper interprets absence as malformed input and emits `cache_breakpoint_invalid_shape`; because that warning is protected by `loss_policy = "reject"`, an ordinary content-array request can be rejected even though the client never requested prompt caching.
2. Anthropic → OpenAI currently treats the helper's boolean return as "a breakpoint was mapped" even when the helper merely observed an invalid, unsupported, or overflowed cache boundary. This can set `mapped_breakpoint = True` and emit top-level `prompt_cache_options = {"mode": "explicit"}` for a target whose explicit prompt-cache capability is absent/unverified or for a request where no breakpoint was actually emitted.

This is not a new roadmap. Correct these two semantics, add the smallest regression coverage needed to pin them, run the ordinary repository gate, record closure, and stop.

## Governing constraints

1. Do not reopen auth/exposure validation, runtime fingerprinting, body-limit behavior, routing, retry/backoff, provider suppression, finalization, request-memory ownership, compression, SQLite, provider-pool sizing, or Raspberry Pi characterization.
2. Do not add a dependency, database migration/table, background task, capability-discovery service, cache store, cache key, telemetry subsystem, benchmark, soak harness, hardware test, or CI job.
3. Keep the existing capability-gated provider-native cache design. Generic OpenAI-/Anthropic-compatible providers must not receive native cache controls merely because of protocol family.
4. Preserve the existing four-breakpoint bound and explicit TTL-loss behavior.
5. Preserve the existing rule that Anthropic tool-definition cache boundaries remain an explicit loss on the current OpenAI Chat Completions translation surface rather than being moved to unrelated message content.
6. No new generalized result object, cache planning graph, translation framework, or whole-payload cleanup pass is justified for these defects unless the current two-boolean contract proves impossible to repair locally.
7. Prefer changing existing helper semantics so callers become correct by construction.
8. Do not broaden routine verification. Use focused owning tests plus the existing one-job repository gate.
9. Do not create plan-numbered test files or historical/replay matrices.
10. Do not log or persist prompt/cache/tool content, raw malformed values, cache keys, or credentials as part of the correction or closure evidence.

## Current defect evidence

### Defect A — missing OpenAI breakpoint is treated as malformed

Current `openai_breakpoint_to_anthropic()` begins by removing the marker from the target-side block:

```python
marker_raw = part.pop("prompt_cache_breakpoint", None)
if not isinstance(marker_raw, dict):
    warnings.append(
        {"kind": "cache_breakpoint_invalid_shape", "field": source_path}
    )
    ...
    return False
```

That logic correctly handles a **present but malformed** marker, but it cannot distinguish malformed presence from ordinary absence.

The current OpenAI → Anthropic body paths call this helper for text parts regardless of whether the source part carried a marker:

- `_translate_openai_content_to_anthropic()` for user/system/developer content lists;
- the assistant content-list loop in `OpenAIToAnthropic.encode_request()`.

Consequences:

- ordinary text parts can receive a false `cache_breakpoint_invalid_shape` warning;
- a request with `loss_policy = "reject"` can raise `TranscodeLossError` despite containing no cache breakpoint;
- cache-boundary observability can report dropped-invalid-shape annotations that never existed in the source request.

Relevant files:

- `src/eggpool/transcoder/cache_translation.py`
- `src/eggpool/transcoder/openai_to_anthropic.py`
- `src/eggpool/transcoder/errors.py` only for verification of the existing protected-loss taxonomy; no taxonomy change is expected
- `tests/unit/test_transcoder/test_openai_to_anthropic_body.py`

### Defect B — handled Anthropic boundary is confused with successfully mapped boundary

Current `anthropic_boundary_to_openai()` returns `True` for several different outcomes:

- a valid boundary successfully translated to `prompt_cache_breakpoint`;
- malformed `cache_control`;
- target capability absent/unverified;
- target four-breakpoint limit exceeded.

The callers interpret any `True` as a successful mapping:

```python
if anthropic_boundary_to_openai(...):
    mapped_breakpoint = True
    ...
```

Later, any true `mapped_breakpoint` causes:

```python
out["prompt_cache_options"] = {"mode": "explicit"}
```

Therefore a source boundary that was *not* represented on the target can still cause a target-native explicit-cache option to be emitted.

This is especially incorrect for a generic/unverified OpenAI-compatible upstream: EggPool can correctly warn that explicit cache breakpoints are unsupported while simultaneously emitting an OpenAI explicit-cache control.

Relevant files:

- `src/eggpool/transcoder/cache_translation.py`
- `src/eggpool/transcoder/anthropic_to_openai.py`
- `tests/unit/test_transcoder/test_anthropic_to_openai_body.py`

## Workstream A — Make absent OpenAI breakpoint a no-op

### Required semantic contract

`openai_breakpoint_to_anthropic()` must distinguish these three states:

1. **marker absent** — nothing was requested; return without warning, tracker annotation, cache-control mutation, or loss-policy consequence;
2. **marker present and valid explicit** — consume the source-only marker and either map it to Anthropic `cache_control` or record a real unsupported/overflow loss according to target capability/bounds;
3. **marker present but malformed/unsupported shape** — consume the source-only marker, emit bounded `cache_breakpoint_invalid_shape`, record the existing dropped-invalid-shape annotation, and allow existing `warn`/`reject` policy to apply.

The important invariant is:

> absence is not loss.

### Preferred implementation shape

Prefer one local helper guard:

```python
if "prompt_cache_breakpoint" not in part:
    return False
marker_raw = part.pop("prompt_cache_breakpoint")
...
```

or an equivalent sentinel-based distinction.

This is preferable to adding `if` guards at every body-transcoder call site because the helper should itself be safe when called against an ordinary target block. A caller must not need to know that absence is special just to avoid a false warning.

Do not stop calling the helper broadly if doing so would leave duplicate semantics across user/system/assistant paths; centralizing the absence contract is safer and smaller.

### Required behavior

1. Ordinary OpenAI text content arrays without `prompt_cache_breakpoint` produce no `cache_breakpoint_invalid_shape` warning.
2. They produce no cache-boundary tracker annotation attributable to a nonexistent breakpoint.
3. `loss_policy = "reject"` does not reject an otherwise valid request solely because it uses OpenAI content-part arrays.
4. The same no-op semantics hold for ordinary assistant text-part arrays, not only user content.
5. System/developer content lists without cache markers likewise remain ordinary text translation.
6. A marker that is actually present but malformed remains a real loss and is still removed from the Anthropic target body.
7. A valid supported marker still maps to `cache_control` with the source-only marker absent from target output.
8. A valid unsupported/unverified marker still follows the existing unsupported-target warning/rejection semantics.
9. The caller's original source payload remains unmodified.
10. No extra whole-payload walk is added.

### Focused tests

Add or tighten capability-based tests in the existing OpenAI → Anthropic body suite.

At minimum pin:

- ordinary user content-list text part, default `warn` policy → no `cache_breakpoint_invalid_shape` warning;
- ordinary user content-list text part, `loss_policy = "reject"` → request succeeds and contains no target cache fields;
- ordinary assistant content-list text part → no false cache warning/rejection;
- present malformed marker → source-only field absent from target, `cache_breakpoint_invalid_shape` present, and `loss_policy = "reject"` rejects;
- existing supported-marker and unsupported-marker tests continue to pass;
- source payload remains unchanged.

If a system/developer content-list regression is already naturally covered by an existing focused test, extend that assertion rather than creating a redundant matrix.

## Workstream B — Make Anthropic helper return mean "successfully mapped"

### Required semantic contract

The boolean return from `anthropic_boundary_to_openai()` should have one narrow meaning:

> `True` means this source boundary produced a target `prompt_cache_breakpoint`.

It must return `False` for:

- no `cache_control` on the source part;
- malformed cache-control shape;
- target explicit-breakpoint capability absent/unverified;
- target breakpoint limit exceeded.

Those non-mapped states may still emit their current bounded warnings/tracker annotations. The return value should describe mapping success, not merely whether the helper observed/handled cache-related input.

A small result enum/object is unnecessary unless a current caller genuinely needs more than mapped/not-mapped after inspection. The existing warnings and tracker already carry loss classification.

### Required caller behavior

1. Set `mapped_breakpoint = True` only when a target `prompt_cache_breakpoint` was actually emitted.
2. Set `message_has_breakpoint = True` only when a target `prompt_cache_breakpoint` was actually emitted for that message.
3. Add a path to `mapped_source_paths` only when the corresponding target breakpoint was actually emitted.
4. Emit top-level `prompt_cache_options = {"mode": "explicit"}` only if at least one actual target breakpoint exists.
5. An unsupported/unverified target must receive neither `prompt_cache_breakpoint` nor `prompt_cache_options`.
6. A malformed source boundary must receive neither target cache field while still producing its real malformed-boundary warning.
7. A fifth/overflow boundary must not cause a phantom mapping; the first four successfully mapped boundaries may still justify top-level explicit mode, but overflow by itself must not.
8. Successfully mapped message/system boundaries continue to be excluded from the later legacy unsupported-boundary sweep via exact source paths.
9. Unsupported/unmapped paths continue to be discoverable by the existing loss sweep or their direct helper warning without duplicate loss classification.
10. TTL mismatch remains independently visible. A supported placement with a TTL mismatch may still map structurally and later reject under `loss_policy = "reject"` because the TTL mismatch is genuine.

### Preferred implementation shape

The smallest intended change is to alter `anthropic_boundary_to_openai()` return values so only the successful mapping branch returns `True`.

Then simplify/verify callers around that contract. An optional defensive assertion may check for `"prompt_cache_breakpoint" in translated_source` before recording the source path, but do not add redundant state machines.

Do not replace the exact-path set introduced by Plan 111; it solves the original duplicate-loss defect and should remain.

### Focused tests

At minimum pin:

- source message boundary + no verified OpenAI breakpoint capability → warning/loss as today, but **no** `prompt_cache_breakpoint` and **no** `prompt_cache_options`;
- same unsupported request under `loss_policy = "reject"` rejects for the genuine unsupported boundary, not because unsupported native target fields were emitted;
- malformed message/system boundary → no target breakpoint/options and bounded malformed warning;
- supported message boundary → target breakpoint + top-level explicit mode, no duplicate unsupported warning;
- supported system boundary → same;
- four supported boundaries → explicit mode remains present and all four mappings remain bounded;
- fifth boundary → overflow warning/loss, no fifth target breakpoint; the presence of the first four may still keep explicit mode enabled;
- request containing only an overflow/unsupported/malformed boundary and no successful mappings → top-level explicit mode absent;
- mixed request with one successful mapped boundary and one unrepresentable tool-definition boundary → explicit mode present because of the real mapping, while the tool-definition loss remains independently visible/rejectable.

Avoid creating every cross-product of policy × placement × capability. Cover semantic branch points once.

## Workstream C — Focused regression audit

After implementing Workstreams A and B, inspect the immediately adjacent cache contracts for accidental semantic drift.

Verify, without redesign:

- `CACHE_CONTROL_LOSS_KINDS` still includes actual malformed/unsupported/overflow/TTL/cache-key losses;
- no new warning kind is required for ordinary absence;
- `openai_breakpoint_to_anthropic()` still consumes source-only markers when they actually exist;
- `anthropic_boundary_to_openai()` still records unsupported/invalid/overflow tracker annotations even when returning `False`;
- exact `mapped_source_paths` still suppress only genuinely mapped boundaries from the final source sweep;
- Anthropic tool-definition cache controls remain explicit losses and are not added to `mapped_source_paths`;
- no provider-native cache field is emitted solely because the provider speaks an OpenAI- or Anthropic-compatible protocol.

If this audit exposes another problem outside these exact semantics, do not expand Plan 112 unless it is a direct one-line consequence of the return-contract correction. Record unrelated work separately.

## Workstream D — Documentation and closure truthfulness

This correction should require little or no user-facing documentation change because the current docs already describe the desired semantics rather than the defective edge cases.

Required closure work:

1. Mark Plan 112 complete only after focused tests and the ordinary gate pass.
2. Record the implementation commit SHA and exact focused test count/commands actually run.
3. If Plan 111 is historically marked complete, do not rewrite its closure record as if the edge cases never occurred. Plan 112 is the explicit post-closure correction.
4. Update active transcode documentation only if implementation wording reveals a factual mismatch after the fix.
5. Do not reopen Roadmap 103 or create another roadmap/closure plan if Plan 112 acceptance criteria pass.

## Verification

Run the smallest owning suites first:

```bash
uv run pytest tests/unit/test_transcoder/test_openai_to_anthropic_body.py -q
uv run pytest tests/unit/test_transcoder/test_anthropic_to_openai_body.py -q
```

If existing focused integration tests directly exercise provider-bound cache translation, run only the relevant existing selectors/files. Do not create a new integration suite for this pass.

Then run the ordinary repository gate exactly as currently defined:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

A full retained-suite pass is optional and is not a closure requirement. No live provider credentials, Raspberry Pi rerun, benchmark, soak workload, or hardware CI is required.

## Explicit acceptance criteria

### OpenAI → Anthropic absence semantics

- [ ] A text content part with no `prompt_cache_breakpoint` is treated as ordinary content, not malformed cache input.
- [ ] Ordinary user content-part arrays emit no `cache_breakpoint_invalid_shape` warning.
- [ ] Ordinary assistant content-part arrays emit no `cache_breakpoint_invalid_shape` warning.
- [ ] Ordinary system/developer text-part arrays do not create false cache-loss metadata.
- [ ] `loss_policy = "reject"` does not reject an otherwise valid request that contains no cache breakpoint.
- [ ] No cache-boundary tracker annotation is created for a breakpoint that was absent from the source.
- [ ] A present malformed marker still emits `cache_breakpoint_invalid_shape`, is removed from the Anthropic target body, and remains rejectable under the existing loss policy.
- [ ] A present valid supported marker still maps to Anthropic `cache_control` and leaves no source-only marker on the target wire.
- [ ] A present valid unsupported marker still follows the existing bounded unsupported-target semantics.
- [ ] The source request object remains unmodified.

### Anthropic → OpenAI mapping-success semantics

- [ ] `anthropic_boundary_to_openai()` returns success only when it actually emits a target `prompt_cache_breakpoint`.
- [ ] Missing, malformed, unsupported/unverified, and overflowed source boundaries do not count as successful mappings.
- [ ] `mapped_breakpoint` becomes true only after at least one actual target breakpoint is emitted.
- [ ] `message_has_breakpoint` becomes true only for a message that actually receives a target breakpoint.
- [ ] `mapped_source_paths` contains only exact paths that were successfully represented on the target.
- [ ] `prompt_cache_options = {"mode": "explicit"}` is absent when zero target breakpoints were emitted.
- [ ] An unverified/generic OpenAI-compatible target receives neither explicit content breakpoints nor `prompt_cache_options` solely because an Anthropic source cache boundary existed.
- [ ] Malformed source cache controls do not cause target-native explicit cache fields to be emitted.
- [ ] A fifth/overflow boundary does not produce a fifth target breakpoint or count as mapped.
- [ ] One or more genuinely mapped boundaries still cause top-level explicit mode to be emitted as required by the existing OpenAI translation contract.
- [ ] Successfully mapped message/system boundaries are not reclassified by the later loss sweep.
- [ ] Genuine unsupported boundaries, including Anthropic tool-definition cache controls, remain visible and rejectable according to existing policy.
- [ ] Genuine TTL mismatch remains visible/rejectable and is not hidden by the boolean return correction.

### Scope and regression control

- [ ] No auth/exposure, runtime fingerprint, request body limit, routing, DB, finalization, compression, request-memory, pool-size, or SBC characterization behavior is changed.
- [ ] No dependency, database migration, background task, cache store, telemetry component, benchmark/soak harness, or CI job is added.
- [ ] Current one-job Python 3.11 CI shape remains unchanged.
- [ ] The existing four-breakpoint bound is unchanged.
- [ ] No cache key is synthesized.
- [ ] No source cache-key value, prompt/tool content, raw malformed payload value, or credential is added to logs/persistence/closure evidence.
- [ ] Focused OpenAI → Anthropic and Anthropic → OpenAI body tests pass.
- [ ] Ruff format/check, Pyright, smoke tests, and both shipped config checks pass.
- [ ] Plan 112 records exact implementation and verification evidence before being marked complete.
- [ ] After these criteria pass, Roadmap 103/Plan 111 follow-up work is considered closed; do not create another optimization or verification phase from this line of work.

## Rejection conditions

Do not close Plan 112 if any of the following remains true:

- an ordinary OpenAI text content part without a cache marker produces `cache_breakpoint_invalid_shape`;
- `loss_policy = "reject"` can reject a no-cache content-array request because of a nonexistent breakpoint;
- an unsupported/unverified Anthropic → OpenAI cache boundary can cause `prompt_cache_options` to be emitted despite zero actual target breakpoints;
- malformed or overflowed Anthropic boundaries count as successful mappings;
- an explicit native cache field is sent to a generic compatible target without verified capability;
- a genuinely mapped Anthropic boundary is again misclassified as unsupported by the final source sweep;
- TTL/tool-definition/overflow losses are accidentally suppressed rather than remaining explicit;
- source-only OpenAI breakpoint fields leak onto the Anthropic wire;
- a new abstraction/framework is introduced that materially exceeds the two-branch correction needed here;
- CI, dependencies, schema, runtime tasks, or unrelated subsystems grow as part of this pass;
- closure claims provider/hardware/full-suite evidence that was not actually run.

## GPT-5.6 Luna implementation sequence

1. Read this plan, Plan 111's closure record, and the current implementations of `cache_translation.py`, `openai_to_anthropic.py`, and `anthropic_to_openai.py`.
2. Reproduce Defect A with an ordinary OpenAI text content-part array and inspect warnings under both `warn` and `reject` policies.
3. Fix `openai_breakpoint_to_anthropic()` so marker absence is a no-op while present malformed input remains a real loss.
4. Add the smallest tests for ordinary user/assistant content arrays, no-cache `reject` success, and present malformed-marker rejection.
5. Reproduce Defect B using an Anthropic message cache boundary with no verified OpenAI breakpoint capability and confirm current output can set explicit mode without a real mapping.
6. Change `anthropic_boundary_to_openai()` boolean semantics so `True` means actual target breakpoint emitted; keep warning/tracker side effects for non-mapped loss branches.
7. Verify callers set `mapped_breakpoint`, `message_has_breakpoint`, and `mapped_source_paths` only from actual mappings.
8. Add focused unsupported/malformed/overflow/supported/mixed regression assertions without building a permutation matrix.
9. Run both owning unit files. Inspect warnings and target bodies, not only exception status.
10. Run Ruff format/check, Pyright, smoke tests, and both shipped config checks.
11. Update Plan 112 with implementation SHA and truthful verification evidence; change status to complete only if every acceptance criterion is satisfied.
12. Stop. Do not reopen Roadmap 103 or create another follow-up phase if this plan closes cleanly.
