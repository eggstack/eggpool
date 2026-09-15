# Plan 196: Codex Responses compatibility closure and live qualification

> **Status:** READY FOR IMPLEMENTATION
>
> **Baseline:** Eggpool `main` at `9729a4fbe5945aa3a78f7e90837c32ea9bccbf14`
>
> **Parent:** Plans 192–195
>
> **Relevant implementation commits:** `0285cffd1179623cfcffd8a3b22018f609cc0964` (native Responses request preservation), `b3735c00f5ee24486a9d1b139d130a2183e558d7` (native stream forwarding + translated lifecycle synthesis), `9729a4fbe5945aa3a78f7e90837c32ea9bccbf14` (Codex conformance/custom-tool closure)
>
> **Audited Codex provenance:** OpenAI Codex `508a006d7aaa485ac0367c9e45c69ebb948af518`; local smoke script previously observed `codex-cli 0.154.0`
>
> **Scope:** close the remaining qualification gaps after Plans 193–195 without reopening the Responses architecture. Add a real parallel-tool conformance case, turn the opt-in Codex smoke into a genuine text + tool-loop qualification, reconcile plan/documentation status, and determine whether the one observed CI timeout failure is a harmless flake or a deterministic-test defect.

## Executive summary

The major Codex/Responses compatibility defects identified in the September 15 audit are now implemented:

- Responses-to-Responses requests retain their source-native representation and only rewrite Eggpool-owned fields such as `model` when an alias requires it;
- native Responses SSE has an observe-and-forward path instead of being decoded and reconstructed;
- translated Responses streams use bounded stateful lifecycle synthesis;
- assistant, reasoning, and tool output items close with authoritative `response.output_item.done` events;
- tool invocation `call_id` remains distinct from generated Responses item IDs;
- ordinary function tools and Responses custom/freeform tools have a small provider-neutral distinction;
- custom/freeform tools can bridge deterministically through function-only upstreams and reconstruct as `custom_tool_call` downstream;
- malformed freeform wrappers fail closed;
- `response.completed` remains required for successful Responses stream completion;
- Eggpool's standard `/v1/models` contract was not replaced with a Codex-private catalog.

The architecture should therefore be treated as **closed unless this plan produces concrete contradictory evidence**. This is a qualification and polish pass, not another compatibility redesign.

Two specific gaps remain:

1. the current deterministic Codex suite does not actually exercise two simultaneously active/interleaved tool calls, despite one test name referring to parallel-call identity;
2. the opt-in live smoke only proves a text response. Plan 195's own acceptance criteria called for a real current Codex HTTP/SSE run covering both text and one client-executed tool loop, but credentials were unavailable during implementation and the smoke script correctly took its skip path.

A third, independent repository-quality item was observed while reviewing the landing commit: the first CI attempt for `9729a4f...` failed in `operations_o002::client_timeout_is_bounded_and_handler_survives_disconnect` with `left: 0`, `right: 1`, while an immediate rerun of the exact same SHA passed the full matrix. This plan performs a bounded reproducibility check and stabilizes the test only if necessary. It must not become a broad control-plane refactor.

---

# Guardrails and non-goals

This closure pass must stay narrow.

Do **not**:

- redesign `CanonicalRequest`, `CanonicalEvent`, or the Responses preservation envelope;
- replace native Responses passthrough with canonical reconstruction;
- add Codex-specific production branches where a general wire invariant already exists;
- add a Codex or OpenCodex runtime dependency;
- fork Codex;
- change `/v1/models` into Codex's rich model-catalog schema;
- implement automatic Codex model discovery as part of this pass;
- add WebSocket Responses support merely for Codex; the qualified integration remains HTTP/SSE with `supports_websockets = false`;
- expand server-side tool emulation for shell, web search, image generation, namespace/tool-search, or other native-only tool forms;
- add new production dependencies;
- loosen stateless Responses policy (`store=true`, `previous_response_id`, conversations, or background execution remain rejected according to the current contract);
- hide a flaky test by simply increasing arbitrary sleep durations or global timeouts.

If a new deterministic or live qualification exposes a production defect, make the smallest general fix at the existing wire/control boundary and add a regression test. Do not pre-emptively refactor working code.

