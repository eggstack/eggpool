# Plan 153 — Live E2E Wire Migration and Rollout Closure

Date: 2026-09-02
Status: implementation complete; credentialed live verification pending
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Depends on: Plans 148–152
Planning baseline: `0bc0e02bbea5eebae70b247542d084e6fa6b122f`
Priority: P0 acceptance / regression closure
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Close the dynamic wire-negotiation line of work with real upstream verification, explicit stale-profile relearning tests, failure-isolation checks, migration validation, and a restrained rollout gate appropriate for EggPool's local/LAN SBC scope.

Mocked provider tests remain necessary for deterministic state-machine coverage, but they are insufficient to prove:

- the real endpoint used by an upstream today;
- endpoint-specific authentication/header behavior;
- current thinking/reasoning request semantics;
- live SSE event framing/termination;
- real 401/model-absence/schema errors;
- whether an upstream silently changed a model from one surface to another.

This plan therefore requires opt-in live E2E with supplied credentials. It explicitly does **not** put those credentials or provider calls into ordinary CI.

---

# Precondition

Plans 148–152 must be implemented enough that:

- provider candidates are represented as wire profiles;
- the coordinator can rebuild an outbound request from canonical intent for an alternate profile;
- failure effects distinguish wire transition from account failover;
- ambiguous 401 does not poison account health;
- runtime preference learning/single-flight is enabled;
- Chat, Responses, Messages and the planned Gemini codecs have their focused deterministic coverage.

Do not use live tests to discover basic local type/codec bugs that should be caught deterministically first.

---

# Existing fake-live test disposition

At the planning baseline, `tests/integration/test_muse_spark_live_e2e.py` is not a live test. It uses `respx` to mock the upstream and hard-codes Muse Spark 1.2 Contributor as an Anthropic `/messages` model.

Before adding live acceptance:

- rename/move the deterministic test to reflect that it is mocked integration coverage, or replace it with generic wire-negotiation fixtures;
- remove its Anthropic-only Muse assumption;
- ensure no test named `live` can pass without making a real provider network request.

This naming distinction matters because the regression survived precisely because the existing test proved an internal assumption rather than the upstream contract.

---

# Live test isolation

Create an explicit opt-in marker, for example:

```text
pytest -m live_provider
pytest -m live_opencode_go
pytest -m live_gemini
```

The exact marker names should follow current pytest conventions.

Requirements:

- excluded from default `pytest`, smoke and CI unless explicitly selected;
- skip cleanly when required environment variables are absent;
- never print keys in pytest output/logs;
- use low-token prompts and bounded `max_output_tokens`/equivalent;
- use unique temporary EggPool database/config state so a developer's normal runtime is not mutated;
- do not assume test ordering;
- restore/delete temporary state after the run;
- no provider billing benchmark or load test.

Live tests may be used manually before release or after provider contract changes.

---

# Credential variables

Use provider-specific test-only environment names distinct from normal runtime config where practical.

Recommended:

```text
EGGPOOL_E2E_OPENCODE_GO_API_KEY
EGGPOOL_E2E_OPENCODE_GO_API_KEY_2   # optional second valid account
EGGPOOL_E2E_GEMINI_API_KEY          # optional when Gemini direct provider is implemented
```

For failure-isolation testing, do not require a second real bad credential secret. Derive/use an obviously invalid test value in the ephemeral test config if the provider safely returns a normal auth error for it.

Never commit actual keys, `.env` contents or captured Authorization/x-api-key headers.

---

# Live request instrumentation

The live suite must assert what EggPool actually sends upstream, not merely that the client got HTTP 200.

Add a test-only/diagnostic outbound observation hook at the EggPool HTTP boundary that records only sanitized structural facts:

```text
provider_id
account_id or stable test alias
canonical model_id
wire surface ID
sanitized request path
HTTP status
selected auth scheme name, not credential/header value
request semantic field names / redacted shape
stream/nonstream
attempt ordinal
wire selection source
```

Do not record raw request content, tool arguments, API key, full response body or sensitive query parameters.

If existing routing trace/attempt metadata can expose these structural fields safely, reuse it instead of building another observer.

The live test must be able to prove:

```text
Muse -> /responses
MiniMax M3 -> /messages
MiMo/GLM -> /chat/completions
```

