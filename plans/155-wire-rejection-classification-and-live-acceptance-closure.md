# Plan 155 — Wire Rejection Classification and Live Acceptance Closure

Date: 2026-09-03
Status: ready after / alongside Plan 154
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Corrects: Plans 151–153 acceptance gaps
Depends on: current Plans 148–153 implementation; Plan 154 should land before final closure
Planning baseline: `dda314a16bf4214af1040e57dbd4931d4b505cb6`
Priority: P0 correctness / release closure
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Close the remaining correctness and verification gaps in the dynamic wire-surface work without expanding its architecture.

Current `main` correctly separates explicit credential invalidity from ambiguous 401 responses and can relearn a stale profile after a deterministic surface rejection. However, two important gaps remain:

1. model-absence detection is still too broad to reliably distinguish a model that is truly absent from a provider from a model that is merely unsupported on the currently selected endpoint/surface;
2. the live OpenCode Go suite exists but has not been run with credentials, primarily exercises matching client/upstream surfaces, and contains a nondeterministically skippable invalid-key isolation case.

There is also a stale mocked Muse regression test that still models Muse Spark 1.2 through Chat Completions even though the bundled registry and current provider documentation classify it as Responses.

This pass should make the failure classification negotiation-aware, add the missing cross-surface acceptance cases, make credential-isolation verification deterministic, and produce actual live evidence when a test key is supplied.

Do not reopen the wire architecture or add more default surfaces in this pass.

---

# Governing invariants

1. A model that exists on a provider but is rejected by one declared endpoint may be retried on another declared compatible surface when the failure is deterministic and pre-handoff.
2. A model that is truly absent must retain model-unavailable/quarantine behavior rather than causing blind surface roulette.
3. Generic client validation must not become surface negotiation.
4. Explicit invalid/expired/revoked credentials remain account-specific terminal auth evidence.
5. Bare/ambiguous 401 remains non-poisoning.
6. Surface negotiation must never be triggered by 429, 5xx, timeout/reset, or midstream failure.
7. Existing provider/model hints remain low-authority accelerators, not correctness requirements.
8. An unhinted model must still be able to discover a valid declared surface when the upstream gives safe structural evidence.
9. Tests must reflect the provider contract they claim to represent.
10. Live tests remain manual/opt-in and must not become CI requirements.

---

# Phase A — Distinguish provider model absence from endpoint-specific model rejection

## Current risk

`src/eggpool/failure/signal_extract.py` currently gives model-absence patterns highest precedence and includes a broad `is not supported` match. This was originally necessary because OpenCode Go can use HTTP 401 for unsupported models and EggPool needed to avoid treating that as credential invalidity.

With dynamic wire negotiation, the same phrase can also mean:

```text
this model is not supported on this endpoint/surface
```

For a provider that declares multiple surfaces, treating that response immediately as global `MODEL_ABSENT` can skip a valid alternate surface.

This is especially important for model IDs that are present in provider discovery/catalog data but are not listed in `_wire_profiles.toml` hints.

## Required change

Add bounded context to the failure observation/classification path so the classifier can distinguish at least:

```text
model known to provider + alternate wire available + endpoint-local rejection
model not known to provider / authoritative absence
```

Prefer a small boolean/enum carried in `FailureObservation`, for example:

```text
provider_model_presence = known | unknown | absent_authoritative
```

or an equivalently narrow representation.

Do not pass the whole catalog object into `signal_extract.py`.

The coordinator already knows the selected provider/model and can query existing catalog/provider metadata before constructing the observation. Reuse that fact.

### Recommended signal behavior

Keep strong explicit model absence patterns such as:

- `model not found`;
- `unknown model`;
- `model does not exist`;
- `no such model`;
- explicit structured provider catalog withdrawal.

Treat generic forms like:

- `model X is not supported`;
- `unsupported model`;

more carefully when:

- the model is known to the same provider catalog/config;
- another compatible wire surface is configured;
- the response arrived at `response_status` before downstream handoff.

In that case, classify as a surface-scoped/model-on-surface rejection that can authorize `alternate_wire_same_account` rather than global model absence.

Possible implementation choices:

- introduce a new bounded `FailureSignal.MODEL_UNSUPPORTED_ON_SURFACE`; or
- leave extraction signal-neutral and let `classify_failure_effects()` reinterpret `MODEL_ABSENT` when provider-model presence is known and evidence is the weak `unsupported` class.

Prefer the option with the least stringly special casing.

Do not make every `MODEL_ABSENT` negotiable.

---

# Phase B — Preserve authoritative model absence

The corrective logic must not weaken true model-withdrawal handling.

Required cases:

### Authoritative provider/catalog absence

If provider discovery/catalog authoritatively says the model is gone:

- retain terminal withdrawal or existing authoritative model effect;
- do not try every surface;
- do not change account credential health.

### Runtime strong absence

For a strong runtime `model not found` / `does not exist` response with no contradictory provider knowledge:

- retain current model-unavailable/quarantine behavior;
- existing account/provider failover may continue according to normal routing policy;
- do not reinterpret as endpoint negotiation solely because another surface exists.

### Weak endpoint-local rejection

For `is not supported`/equivalent where the provider catalog says the model exists:

- permit same-account alternate-wire retry if pre-handoff and another compatible surface exists;
- reject/suppress only the selected wire candidate;
- keep the account healthy;
- learn the successful alternate if one succeeds.

This makes provider hints optional for correctness rather than mandatory.

---

# Phase C — Add an unhinted-model migration regression

Extend `tests/integration/test_wire_negotiation_e2e.py` with a model that has no bundled or operator wire hint.

Synthetic provider:

```text
surfaces:
  openai_responses       preferred first by provider priority
  openai_chat_completions

model:
  unhinted-model
  known to provider catalog/static model list
```

Fake upstream behavior:

```text
POST /responses
  -> 401 or provider-realistic status
  -> body: "Model unhinted-model is not supported"

POST /chat/completions
  -> 200
```

Acceptance:

1. first request starts with Responses due only to provider ordering;
2. the weak unsupported-model response is not classified as credential failure;
3. because the model is known to the provider, it is not treated as global model absence;
4. the same account retries Chat within the shared request submission budget;
5. Chat succeeds;
6. account remains healthy;
7. Chat becomes learned preference;
8. second request goes directly to Chat;
9. no restart, rehash, DB reset, or added hint is required.

Add the inverse control test:

- model is not known to provider;
- upstream returns strong `model not found`;
- no surface enumeration occurs solely because multiple surfaces are configured.

---

# Phase D — Correct stale Muse deterministic fixtures

`tests/integration/test_muse_spark_e2e.py` is now correctly named as mocked integration rather than live, but it still represents `muse-spark-1.2-contributor` through `/chat/completions`.

That fixture should not encode a provider contract that conflicts with the bundled wire registry and current live acceptance definition.

Choose one of these narrow corrections:

### Preferred

Move the Muse happy-path fixture to declared OpenAI Responses and assert `/responses`.

Keep the client-facing endpoint used by the regression meaningful. For example, an Anthropic `/v1/messages` client may still target Muse, but the upstream observation must prove Responses after transcoding/canonical encoding.

### Alternative

If the test only needs generic per-model 5xx isolation and changing it to Responses creates unrelated complexity, rename the upstream model to a synthetic Chat model and remove Muse-specific claims from that test.

Do not leave a test named for Muse that teaches future maintainers that Muse is a Chat model.

---

# Phase E — Add deterministic cross-surface client acceptance

The current live matrix primarily sends each representative model through the client endpoint matching its upstream surface. That proves native path behavior but does not prove the core proxy/transcoding use case.

Add deterministic fake-upstream integration coverage for at least these two directions:

## E1. Anthropic Messages client -> OpenAI Responses upstream

Representative model may be synthetic or Muse-shaped.

Client:

```text
POST /v1/messages
```

Selected model/provider contract:

```text
upstream family/surface = openai_responses
```

Acceptance:

- request is canonicalized once;
- upstream request goes to `/responses`;
- request reasoning controls use Responses-native structure when applicable;
- upstream Responses response is adapted back to valid Anthropic Messages grammar;
- streaming case terminates with valid client Messages terminal semantics;
- account health remains unchanged.

## E2. OpenAI Chat client -> Anthropic Messages upstream

Representative model may be synthetic or MiniMax-M3-shaped.

Client:

```text
POST /v1/chat/completions
```

Selected model/provider contract:

```text
upstream surface = anthropic_messages
```

Acceptance:

- upstream request goes to `/messages` with surface-specific auth;
- response is adapted back to Chat Completions grammar;
- streaming case produces valid Chat completion chunks and terminal evidence;
- reasoning controls are not guessed across incompatible semantics;
- account health remains unchanged.

These tests should exercise the real coordinator and wire codecs with `respx`/fake upstream, not direct codec-only calls.

---

# Phase F — Strengthen opt-in OpenCode Go live acceptance

File: `tests/live/test_opencode_go_wire_live.py`

The suite remains opt-in and credentialed.

## F1. Preserve native current-surface matrix

Keep representative coverage for the provider's current documented families, re-checking the official endpoint table immediately before live execution.

At the 2026-09-03 planning point, the intended representatives are still conceptually:

- Muse Spark contributor -> Responses;
- GPT-5.6 Luna -> Responses;
- MiniMax M3 -> Messages;
- MiMo/GLM representative -> Chat.

Model IDs must be re-verified at execution time because OpenCode Go changes catalog contents.

## F2. Add at least one real cross-surface client case

With the same real upstream, add at least one request where the public EggPool endpoint differs from the upstream surface.

Preferred cases:

- `/v1/messages` client -> Muse Responses upstream; and/or
- `/v1/chat/completions` client -> MiniMax Messages upstream.

Assertions must use the sanitized outbound observer to prove the actual upstream path and surface.

A successful client response without path evidence is insufficient.

## F3. Make invalid-key isolation deterministic

The current live invalid-key test may `pytest.skip()` when routing selects the valid account before the bad account. Remove that nondeterminism.

Use existing test scaffolding/configuration to force the first attempt onto the deliberately invalid account without adding production-only hooks.

Acceptable methods include:

- deterministic account order under a test-specific routing configuration;
- test fixture score/priority state that makes the invalid account first;
- another existing routing control that is already supported by production code.

Do not add a special `force_account` production API solely for testing.

Acceptance:

1. outbound observations prove the bad account was attempted first;
2. provider returns explicit credential-invalid evidence;
3. only bad account becomes `authentication_failed`;
4. valid account succeeds in the same request if budget permits;
5. immediate follow-up succeeds on valid account;
6. sibling model remains usable;
7. no restart/rehash/database reset is needed.

If the provider returns only an ambiguous 401 for the deliberately invalid key, record that actual behavior rather than weakening EggPool's classifier to force the test expectation. The live result should then prove that ambiguous 401 does not poison either account.

## F4. Add live reasoning assertion for Muse without fabricated Anthropic budget

When the current provider still supports a Muse reasoning effort:

- send a supported effort through EggPool;
- outbound observer must show Responses-native `reasoning` top-level structure;
- outbound semantic fields must not contain `thinking` solely as an artifact of effort-to-budget conversion;
- account must remain healthy on either success or a bounded provider capability rejection.

Do not assert undocumented hidden chain-of-thought content.

---

# Phase G — Closure evidence and plan status

Do not mark this line complete from deterministic tests alone.

After implementation:

1. run focused deterministic tests;
2. run the ordinary repository gate;
3. run credentialed OpenCode Go acceptance when a test key is supplied;
4. append a concise closure record to this plan or update Plan 153 with a corrective-follow-up reference and evidence;
5. record skipped optional Gemini live verification honestly if no Gemini credential/template is available.

The closure record must state exactly which live tests ran versus skipped.

Do not write `live verification passed` when the marker merely skipped.

---

# Focused deterministic tests

At minimum:

```bash
uv run pytest tests/unit/test_failure_signal_extraction.py -q
uv run pytest tests/unit/test_failure_effects_table.py -q
uv run pytest tests/integration/test_wire_negotiation_e2e.py -q
uv run pytest tests/integration/test_muse_spark_e2e.py -q
uv run pytest tests/unit/test_wire_codecs.py -q
```

Also run the Plan 154 focused negotiation concurrency tests after that plan lands.

Then run the repository's existing lean project gate.

No new CI workflow is required.

---

# Live command

When a test-only OpenCode Go key is available:

```bash
export EGGPOOL_E2E_OPENCODE_GO_API_KEY='...'
uv run pytest tests/live/test_opencode_go_wire_live.py -m live_opencode_go -v
```