---

# Workstream 1 — Add a genuine parallel-tool Codex conformance fixture

Primary file:

- `rust/tests/codex_responses_compat.rs`

Possible supporting file only if a lower-level invariant needs direct coverage:

- `rust/tests/wire_stream.rs`

## 1.1 Replace the current naming/coverage mismatch

The existing test named approximately:

```text
translated_streams_keep_parallel_call_identity_and_eof_is_failure
```

currently verifies translated/native forwarding mode selection and partial-EOF classification, but it does not feed two concurrent tool calls through the stateful Responses encoder.

Do one of the following:

- rename that test to describe what it actually covers and add a new dedicated parallel-call test; **preferred**;
- or expand it, but only if the resulting test remains easy to diagnose.

Prefer separate tests because EOF/forwarding-mode behavior and parallel tool identity are independent invariants.

## 1.2 Build a two-call interleaving fixture

Exercise a translated provider path, preferably OpenAI Chat SSE -> canonical events -> Responses SSE because that path naturally exposes indexed tool-call deltas.

Construct two simultaneous ordinary function calls with intentionally non-lexical call IDs so a `BTreeMap` iteration order cannot accidentally masquerade as output-index order. For example:

```text
source index 0 -> call_id = call_z -> tool name lookup_a
source index 1 -> call_id = call_a -> tool name lookup_b
```

Feed the calls in interleaved chunks, conceptually:

```text
frame 1: start call index 0 + start call index 1
frame 2: arguments delta for index 1
frame 3: arguments delta for index 0
frame 4: more arguments for index 1
frame 5: more arguments for index 0
frame 6: finish_reason = tool_calls
frame 7: [DONE]
```

Use argument fragments that require actual accumulation, not one complete JSON object in a single frame.

The test must prove:

- both calls remain distinct;
- each `call_id` is preserved exactly;
- each generated Responses item ID is non-empty and differs from its `call_id`;
- each call has a distinct `output_index`;
- source-index association survives interleaving;
- argument fragments accumulate into the correct complete argument string for each call;
- each completed call produces exactly one authoritative `response.output_item.done` item;
- every completed function item contains the correct `call_id`, name, complete arguments, and `status: "completed"`;
- both tool items are closed before the terminal `response.completed` event;
- only one successful terminal event is emitted.

Do **not** require completed-item SSE event order to match lexical call-ID order or output-index order unless current Codex explicitly requires that ordering. The correctness invariant is identity + attached `output_index` + completion before terminal, not incidental container iteration order.

## 1.3 Verify the continuation pairing

After reconstructing the two completed calls, add or extend a request-admission fixture representing the next Codex turn with two corresponding `function_call_output` items.

The fixture should prove that:

- the two outputs remain paired to their original `call_id`s;
- reverse ordering of the two output items does not collapse or swap identity;
- the request is accepted under the stateless Responses contract;
- native Responses preservation retains the exact ordered input items when routed to a Responses upstream;
- translated request construction preserves both tool-result identities when routed to a function-capable non-Responses surface.

This does not need to model a whole conversation engine. It is a protocol identity test.

## 1.4 Optional mixed function/freeform parallel case

Only if it is small and reuses the same fixture machinery, add a second case with one ordinary function tool and one `CanonicalToolKind::Freeform` tool active at the same time.

Useful assertions:

- the ordinary call closes as `function_call`;
- the freeform call closes as `custom_tool_call`;
- the freeform wrapper is unwrapped only for the tool declared freeform in that request;
- the function tool's arguments are not accidentally treated as the freeform wrapper merely because of shape/name collisions.

Do not make this optional case block closure if it materially complicates the fixture. The required gap is two active calls, not exhaustive combinatorics.

---

# Workstream 2 — Upgrade the live Codex smoke from text-only to text + real tool loop

Primary file:

- `scripts/smoke_codex_compat.sh`

Documentation likely touched:

- `docs/agent-configuration.md`;
- `README.md` only if the top-level qualification wording currently overstates or understates the supported path.

The current smoke script is well-scoped: it is opt-in, requires credentials through environment variables, never prints the key, uses HTTP/SSE Responses, disables WebSockets, uses an explicit model, and exits `77` when credentials are not supplied.