without trusting the same resolver code under test to report its own expected URL incorrectly.

---

# Required OpenCode Go live matrix

Re-check the current official Go model table immediately before execution and update model IDs if the service changed.

At the 2026-09-02 planning point, use representative models from all three documented surfaces:

| Model | Expected current surface |
| --- | --- |
| Muse Spark 1.2 Contributor | OpenAI Responses |
| GPT-5.6 Luna | OpenAI Responses |
| MiniMax M3 | Anthropic Messages |
| MiMo-V2.5 or current GLM-5.3 Flash equivalent | OpenAI Chat Completions |

The purpose is not to test every Go model. Four representative models prove heterogeneous routing.

For each representative surface, run a minimal **non-streaming** request first.

Then run at least one **streaming** request per surface family:

- one Responses model;
- one Chat model;
- one Messages model.

Muse should receive extra coverage because it triggered the regression.

---

# Basic live acceptance per model

For a minimal request:

1. call EggPool through its public client endpoint, not the provider directly as the main assertion path;
2. require successful EggPool routing/response;
3. assert the outbound sanitized observation used the selected model's expected current wire profile/path;
4. assert account remains healthy after the request;
5. immediately issue a second request and verify the learned runtime preference avoids extra negotiation attempts;
6. issue a request to a sibling model afterward to prove no cross-model/account poisoning occurred.

Use provider-direct calls only as diagnostic controls when EggPool behavior is unclear, not as a replacement for proxy E2E.

---

# Thinking/reasoning live acceptance

This line of work began with incorrect thinking-level handling, so live tests must verify actual outbound semantic shape.

Do not assert that all models expose the same effort vocabulary.

For each selected reasoning-capable representative model:

1. query/use current bundled/catalog capability facts;
2. choose one or two documented supported controls;
3. send the control through a public EggPool endpoint;
4. assert the selected wire encoder emitted the correct **surface-native** structure;
5. require a successful upstream response or a current provider-documented capability error;
6. assert no arbitrary Anthropic budget was fabricated from an OpenAI/Gemini effort unless an explicit model capability mapping requires that exact transformation.

### Muse

When current Go docs/provider behavior still describe Muse as Responses:

- outbound surface must be `openai_responses`;
- reasoning effort must use Responses-native semantics;
- no `thinking: {budget_tokens: ...}` may be invented solely from Muse effort labels;
- a reasoning-related rejection must not mark Go account auth invalid.

### MiniMax M3

Do not assume the old low/medium/high budget mapping is correct merely because it exists in current EggPool metadata. Verify current live/provider docs and either:

- prove the supported Messages thinking control; or
- correct/degrade the capability metadata in the implementation before declaring closure.

The live suite should be small; it exists to validate provider facts that mocks cannot guarantee.

---

# Streaming live acceptance

For each of the three primary Go surfaces verify:

- EggPool returns the correct **client** wire grammar;
- the upstream surface's terminal evidence is recognized;
- client stream terminates without hanging indefinitely;
- usage/finalization occurs once;
- a missing visible reasoning delta is not itself treated as a failure if the upstream is otherwise alive;
- an explicit upstream stream error is not recorded as success;
- no retry occurs after downstream response start.

Use bounded read timeouts appropriate for current reasoning models, but do not reduce them so aggressively that valid silent reasoning becomes a false failure.

If a current upstream has a documented terminal quirk, encode that as a narrowly scoped profile/codec policy with deterministic tests. Do not make all stream observers accept arbitrary EOF.

---

# Stale-profile relearning — deterministic required, live opportunistic

The core product requirement is that a model can move surfaces over time.

A real provider cannot be forced to migrate during a test, so this requires two layers.

## Deterministic integration acceptance

Use an in-process fake upstream that can switch contract during the test:

```text
phase A:
  Responses accepted
  Chat rejected

phase B:
  Responses returns safe endpoint/surface rejection
  Chat accepted
```

Acceptance:

1. first request learns Responses;
2. fake upstream switches to phase B without rehash/restart;
3. next request tries learned Responses, receives a safe rejection, tries Chat on the same account within budget, and succeeds;
4. Chat becomes preferred;
5. subsequent request uses Chat directly;
6. account health is unchanged;
7. no database reset/restart occurs.

