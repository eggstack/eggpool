# Plan 156 — Wire Classifier and MiniMax Thinking Final Closure

Date: 2026-09-03
Status: completed (2026-09-03)
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Corrects: residual edge cases after Plans 154–155
Depends on: `bebd5c074c4e7c4dad09bcb196cf049c01a6cc8a`
Priority: P0 narrow correctness / final release closure
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Close the final three concrete gaps found after the successful Plans 154–155 implementation without reopening the dynamic wire architecture.

Current `main` has already achieved the important goals:

- production alternate-surface dispatch is governed by the existing provider/model single-flight mechanism;
- cancellation ownership and follower shielding are corrected;
- weak `model ... is not supported` evidence can trigger safe same-account wire migration when the provider is known to serve the model;
- strong model absence remains model-scoped rather than causing surface roulette;
- Muse Spark 1.2 is correctly modeled as an OpenAI Responses upstream;
- deterministic cross-surface request/streaming adaptation exists in both directions;
- credentialed OpenCode Go acceptance currently passes 5/5 live tests;
- explicit invalid credentials no longer poison sibling accounts, and ambiguous 401 remains non-poisoning.

Three narrow closure items remain:

1. explicit wire signals can still be masked by the classifier's generic `"unsupported"` error-class fallback;
2. `model is not available` is currently treated as strong global model absence even though providers can qualify that wording with `on this endpoint/surface`;
3. the original MiniMax-M3 thinking failure has not yet been exercised in the credentialed live suite with a thinking-enabled request through the real Chat-client -> Anthropic-Messages-upstream path.

This plan must fix only those items, preserve the existing successful live matrix, record final evidence, and stop this workstream unless the live MiniMax request reveals a genuinely new provider-contract defect.

---

# Scope discipline

This is a closure pass, not another architecture phase.

Do **not** add:

- a new wire surface;
- a new resolver or retry path;
- provider-specific retry logic;
- DB state or migrations;
- background probing;
- distributed coordination;
- a new dependency;
- live-provider CI;
- a larger provider/model live matrix;
- broad fuzzy/NLP error interpretation;
- a new thinking abstraction;
- another compatibility retry loop;
- a full provider model table hard-coded in Python.

Prefer changing a few lines in the canonical classifier/signal extractor plus focused regression coverage and one additional live MiniMax acceptance case.

---

# Governing invariants

1. A typed, context-qualified wire signal is stronger evidence than a generic error-class substring such as `unsupported`.
2. Generic `unsupported` error classes must not independently authorize wire migration.
3. Safe wire migration still requires a declared alternate, pre-handoff `response_status` evidence, and the existing canonical failure-effects path.
4. Strong model absence such as `model not found`, `unknown model`, `does not exist`, or `no such model` must remain model-scoped and must not enumerate surfaces.
5. Ambiguous availability wording must use provider-model knowledge and endpoint context rather than being treated as globally authoritative by wording alone.
6. Bare/ambiguous 401 remains non-poisoning.
7. Explicit invalid/expired/revoked credentials remain account-specific auth failures.
8. 429, 5xx, transport failure, timeout, cancellation, and midstream errors never become wire-negotiation evidence.
9. MiniMax-M3 must be sent through its selected Anthropic Messages wire surface; OpenAI-only thinking fields must not leak to `/messages`.
10. A live MiniMax thinking failure must never disable or globally poison an otherwise valid OpenCode Go account.
11. Existing successful live OpenCode Go cases must remain green.
12. No new CI/test apparatus is required.

---

# Phase A — Give explicit wire signals precedence over generic `unsupported` error classes

## Current defect

`src/eggpool/failure/classifier.py` currently handles generic capability/error-class wording before the dedicated alternate-wire signal block.

Conceptually the order is currently:

```text
context/capability signal or generic validation
"capability" in error_class or "unsupported" in error_class
...
explicit WIRE_AUTH_MISMATCH / WIRE_SURFACE_UNSUPPORTED /
WIRE_SCHEMA_MISMATCH / MODEL_UNSUPPORTED_ON_SURFACE
```

That means a correctly extracted and context-qualified signal can still be masked if the provider's structured error class happens to contain `unsupported`.

Example:

```text
status = 401 or 400
error_class = UnsupportedModelError
response_signal = MODEL_UNSUPPORTED_ON_SURFACE
provider_model_presence = known
alternate_wire_available = true
dispatch_phase = response_status
```

The correct result is:

```text
retry_action = alternate_wire_same_account
wire_effect = reject_candidate
account_effect = none
model_effect = none
```

The generic error-class substring must not turn this back into a request-local capability rejection.

## Required change

Reorder or narrow the classifier so already-extracted explicit wire signals are handled before the broad error-class fallback.

Preferred structure:

1. request-local source classes remain first;
2. explicit request-control/context-limit/generic-client-validation signals remain request-local;
3. transport/midstream handling remains unchanged;
4. explicit credential invalidity remains unchanged;
5. explicit safe wire signals are evaluated;
6. only then apply generic `error_class` compatibility fallbacks such as `capability`/`unsupported`;
7. continue with status-code handling.

Do not make `"unsupported" in error_class` itself sufficient to negotiate. The negotiation path must still be authorized by one of the bounded `FailureSignal` values.

## Regression tests

Extend the existing failure-effects/signal tests rather than creating another test module.

Required cases:

### A1 — Explicit model-on-surface signal wins

Input:

```text
status_code = 401
error_class = UnsupportedModelError
response_signal = MODEL_UNSUPPORTED_ON_SURFACE
provider_model_presence = known
alternate_wire_available = true
dispatch_phase = response_status
```

Assert:

- retry is true;
- action is `alternate_wire_same_account`;
- `wire_effect == reject_candidate`;
- account/model effects are none;
- no credential disable/circuit penalty.

### A2 — Explicit wire-schema/surface/auth signals also win

At least one representative structured error class containing `unsupported` with a true `WIRE_SCHEMA_MISMATCH` or `WIRE_SURFACE_UNSUPPORTED` signal must still negotiate.

Do not duplicate every combination.

### A3 — Generic unsupported class alone remains non-negotiating

Input:

```text
error_class = UnsupportedParameterError
response_signal = None
```

Assert that no alternate-wire transition is manufactured solely from the class name.

### A4 — Post-handoff safety remains authoritative

Even with `MODEL_UNSUPPORTED_ON_SURFACE`, `downstream_started = true` must produce no retry/wire effect.

---

# Phase B — Refine `model is not available` without weakening true absence

## Current defect

`src/eggpool/failure/signal_extract.py` includes a broad strong-absence pattern equivalent to:

```text
model is not available
```

The extractor evaluates strong absence before weak surface-local model rejection.

This makes a response such as:

```text
Model X is not available on this endpoint
```

look globally authoritative even when:

- the selected provider catalog/config says Model X exists;
- another compatible wire surface is declared;
- the failure is pre-handoff.

That is inconsistent with the negotiation-aware behavior introduced by Plan 155.

## Required change

Keep the strong model-absence bucket limited to wording that is genuinely authoritative without additional context, including forms equivalent to:

- `model not found`;
- `unknown model`;
- `model does not exist`;
- `no such model`;
- `model_id not found`.

Treat `model is not available` as ambiguous/weak availability evidence rather than unconditional strong absence.

Recommended implementation:

- move `model is not available` into a bounded weak/availability pattern group alongside unsupported-model wording, or introduce one small dedicated ambiguous-availability group;
- when `provider_model_presence == "known"`, an alternate wire exists, and `dispatch_phase == "response_status"`, map the ambiguous wording to `MODEL_UNSUPPORTED_ON_SURFACE`;
- otherwise preserve the conservative historical fallback to `MODEL_ABSENT`.

This retains model-unavailable behavior when EggPool has no contradictory provider knowledge while allowing a known model to escape a stale/wrong endpoint.

Do not attempt semantic interpretation of arbitrary prose beyond these bounded patterns.

## Regression tests

### B1 — Known model + endpoint-qualified unavailable wording migrates

Example body:

```json
{"error":{"type":"ModelError","message":"Model example is not available on this endpoint"}}
```

Context:

```text
provider_model_presence = known
alternate_wire_available = true
dispatch_phase = response_status
```

Assert `MODEL_UNSUPPORTED_ON_SURFACE` and same-account alternate-wire effects.