Preserve those properties.

## 2.1 Keep the current text smoke

Retain a simple deterministic text phase equivalent to:

```text
Reply with exactly: eggpool-codex-smoke-ok
```

This proves basic custom-provider configuration and terminal Responses handling.

Do not rely on the tool phase alone because a tool-path failure is harder to classify if basic connectivity has not already been proven.

## 2.2 Add a tool smoke that the model cannot answer without using a client tool

A prompt that merely says "run `pwd`" or "run `printf X`" is weak evidence because a model could potentially produce the expected text without actually invoking a tool.

Instead, have the script create a temporary read-only test artifact containing a random/non-prompted marker, then require Codex to retrieve it using its normal local shell tool.

Conceptual procedure:

```bash
workdir="$(mktemp -d ...)"
marker="eggpool-tool-$(random value)"
printf '%s\n' "$marker" > "$workdir/tool-smoke-marker.txt"

cd "$workdir"
# Invoke codex with the same Eggpool provider configuration.
# Prompt: use the shell tool to read ./tool-smoke-marker.txt and reply exactly
# with its contents.
```

Important properties:

- the marker value must **not** appear in the prompt;
- Codex receives only the file path/instruction;
- `--sandbox read-only` remains enabled;
- `approval_policy="never"` remains enabled;
- the command does not need network/file mutation beyond reading the fixture;
- success is determined by matching the generated marker in final Codex output;
- temporary files are removed with the existing trap/cleanup discipline;
- stdout/stderr handling must not print credentials or full request bodies.

This gives meaningful proof of the complete loop:

```text
Codex
  -> /v1/responses request through Eggpool
  -> model emits tool call
  -> Eggpool streams completed tool item
  -> Codex executes local read-only shell tool
  -> Codex submits function/tool output in the next stateless Responses request
  -> model produces final response
  -> Eggpool emits response.completed
```

That is the external qualification Plan 195 was missing.

## 2.3 Keep qualification opt-in and explicit

Continue requiring at least:

```text
EGGPOOL_CODEX_API_KEY
EGGPOOL_CODEX_MODEL
```

Continue allowing:

```text
CODEX_BIN
EGGPOOL_CODEX_BASE_URL
```

If useful, allow distinct model variables for a translated-provider qualification, but do not complicate the default smoke interface unless there is a real need.

The script should continue returning exit code `77` when the required live environment is absent. A skipped live smoke is not a failure for ordinary CI, but it also does **not** count as closure evidence.

## 2.4 Record the exact Codex version used for live qualification

At smoke start, obtain a bounded version string from the selected Codex binary, for example `codex --version`, and print only that non-sensitive version metadata.

Do not hard-code `0.154.0` as forever supported. The existing audited source commit/version is provenance, not a permanent compatibility pin.

If the installed Codex CLI has materially changed its custom-provider syntax or tool protocol, re-audit only the changed boundary and update deterministic fixtures accordingly. Do not preserve obsolete syntax just because it appeared in Plan 195.

## 2.5 Native versus translated live qualification

Required closure evidence:

- one live text run through unmodified current Codex -> Eggpool;
- one live client-executed tool loop through unmodified current Codex -> Eggpool.

If the selected `EGGPOOL_CODEX_MODEL` routes to a native Responses upstream, that is sufficient to prove the actual Codex/Eggpool HTTP/SSE integration.

If credentials/configuration for at least one stable non-Responses tool-capable provider are already available, additionally run the same tool smoke pinned to that route to prove the translated encoder in a real provider loop. Record the surface/provider family, but never record secrets.

Do not make external translated-provider availability a prerequisite when deterministic cross-surface fixtures already cover it and no credentials are available. Conversely, do not claim that a translated provider was live-qualified if only the native Responses path was exercised.

---

# Workstream 3 — Reconcile plan and documentation status with what actually landed

Primary files:

- `plans/193-responses-native-request-preservation-and-replay-safe-admission.md`;
- `plans/194-responses-native-stream-forwarding-and-stateful-codex-lifecycle-synthesis.md`;
- `plans/195-codex-cross-provider-tool-compatibility-conformance-harness-and-model-discovery-closure.md`;
- this Plan 196 at completion;
- `docs/agent-configuration.md` as necessary.