Repeat one inverse migration if cheap; one well-designed state transition is sufficient for the ordinary gate.

## Live stale-hint acceptance

For a current live model, seed the ephemeral resolver/config with a deliberately wrong **non-fixed** preference that is still a configured candidate.

Example when current Muse is Responses:

```text
preferred hint = anthropic_messages
```

Then issue the real request.

Only run this test if the wrong candidate returns a known safe deterministic rejection. If the provider's wrong surface has ambiguous behavior or could actually start inference, skip the live stale-hint transition and rely on the deterministic migration test.

Acceptance when safe:

- wrong surface is rejected;
- no account is marked authentication_failed;
- same account tries the correct candidate;
- request succeeds;
- correct profile is learned;
- second request is single-attempt on the learned profile.

---

# 401/auth failure isolation live acceptance

Use an ephemeral provider pool with:

```text
account bad  -> deliberately invalid key
account good -> supplied valid key
```

Run a model whose current wire surface is already known/verified.

Acceptance:

- explicit invalid credential disables/suppresses only the bad account according to Plan 151;
- the wire profile is not invalidated merely because one credential is bad;
- the good account is tried on the same wire surface and succeeds within attempt budget;
- subsequent requests use the good account normally;
- no restart/database wipe is needed.

Separately exercise a wrong-surface/"missing API key" style response if it can be obtained safely:

- it must **not** set authentication_failed on the valid account;
- a sibling model request must still succeed immediately afterward.

This is the live regression for the current provider-wide poisoning bug.

---

# Request/schema failure isolation

Send one deliberately invalid but safe request control/schema case through a valid account, chosen so the provider rejects before inference.

Acceptance:

- EggPool classifies it as local/request or explicit wire-schema failure according to Plan 151;
- account health remains usable;
- it does not recursively retry the same deterministic client error across every account;
- an immediate valid request succeeds.

Do not send destructive tool calls or prompts with external side effects for these tests.

---

# Single-flight live/concurrency acceptance

Do not run a high-load benchmark against a paid provider.

Use deterministic fake upstream concurrency as the mandatory test:

- seed one stale profile;
- launch e.g. 10–20 concurrent proxy requests;
- assert one negotiation leader for the provider/model;
- assert bounded negotiation-only attempts independent of follower count;
- assert followers converge after alternate acceptance;
- assert normal total request attempt limits remain enforced.

Optionally run a tiny live concurrency check (2–3 requests) only if provider terms/rate limits permit and it adds evidence. It is not required for closure.

---

# Provider negotiation rate-limit acceptance

Mandatory deterministic case:

- preferred profile receives safe surface rejection;
- alternate candidate returns 429 with `Retry-After`;
- no third candidate is attempted;
- provider negotiation governor blocks immediate repeated negotiation;
- account-rate-limit handling remains separate;
- wire preference is not marked bad because of 429.

No deliberate live provider rate-limit triggering is required or desirable.

---

# Gemini live acceptance

If a Gemini direct provider template/connect path is implemented in Plan 152 and a key is supplied, run a small optional live matrix.

## Interactions

- stateless `store=false` request;
- non-stream response;
- one stream with `interaction.completed`/current terminal semantics;
- one portable tool or reasoning request if current model supports it;
- assert no `previous_interaction_id` is used.

## generateContent

- one non-stream request;
- one stream using the dedicated streaming method/path;
- assert model ID is safely rendered into the path template.

Do not block OpenCode Go regression closure on Gemini credentials if the codec has strong deterministic tests and direct Gemini is not yet a bundled user-facing provider. Record whether Gemini live was run.

---

# Migration from current config/runtime

Verify existing user-facing configuration continues to work:

- legacy provider `protocols` + `openai_path` / `anthropic_path` / `responses_path` synthesize profiles;
- explicit new `wire_surfaces` config passes `check-config`;
- `rehash` can load the new structure without a hard restart;
- a rehash changing path/auth/candidate surfaces changes the fingerprint and invalidates stale learned profile state;
- a rehash that leaves structural candidates unchanged may retain process-local learned preference;
- default and SBC configs remain lean.

Do not require users to rewrite configs for this release.

If OpenCode Go bundled defaults are corrected, existing manually configured OpenCode Go providers should continue to work through legacy synthesis where possible. Document any case where surface-specific auth requires opting into the new explicit surface config.