### B2 — Unknown model + same weak wording remains model absence

With `provider_model_presence = unknown`, the same broad availability wording must not trigger blind surface enumeration.

### B3 — Strong absence remains strong even for a known model

`Model not found` / `does not exist` must remain `MODEL_ABSENT` and must not enumerate surfaces merely because the provider catalog is stale or another endpoint exists.

### B4 — Existing OpenCode-Go-like 401 regression remains green

Keep the Plan 155 `Model unhinted-model is not supported` coordinator migration test unchanged in intent and passing.

---

# Phase C — Add the missing credentialed MiniMax-M3 thinking acceptance

## Why this is required

The original user-visible failure that initiated this work included MiniMax-M3 through OpenCode Go receiving an incompatible thinking-level/control shape and then destabilizing routing.

The current live suite proves:

- MiniMax-M3 reaches `/messages` for ordinary requests;
- MiniMax-M3 streaming reaches Messages terminal semantics;
- Muse and MiMo reasoning shapes are surface-native;
- cross-surface Messages -> Muse Responses works;
- invalid-key isolation works.

It does **not** currently send a thinking-enabled MiniMax-M3 request through the real client-to-wire adaptation path.

The built-in OpenCode Go MiniMax contract currently claims:

```text
protocol = anthropic
mode = effort
accepted efforts = low, medium, high
effort -> budget mapping = 1024 / 4096 / 16384
request fields include thinking/reasoning_effort
```

That claim must now be validated against the actual provider rather than remaining mock-derived or historical metadata.

## Required live path

Extend `tests/live/test_opencode_go_wire_live.py` with one narrowly targeted test using the already-supplied OpenCode Go credential.

Preferred original-bug path:

```text
client endpoint: /v1/chat/completions
model: minimax-m3
client thinking intent: reasoning_effort = low
selected upstream wire: anthropic_messages
actual upstream path: .../messages
```

This specifically proves that EggPool can accept OpenAI-style client reasoning intent while selecting MiniMax's Anthropic Messages wire grammar.

Use a small prompt and low output ceiling consistent with the existing live tests.

## Required outbound assertions

Using the existing sanitized outbound observer, assert:

- upstream surface is `anthropic_messages`;
- actual path ends with `/messages`;
- authentication uses the Messages surface's configured shape;
- an Anthropic-native `thinking` control is present if the provider contract says thinking is supported;
- `reasoning_effort` is **not** leaked as a top-level field to `/messages`;
- OpenAI Responses-style `reasoning` is not leaked to `/messages`;
- no fabricated unrelated control fields appear;
- account health remains usable after the request.

Do not log the raw credential, raw request body, or hidden reasoning output.

## Success criterion

When OpenCode Go MiniMax-M3 still supports selectable thinking, the live request must return a successful model response using at least one currently supported level (`low` preferred for cost).

A 400 generated by EggPool before dispatch or an upstream 4xx caused by an incorrect thinking shape is **not** closure.

## Evidence-driven contract correction branch

If the live provider rejects the current built-in MiniMax thinking contract:

1. confirm the failure is thinking/control-specific and not quota, credential, model absence, or transient provider failure;
2. inspect only bounded/sanitized structural evidence;
3. correct the existing OpenCode Go MiniMax thinking contract or target encoding minimally to match observed provider behavior;
4. do not add provider-specific retry logic;
5. do not make a broad error classifier exception merely to force the test green;
6. rerun the live MiniMax case.

If current OpenCode Go behavior demonstrates that MiniMax-M3 no longer supports client-selectable thinking at all, do not preserve a false built-in capability. Update the provider-scoped capability/contract truthfully so EggPool no longer advertises unsupported thinking, and add the corresponding deterministic/live acceptance that the request is handled correctly without poisoning routing.

That fallback must be based on actual provider evidence, not a convenient assumption.

---

# Phase D — Preserve failure isolation during the MiniMax live case

Regardless of whether the live thinking request succeeds immediately or exposes a contract correction:

- no ambiguous thinking/control 4xx may disable the OpenCode Go credential;
- no model-specific control rejection may poison sibling models/accounts;
- no database reset, restart, or rehash may be required for recovery;
- a valid follow-up ordinary request to MiniMax-M3 or another representative OpenCode Go model must still work.