## 3.1 Mark Plans 193 and 194 implemented with actual evidence

Plans 193 and 194 still report `READY FOR IMPLEMENTATION`, although their work is present on `main`.

Update their headers to `IMPLEMENTED` and append concise completion evidence rather than rewriting the original plan body.

Plan 193 evidence should reference at least:

```text
0285cffd1179623cfcffd8a3b22018f609cc0964
Preserve native Responses requests across aliases
```

and summarize:

- native preservation envelope;
- alias rewrite changes only `model`;
- unknown/native items can survive same-surface routing;
- cross-surface blockers/notices are explicit;
- stateless Responses policy remains enforced.

Plan 194 evidence should reference at least:

```text
b3735c00f5ee24486a9d1b139d130a2183e558d7
Preserve native Responses streams and synthesize lifecycles
```

and summarize:

- native-observed Responses SSE forwarding;
- stateful translated Responses encoder;
- output-item completion;
- reasoning indexes/summary metadata;
- strict terminal/EOF behavior;
- bounded retained state.

## 3.2 Amend Plan 195 qualification evidence, not its architecture

Plan 195 is already `IMPLEMENTED` and accurately records that the original live smoke skipped because credentials were unavailable.

After the live text + tool smoke succeeds, append closure evidence containing:

- Eggpool commit used;
- Codex CLI version used;
- selected model/alias name if it is non-sensitive;
- whether the tested upstream path was native Responses or translated, expressed as a wire surface/provider family rather than credential/account identity;
- exact smoke command/environment variable names without secret values;
- result of text phase;
- result of tool-loop phase;
- whether optional translated-provider live qualification ran;
- confirmation that `/v1/models` remains unchanged and automatic rich Codex discovery remains deferred.

If credentials are still unavailable, do **not** fabricate this evidence and do not mark Plan 196 complete. Leave a concise pending-qualification note.

## 3.3 Avoid overclaiming support

Documentation should distinguish:

```text
Deterministically tested protocol compatibility
```

from:

```text
Live-qualified with Codex <version> on <date>
```

Do not convert one live smoke into an unlimited claim that all future Codex versions or all Responses server tools are supported.

Keep the documented integration contract:

```toml
model_provider = "eggpool"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://127.0.0.1:11300/v1"
env_key = "EGGPOOL_API_KEY"
wire_api = "responses"
supports_websockets = false
```

with explicit model/alias selection. Preserve the actual current server port/config conventions rather than copying stale examples.

---

# Workstream 4 — Qualify the one observed CI timeout failure without overengineering

Primary test file:

- `rust/tests/operations_o002.rs`

Production files should change only if the test demonstrates a real control-plane defect.

## 4.1 Reproduce in a bounded way

The first CI attempt for baseline `9729a4f...` failed:

```text
operations_o002::client_timeout_is_bounded_and_handler_survives_disconnect
assertion failed: left == right
left: 0
right: 1
```

An immediate rerun of the same SHA passed the full CI matrix.

Run the focused test repeatedly in the same single-threaded mode used by CI, with a bounded count such as 10–20 iterations. Also run the full `operations_o002` target several times if the focused test never reproduces in isolation, because scheduling interaction may matter.

Do not add an expensive permanent stress loop to normal CI.

## 4.2 If it does not reproduce

If the test remains green across the bounded repetitions:

- record the observed one-off CI failure in Plan 196 completion evidence;
- do not alter production code;
- do not increase timeouts pre-emptively;
- leave the test unchanged unless inspection finds an obvious nondeterministic assertion race.

One historical transient failure followed by a clean same-SHA rerun is not sufficient justification for a control-plane redesign.

## 4.3 If it reproduces

If the test reproduces, determine whether the failure is:

1. production behavior racing incorrectly; or
2. the test checking an asynchronous effect before the handler has reached an observable synchronization point.

Prefer deterministic test synchronization over sleep inflation. Examples include:

- wait for a bounded explicit channel/notification already associated with handler completion;
- wait for a socket/control state transition rather than a fixed duration;
- use a bounded polling helper with a very small deadline only when no explicit signal exists.