---

# Operational observability closure

Ensure existing status/debug tooling can answer, without exposing secrets:

```text
Which provider/account/model was selected?
Which wire surface was used?
Was it selected from runtime success, operator hint, bundled hint, or fallback order?
Did negotiation occur?
Why was a candidate rejected?
Did account health change, and for what independent reason?
```

This does not require a new dashboard page or persisted event table.

A structured debug log and/or current routing trace fields are sufficient for SBC/local diagnosis.

Do not log raw provider response bodies to make debugging easier.

---

# Documentation updates

Update active docs/config comments to state:

- EggPool distinguishes client API from upstream wire surface;
- providers/models may expose more than one candidate surface;
- surface selection can be learned/relearned at runtime;
- provider/model hints are not permanent truth unless explicitly fixed by operator config;
- reasoning controls are model capabilities and do not choose transport;
- negotiation is bounded and does not run background probes;
- live provider validation is opt-in;
- stateful Responses/Gemini interactions are not transparently failover-safe.

Correct any documentation claiming the mocked Muse test is live.

---

# Lean verification gate

Do not add a new GitHub Actions matrix.

Implementation should run focused tests for:

- wire registry/config;
- canonical request/event adapters;
- failure classifier/effects;
- negotiation resolver/single-flight;
- core surface codecs/stream observers;
- stale-profile migration;
- 401 isolation;
- shared attempt budget;
- rehash fingerprint behavior.

Then run the ordinary project gate at implementation time, expected to include roughly:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Run the explicit live suite manually with supplied keys after deterministic gates pass.

Do not make release correctness depend on a provider being online in GitHub Actions.

---

# Closure acceptance criteria

## Architecture

- [ ] Upstream wire surface is independent of provider/protocol/model capability metadata.
- [ ] Static/bundled model-surface knowledge is a revocable hint unless explicitly fixed.
- [ ] Learned runtime profile can change without restart/database reset.
- [ ] No background probe or persistent surface-history subsystem exists.

## Failure isolation

- [ ] Bare/ambiguous 401 cannot poison an account.
- [ ] Confirmed invalid credential affects only the proven bad account.
- [ ] Surface/schema failures do not alter account health.
- [ ] Rate limits do not alter surface knowledge.
- [ ] 5xx/transport/midstream failures do not trigger alternate-surface inference.
- [ ] A failure on one model cannot make unrelated provider models return "no account available" through accidental account-wide poisoning.

## Negotiation

- [ ] Deterministic stale-profile migration relearns a new surface in-process.
- [ ] Next request uses learned surface directly.
- [ ] Same-account wire fallback happens before irrelevant account cycling.
- [ ] Account retry + surface fallback share one total upstream-submission budget.
- [ ] Single-flight prevents N concurrent requests from performing N independent negotiations.
- [ ] Provider negotiation concurrency/interval/429 pressure are bounded.

## API semantics

- [ ] Chat, Responses and Messages are distinct request/stream grammars.
- [ ] Responses client output is real Responses grammar regardless of upstream surface.
- [ ] Anthropic client output is real Messages grammar regardless of upstream surface.
- [ ] Reasoning intent is encoded from selected model capability/surface without guessed equivalence.
- [ ] Muse Spark current live request uses the current documented Go surface and does not fabricate Anthropic budget semantics when Responses is the actual target.
- [ ] Current Go representative models exercise all three documented surface families live.

## Live evidence

- [ ] `live_opencode_go` runs against the actual provider with a supplied key and no mocked HTTP layer.
- [ ] At least one non-stream and stream request succeeds for each current Go surface family.
- [ ] Sanitized outbound observations prove actual path/surface selection.
- [ ] Live failure-isolation case proves a bad/wrong request cannot poison the valid provider pool.
- [ ] A second valid request after negotiation/failure proves no restart/DB refresh is needed.
- [ ] Optional Gemini live status is recorded if credentials/provider template are available.

## Scope/resource discipline

- [ ] No new provider SDK dependency.
- [ ] No DB migration solely for wire learning.
- [ ] No new broad CI matrix/live-provider CI job.
- [ ] No stateful Responses/Interactions failover feature creep.
- [ ] No plugin/DSL/general enterprise gateway subsystem.