If the first corrected live request succeeds, a separate destructive failure case is unnecessary; the existing invalid-key and deterministic capability/failure-isolation tests already cover the broader machinery.

Do not intentionally generate extra paid-provider failures merely for coverage.

---

# Phase E — Verification and closure bookkeeping

## Focused deterministic gate

Run only the directly affected suites plus the repository's existing lean gate.

Minimum:

```bash
uv run pytest tests/unit/test_failure_signal_extraction.py -q
uv run pytest tests/unit/test_failure_effects_table.py -q
uv run pytest tests/integration/test_wire_negotiation_e2e.py -q
uv run pytest tests/integration/test_muse_spark_e2e.py -q
```

If MiniMax thinking-contract code changes, also run the existing focused thinking/transcoder/provider-adaptation tests that cover that module. Do not create a new broad test framework.

Then run the documented ordinary lean project gate:

- Ruff format/check;
- Pyright for the normal project targets;
- config validation/smoke tests currently required by the repo.

A full multi-thousand-test repository run is not required solely for this narrow closure unless the implementation unexpectedly touches unrelated shared infrastructure.

## Live gate

Run the existing credentialed suite including the new MiniMax thinking case:

```bash
EGGPOOL_E2E_OPENCODE_GO_API_KEY='...' \
uv run pytest tests/live/test_opencode_go_wire_live.py -m live_opencode_go -v
```

The prior baseline is 5 passing live tests. After adding the MiniMax case, all live tests in this file must pass (or the evidence-driven unsupported-capability branch must be explicitly implemented and validated as described above).

Do not convert the live suite into CI.

## Closure record

Append exact evidence to this plan after implementation:

- implementation SHA(s);
- focused deterministic test counts;
- lean gate result;
- live OpenCode Go test count/result;
- MiniMax-M3 selected client endpoint and upstream surface/path;
- whether MiniMax thinking succeeded under the existing contract or required a contract correction;
- sanitized semantic-field assertion (`thinking` present / OpenAI-only controls absent) when supported;
- immediate follow-up health/routing result;
- any optional Gemini live work explicitly left out.

Do not paste credentials or raw provider bodies into the plan.

---

# Files expected to change

The implementation should normally be limited to a subset of:

```text
src/eggpool/failure/classifier.py
src/eggpool/failure/signal_extract.py
tests/unit/test_failure_effects_table.py
tests/unit/test_failure_signal_extraction.py
tests/live/test_opencode_go_wire_live.py
plans/156-wire-classifier-and-minimax-thinking-final-closure.md
```

Only if the real MiniMax live request disproves the current contract should this expand narrowly into existing thinking-control files such as:

```text
src/eggpool/transcoder/builtin_contracts.py
src/eggpool/request/thinking_adaptation.py
existing focused thinking/provider-adaptation tests
```

Do not refactor `RequestCoordinator`, `WireProfileResolver`, codec architecture, or routing merely for cleanup in this pass unless a concrete MiniMax live failure proves one of those components is actually responsible.

---

# Acceptance criteria

## Classifier precedence

- [x] `MODEL_UNSUPPORTED_ON_SURFACE` cannot be masked by an `Unsupported*` error class.
- [x] Explicit safe wire auth/surface/schema signals retain precedence over generic error-class fallbacks.
- [x] Generic `unsupported` error classes alone do not authorize wire negotiation.
- [x] Post-handoff wire signals still cannot retry.
- [x] Bare/ambiguous 401 remains non-poisoning.
- [x] Explicit credential-invalid evidence still disables only the selected credential/account.

## Model availability wording

- [x] `model not found`, `unknown model`, `does not exist`, `no such model`, and equivalent strong absence remain model-scoped.
- [x] `model is not available` is no longer unconditionally treated as strong global absence before provider/surface context can be considered.
- [x] Known model + alternate declared surface + pre-handoff ambiguous availability can migrate safely.
- [x] Unknown model + ambiguous availability does not blindly enumerate surfaces.
- [x] The existing unhinted-model 401 migration regression remains green.

## MiniMax-M3 thinking