Do not simply change `sleep(50ms)` to `sleep(500ms)` or globally enlarge timeout constants unless the production contract itself is proven wrong.

If production code is defective, add a focused regression proving:

- the client timeout remains bounded;
- disconnecting a client does not cancel retained handler work;
- no handler/task/socket state is leaked;
- later control requests remain recoverable.

Keep this fix independent from the Codex wire code.

---

# Workstream 5 — Run the closure verification matrix

The implementation handoff should run the smallest matrix that proves the touched boundaries, then the repository gates.

## 5.1 Focused Rust tests

At minimum:

```bash
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
```

Add `wire_runtime`/request tests if implementation changes those files, but do not run unrelated focused targets merely for volume.

## 5.2 Static Rust gates

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

## 5.3 Full Rust suite

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

If the repository's current CI also runs a no-default-feature test matrix at implementation time, run the same matrix locally before closure. Follow current `main`, not stale plan text.

## 5.4 Tooling/documentation gates

Run the current repository equivalents of:

```bash
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
bash -n scripts/smoke_codex_compat.sh
```

Run any repository release-doc/runtime-boundary checks that are current on `main` if documentation or architecture guardrails are touched.

## 5.5 Live qualification

With real credentials and a running Eggpool instance:

```bash
EGGPOOL_CODEX_API_KEY=... \
EGGPOOL_CODEX_MODEL=... \
scripts/smoke_codex_compat.sh
```

The script must prove both phases and exit success only when both pass.

Never add real provider secrets to the repository, fixtures, CI logs, completion notes, or shell history examples.

---

# Expected implementation shape

A successful closure should be mostly tests, script qualification, and status/documentation edits.

Expected changed files:

```text
rust/tests/codex_responses_compat.rs
scripts/smoke_codex_compat.sh
plans/193-responses-native-request-preservation-and-replay-safe-admission.md
plans/194-responses-native-stream-forwarding-and-stateful-codex-lifecycle-synthesis.md
plans/195-codex-cross-provider-tool-compatibility-conformance-harness-and-model-discovery-closure.md
plans/196-codex-responses-compatibility-closure-and-live-qualification.md
docs/agent-configuration.md          # only if qualification wording needs updating
```

Possible only if evidence requires them:

```text
rust/tests/wire_stream.rs
rust/tests/operations_o002.rs
rust/src/wire/stream.rs              # only for an actual parallel-call defect
rust/src/operations/...              # only for a reproduced control-plane defect
```

A large production diff is a warning sign. Stop and re-check whether the closure tests are exposing a genuine general bug or whether the plan is drifting into a new feature project.

No new Cargo or Python dependency should be necessary.

---

# Acceptance criteria

Plan 196 is complete only when all required items below are true.

1. `codex_responses_compat` contains a real two-active-call/interleaved tool fixture rather than only a parallel-call test name.
2. The parallel fixture proves independent `call_id`, item ID, `output_index`, accumulated arguments, exactly-once `response.output_item.done`, and completion before `response.completed` for both calls.
3. The next-turn fixture proves two tool outputs remain paired to the correct `call_id`s even when output-item order differs.
4. Premature Responses EOF remains a failure and existing terminal semantics are unchanged.
5. Native Responses request and stream preservation tests remain green.
6. Function/freeform custom-tool wrapper tests remain green.
7. `scripts/smoke_codex_compat.sh` performs both a basic text phase and a real client-tool loop whose result depends on reading a random temporary file marker not present in the prompt.
8. The smoke keeps HTTP/SSE Responses, `supports_websockets=false`, read-only sandboxing, no approvals, explicit model selection, secret-safe environment handling, and exit `77` when live qualification is unavailable.
9. A real unmodified current Codex CLI successfully completes both live smoke phases through Eggpool before Plan 196 is marked implemented.
10. The exact Codex CLI version and Eggpool commit used for successful live qualification are recorded without secrets.
11. If only a native Responses upstream is live-qualified, documentation says so; translated-provider live qualification is never implied without evidence.
12. Plans 193 and 194 are reconciled from `READY FOR IMPLEMENTATION` to `IMPLEMENTED` with the actual implementation commits/evidence.
13. Plan 195 receives an appended live-qualification note rather than having its original skipped-smoke evidence erased.
14. `/v1/models` remains the standard Eggpool/OpenAI-compatible model list; no Codex-private replacement is introduced.
15. Automatic Codex model discovery remains explicitly deferred/optional.
16. The observed `operations_o002` CI failure is subjected to a bounded reproduction attempt. If it does not reproduce, no speculative production change is made. If it reproduces, the smallest deterministic test or real control-plane defect is fixed and regression-covered.
17. Formatting, Clippy, no-default-feature checks, focused tests, full Rust suite, and tooling tests are green.
18. No new runtime dependency on Codex/OpenCodex and no new production dependency are introduced.
19. Plan 196 completion evidence records exactly what was tested and does not overclaim future Codex or all-provider compatibility.