Never commit the key, `.env` data, raw auth headers, or raw provider error bodies.

If an optional second valid account is useful for a specific assertion, use the existing reserved test environment variable rather than changing normal config semantics.

---

# Explicit non-goals

Do not add:

- more built-in wire surface families;
- automatic internet/document scraping at runtime;
- per-request provider-doc lookup;
- a provider-specific OpenCode Go dispatcher;
- hard-coded Python tables for every OpenCode Go model;
- persistent wire learning;
- model-output retry after ambiguous timeout/5xx;
- broad fuzzy NLP classification of provider errors;
- live provider CI;
- a load/concurrency benchmark against paid upstreams;
- stateful Responses/Interactions failover.

The packaged TOML hint registry remains an optimization. Correctness must come from declared candidate surfaces plus bounded runtime evidence.

---

# Acceptance criteria

## Classification

- [ ] A bare/ambiguous 401 remains non-poisoning.
- [ ] Explicit invalid/expired/revoked credential evidence still disables only the selected account.
- [ ] A weak `model ... is not supported` response for a model known to the provider can authorize alternate-surface negotiation when safe.
- [ ] Strong/authoritative true model absence does not trigger blind surface enumeration.
- [ ] 429, 5xx, timeout/reset and midstream failure never become wire negotiation.
- [ ] An unhinted known model can relearn a valid alternate surface without restart or config edits.

## Deterministic proxy behavior

- [ ] Anthropic Messages client -> Responses upstream succeeds through the real coordinator path.
- [ ] Chat client -> Anthropic Messages upstream succeeds through the real coordinator path.
- [ ] Cross-surface streaming emits valid client grammar and terminates correctly.
- [ ] Surface-native reasoning controls are emitted only after target selection.
- [ ] The stale Muse mocked fixture no longer claims Muse uses Chat Completions upstream.

## Live OpenCode Go

- [ ] Current provider model/surface table is re-verified immediately before live execution.
- [ ] Native representative surface matrix succeeds with a real key.
- [ ] At least one real cross-surface client/upstream case succeeds and outbound observation proves the upstream path.
- [ ] Streaming completes for representative Responses, Chat and Messages surfaces.
- [ ] Muse reasoning is Responses-native and does not fabricate Anthropic budget fields from effort labels.
- [ ] Invalid-key/ambiguous-401 isolation case deterministically exercises the intended bad account first.
- [ ] Immediate valid follow-up succeeds without restart/rehash/DB reset.

## Scope discipline

- [ ] No new dependency is required.
- [ ] No DB migration is required.
- [ ] No new CI matrix/live-provider gate is added.
- [ ] No OpenCode Go model list is hard-coded into Python dispatch logic.
- [ ] No provider-specific retry loop bypasses canonical failure effects.
- [ ] Plan 154's production single-flight acceptance remains green.

---

# Rejection conditions

Do not declare the wire-negotiation line complete if any of these remain true:

- single-flight exists only in unit tests but not production dispatch;
- unhinted known models can be misclassified as globally absent before alternate-surface discovery;
- a test named for Muse still asserts `/chat/completions` as Muse's real upstream contract;
- cross-surface behavior is tested only at codec/unit level;
- live OpenCode Go tests skip and the closure record still calls live verification complete;
- invalid-key live acceptance can skip merely because routing chose the good account first;
- a wrong-surface response can still poison an account;
- a 429/5xx/timeout causes surface roulette;
- reasoning effort is translated into an arbitrary numeric budget before final target selection.

---

# Handoff order

1. Implement Plan 154 first or in parallel, but do not close Plan 155 until its production single-flight tests pass.
2. Add provider-model-presence context to canonical failure observation.
3. Narrow weak unsupported-model handling without weakening strong true-absence behavior.
4. Add unhinted-model deterministic migration test.
5. Correct the stale Muse deterministic fixture.
6. Add both deterministic cross-surface coordinator cases.
7. Make live invalid-key account ordering deterministic.
8. Add real cross-surface live request and strengthen Muse reasoning assertion.
9. Run focused + ordinary gates.
10. Run the real OpenCode Go live suite with a supplied test key.
11. Record exact closure evidence and stop this line unless live provider behavior reveals a new concrete defect.