- [x] The credentialed live suite includes a MiniMax-M3 request with thinking/reasoning enabled.
- [x] Preferred acceptance exercises `/v1/chat/completions` client input with `reasoning_effort=low` and proves the upstream request uses `/messages`.
- [x] Upstream semantic fields are Anthropic Messages-native; `reasoning_effort`/Responses `reasoning` do not leak to `/messages`.
- [x] When thinking is currently supported by OpenCode Go MiniMax-M3, at least one supported level succeeds live.
- [x] If the provider has changed and selectable thinking is no longer supported, EggPool's advertised provider capability/contract is corrected rather than forcing a false success.
- [x] A thinking/control rejection never disables or globally poisons the valid account.
- [x] An immediate valid follow-up works without restart, rehash, or DB reset.

## Regression / resource posture

- [x] Existing 5/5 credentialed live cases remain green after adding the new MiniMax case.
- [x] Existing single-flight, stale-profile migration, invalid-key isolation, Muse Responses routing, and cross-surface tests remain green in focused coverage.
- [x] No new dependency is added.
- [x] No DB migration is added.
- [x] No new background task is added.
- [x] No new live-provider CI gate is added.
- [x] No provider-specific retry loop is added.
- [x] No full OpenCode Go model table is hard-coded into dispatch logic.
- [x] The ordinary lean project gate passes.
- [x] Exact closure evidence is recorded in this plan.

---

# Handoff order

1. Reorder/narrow failure classifier precedence and add the small unit regressions.
2. Split ambiguous `model is not available` wording from unequivocal strong model absence and add extraction/effects tests.
3. Re-run the existing unhinted-model migration integration test before touching thinking code.
4. Add the one MiniMax-M3 thinking live case using the existing live fixture and sanitized outbound observer.
5. Run the live case against the real OpenCode Go credential.
6. Only if the real provider disproves the current MiniMax contract, make the smallest evidence-backed correction in the existing thinking contract/adaptation path.
7. Re-run focused deterministic tests and the complete opt-in OpenCode Go live file.
8. Run the repository's existing lean gate.
9. Append exact closure evidence and implementation SHA to this plan.
10. Stop this wire/thinking workstream unless the live MiniMax result exposes a distinct reproducible defect.

---

# Closure evidence

- Implementation commits: `bdbf47c3aa6c6a563c43167a3b028216a27defe1` and
  `9c38e8d784feedc7212342dce8635a0b75ba5f83`.
- Deterministic focused gates: 103 passed across
  `test_failure_signal_extraction.py`, `test_failure_effects_table.py`,
  `test_wire_negotiation_e2e.py`, and `test_muse_spark_e2e.py`; 23 passed
  across `test_builtin_contracts.py`,
  `test_provider_thinking_control_e2e.py`, and
  `test_minimax_opencode_go_isolation.py` after the contract correction.
- Full repository suite: 7,792 passed, 42 skipped, one existing
  `StarletteDeprecationWarning`.
- Lean CI-equivalent gate: `uv sync --frozen --extra ci`; Ruff format passed
  for 697 files, Ruff check passed, Pyright reported 0 errors/warnings, and
  smoke passed with 14 tests.
- Live OpenCode Go gate: six passed in 48.66s with the supplied credential;
  the original five cases and the new MiniMax case are green.
- MiniMax-M3 acceptance added at
  `tests/live/test_opencode_go_wire_live.py::test_opencode_go_minimax_chat_reasoning_reaches_messages`.
  It exercises client `POST /v1/chat/completions` with
  `reasoning_effort=low`, expects the `anthropic_messages` surface and an
  upstream path ending in `/messages`, requires the sanitized `api_key` auth
  scheme and `thinking` field, rejects top-level `reasoning_effort` and
  `reasoning`, and sends an ordinary follow-up while checking account health.
  The initial live attempt exposed a local capability rejection because the
  OpenCode Go MiniMax contract was `effort` while the cross-protocol adapter
  correctly emitted `thinking.budget_tokens`. The contract was minimally
  corrected to `effort_or_budget`; the rerun passed without an upstream
  thinking-shape rejection or account-health effect. Optional Gemini live work
  was not run.
- A small defensive `getattr` correction in
  `RequestCoordinator._alternate_wire_available` was required by the full
  suite's minimal stream-timeout test double; it does not alter production
  wire negotiation behavior.