---

# Suggested implementation order for handoff

This ordering minimizes wasted investigation and keeps failures attributable.

## Step 1 — Verify baseline and provenance

- confirm `main`/working baseline;
- run the current focused `codex_responses_compat` test once;
- record current `codex --version` if installed;
- do not edit production code yet.

## Step 2 — Fix the deterministic coverage gap first

- rename the misleading existing test if appropriate;
- add the two-call interleaving fixture;
- add continuation/output pairing assertions;
- run only `codex_responses_compat` until green.

If this exposes a wire defect, make the smallest general `stream.rs` correction and add a direct lower-level regression only where useful.

## Step 3 — Upgrade the smoke script

- keep the existing text phase;
- add temporary random marker creation;
- run a second Codex invocation (or a clearly isolated second phase) requiring a shell read of the marker file;
- preserve trap cleanup and secret handling;
- run `bash -n` and any shell/tooling checks.

## Step 4 — Perform live qualification

- start/use the normal Eggpool service configuration;
- select an explicit known working model/alias;
- run the script with real credentials;
- if possible, repeat on a translated non-Responses provider, but record that as extra evidence rather than silently making it mandatory;
- if credentials are unavailable, stop short of marking this plan implemented.

## Step 5 — Qualify the CI flake

- repeat the focused `operations_o002` test a bounded number of times;
- if clean, document and move on;
- if reproducible, stabilize synchronization or fix the actual defect without touching Codex/wire code.

## Step 6 — Reconcile plan/docs metadata

- mark 193/194 implemented with exact commits;
- append Plan 195 live evidence;
- update current Codex qualification wording in docs only as needed;
- keep model discovery deferred and `/v1/models` unchanged.

## Step 7 — Run full gates once

Only after focused tests and live qualification are clean, run the full Rust/tooling matrix. Do not repeatedly pay the whole CI cost while iterating on one fixture.

---

# Completion evidence template

When implementation is finished, append a short section to this plan using concrete values:

```text
## Completion evidence

- Eggpool commit: <sha>
- Codex CLI: <version>
- Audited/current Codex source provenance: <sha if rechecked>
- Deterministic parallel-call fixture: PASS
  - two active calls: PASS
  - interleaved argument accumulation: PASS
  - distinct item IDs/call IDs: PASS
  - exactly-once output_item.done: PASS
  - both items closed before response.completed: PASS
- Continuation/output pairing: PASS
- Live text smoke: PASS
- Live tool-loop smoke: PASS
  - upstream path: <native Responses | translated surface/provider family>
- Optional translated-provider live smoke: <PASS | NOT RUN>
- operations_o002 repeated qualification: <N/N PASS | reproduced and fixed in SHA>
- focused Rust tests: <results>
- full Rust workspace: <results>
- no-default-feature checks: <results>
- tooling checks: <results>
- `/v1/models`: unchanged standard schema
- Codex automatic rich model discovery: deferred
- new production dependencies: none
```

Do not include API keys, provider account identifiers, request/response bodies containing private prompts, or environment dumps.

---

# Closure decision

If this plan passes without exposing a production bug, the current Codex/Responses work should be considered **qualified and closed**. Future changes should be driven by a concrete Codex protocol change, a provider compatibility regression, or a separately scoped model-discovery feature request—not by further speculative redesign.

The next unrelated maintenance item remains the existing Eggress 1.0.7 facade/fallback retirement work in Plan 191; do not mix that dependency migration into this Codex closure commit.