---

# Rejection conditions

Do not declare this line complete if:

- only mocked tests have been run for the OpenCode regression;
- Muse succeeds only because the test still mocks `/messages`;
- wrong-surface 401 can still disable all accounts;
- the implementation hard-codes current Go model IDs in Python dispatch logic;
- a cached profile cannot be overturned without restart;
- every concurrent request independently probes alternate surfaces;
- alternate surfaces are tried after 429/5xx/timeout/midstream failure;
- retries can exceed the configured total budget due to nested loops;
- Responses streams are translated through Chat grammar internally and still stall/lose terminal events;
- reasoning effort is still converted to guessed provider budgets before final target selection;
- provider keys are added to CI/secrets requirements for ordinary tests;
- a new closure-plan chain is created instead of recording final evidence here when the implementation satisfies this plan.

---

# Closure record template

Append this section when implementation is complete:

```text
Implementation SHA(s):
Implementation date:
Official docs re-verified:
Focused deterministic tests:
Ordinary project gate:
OpenCode Go live command:
OpenCode Go model/surface results:
Live stream results:
401/failure isolation result:
Stale-profile relearning result:
Single-flight concurrency result:
Gemini live result (run/skipped + reason):
Known intentional limitations:
```

Never paste API keys or raw auth/error payloads into the closure record.

---

# Handoff

1. Implement Plans 148–152 and pass deterministic tests first.
2. Reclassify the existing mocked Muse test.
3. Add explicit live markers/env handling and sanitized outbound observation.
4. Run the current OpenCode Go three-surface live matrix with supplied credentials.
5. Run reasoning and streaming checks, especially Muse.
6. Run invalid-key/wrong-surface failure isolation and immediate sibling-model recovery.
7. Run deterministic stale-profile migration, single-flight and negotiation-429 tests.
8. Verify legacy/new config and rehash behavior.
9. Run the ordinary lean gate.
10. Append exact closure evidence here and stop this planning line unless a genuinely new defect is found.

---

# Closure record

Implementation SHA(s): `e34cd9ef8dd26ca07ac367b2604859b1d29f92c3`
Implementation date: 2026-09-02
Official docs re-verified: [OpenCode Go endpoint table](https://dev.opencode.ai/docs/go/); the representative model/surface assignments in `docs/live-wire-e2e.md` match the current table.
Focused deterministic tests: `tests/integration/test_wire_negotiation_e2e.py`, resolver, failure-effects, and signal-extraction coverage — 90 passed; the broader focused wire/failure/reload selection also passed with 146 passed and 5 skipped.
Ordinary project gate: `uv sync --frozen --extra ci`, Ruff format/check, Pyright, both example-config `check-config` commands, and smoke tests (`14 passed`) all passed. The full repository suite passed with `7769 passed, 40 skipped`; one existing FastAPI/Starlette deprecation warning was emitted by the installed test dependency.
OpenCode Go live command: `uv run pytest tests/live/test_opencode_go_wire_live.py -m live_opencode_go -q`
OpenCode Go model/surface results: skipped cleanly because `EGGPOOL_E2E_OPENCODE_GO_API_KEY` was not set in the execution environment; no provider call was made.
Live stream results: skipped for the same missing credential; the suite contains Responses, Chat Completions, and Anthropic Messages terminal-evidence checks without a mocked HTTP layer.
401/failure isolation result: deterministic failure-isolation coverage passed; credentialed live invalid-key isolation remains pending because no live key was supplied.
Stale-profile relearning result: the new deterministic integration test passed; the same account moved from `/responses` to `/chat/completions`, retried within the shared attempt budget, learned the alternate, and used it on the next request without restart or database reset.
Single-flight concurrency result: existing deterministic resolver/reload concurrency coverage remained green in the focused and full suites; no live concurrency probe was run.
Gemini live result (run/skipped + reason): skipped; `EGGPOOL_E2E_GEMINI_API_KEY` was unset and direct Gemini live verification is optional. Deterministic Gemini codec/path coverage remains in the ordinary suite.
Known intentional limitations: credentialed OpenCode Go evidence, including live MiniMax reasoning acceptance and live invalid-key isolation, must be run manually before release when a test-only key is available. Live provider calls remain excluded from CI, and the official model list may change.
