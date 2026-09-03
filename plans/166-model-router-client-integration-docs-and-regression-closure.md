# Plan 166 — Model-Router Client Integration, Documentation, and Regression Closure

Date: 2026-09-03
Status: ready for implementation
Planning baseline: `525189763a3a6d506e9e8001e2426c9bd9a247fe`
Parent roadmap: `plans/162-optional-llm-model-router-selection-roadmap.md`
Depends on: Plans 163–165
Priority: P0 regression closure for request-path integration
Execution target: GPT-5.6 Luna or comparable implementation model

## Objective

Integrate the completed optional semantic model-router service into EggPool's shared client request path, expose configured virtual models truthfully, add bounded observability, update all operator/API/architecture documentation with copyable examples, and prove that feature-off behavior remains equivalent to the pre-feature baseline.

This is the only phase that should make a client request naming a configured virtual model resolve to a different concrete target. Keep the integration patch narrow and explicitly preserve the existing concrete-model path.

---

## 1. Exact hot-path insertion point

At the planning baseline, `src/eggpool/api/proxy_request.py` does the following after auth/body/JSON validation:

```text
validate payload model string
acquire generation lease / known providers
parse_model_provider(model_value)
normalize payload model to concrete model_id
build canonical request
validate Responses stateless contract
estimate/check concrete model limits
prepare protocol transcode
construct ProxyRequestContext
RequestCoordinator.execute()
```

Virtual resolution must happen after the client model string is validated and the request has a generation lease, but **before** the model is treated as a concrete `model/provider` reference and before model-specific context/capability/transcode checks.

Recommended shape:

```python
requested_model = model_value
compiled_router = generation.model_router_registry.get(requested_model)

if compiled_router is not None:
    selection = await generation.model_router_service.resolve(...)
    model_value = selection.concrete_model

# existing concrete path resumes here
model_id, provider_id = parse_model_provider(model_value, known_providers)
...
```

The `else`/miss path must preserve the existing concrete request ordering and objects. Do not move canonicalization/transcoding/context checking for every request merely to support the optional branch.

If selector semantic extraction needs an early canonical view, build it only inside the virtual branch. Once a concrete model is resolved, the ordinary authoritative concrete path remains responsible for request validation/transcoding.

---

## 2. Feature-off identity requirement

This is the highest-priority acceptance criterion.

With no `[model_routers.*]` tables configured:

- no semantic prompt extraction;
- no selector invocation;
- no affinity key/hash computation;
- no affinity cache insertion/lookup;
- no extra DB read/write;
- no extra provider/account selection;
- no new retry/submission budget;
- no background task;
- no changed request model normalization;
- no changed context-limit/capability/transcoding behavior;
- no changed routing score/fairness/health/backoff behavior;
- no synthetic `/v1/models` rows;
- no additional required headers;
- no altered error shape.

Add a sentinel regression test that replaces/instruments the model-router service so it raises if invoked, then sends ordinary concrete requests through Chat Completions, Responses, and Messages. Those requests must succeed through the normal path without touching semantic routing.

Where current test fixtures compare durable request/attempt rows, verify the row counts remain identical feature-off.

---

## 3. Cross-surface virtual request integration

Because all supported inference endpoints share `handle_proxy_request()`, implement one semantic-resolution hook there rather than protocol-specific copies.

Cover:

- OpenAI Chat Completions;
- OpenAI Responses under EggPool's existing stateless contract;
- Anthropic Messages.

For each surface:

1. the public payload keeps the operator's virtual model ID only until semantic resolution;
2. selector prompt extraction receives the surface's semantic user/system content through the bounded Plan 164 logic;
3. resolved `model_value` is then parsed/normalized exactly like a normal concrete model reference;
4. context windows, reasoning controls, modality/tool capability, provider protocol, and transcoding are evaluated against the **resolved concrete target**;
5. upstream receives only the concrete target, never the virtual alias;
6. client response semantics remain the same as if that concrete model had been requested directly, apart from optional EggPool diagnostic metadata if already allowed by the endpoint contract.

Do not make the virtual alias alter provider/account selection after concrete resolution.

---

## 4. Provider-qualified target support

A route may map directly to the existing concrete `model/provider` syntax, for example:

```toml
[model_routers.implementer.routes.local-fast]
model = "qwen3-4b/llamacpp-local"
description = "Use for fast local implementation questions."
```

After semantic resolution, `parse_model_provider()` must receive that configured concrete reference unchanged and preserve current provider-scoped behavior.

Regression-test both unsuffixed targets and provider-qualified targets. Do not invent a second provider field inside `ModelRouteConfig`.

---

## 5. Context/capability truth after resolution

A virtual model cannot truthfully advertise one target's concrete context length or reasoning/tool/modality controls as universal.

At request time:

- resolve virtual -> concrete first;
- run existing context estimation and `_check_context_limits` against that concrete model/provider;
- run existing reasoning/thinking capability classification against that concrete target;
- run existing transcode preflight/provider-bound capability checks against that concrete target;
- return current concrete-target error semantics when incompatible.

Do not have the selector override deterministic capability failures. The selector provides semantic specialization; EggPool's existing deterministic correctness checks remain authoritative.

Optional future pre-filtering of definitely incompatible routes is out of scope unless implementation evidence shows a strong need. Do not filter routes based on transient health/quota state in v1 because that would destabilize sticky model affinity and duplicate the existing account router's job.

---

## 6. `/v1/models` virtual entries

Extend model-list construction to append configured virtual models only when present.

Use EggPool's existing namespaced metadata convention. A representative synthetic object:

```json
{
  "id": "implementer",
  "object": "model",
  "owned_by": "eggpool",
  "name": "implementer",
  "eggpool": {
    "virtual": true,
    "model_router": true
  }
}
```

Requirements:

- do not expose selector prompt text;
- do not expose raw session/cache state;
- avoid claiming concrete price/context/capabilities unless a future conservative aggregate contract is implemented and proven truthful;
- client-visible route descriptions/targets are not required in `/v1/models`; keep the catalog surface small;
- stable ordering: preserve current concrete model ordering and append/sort virtual entries deterministically according to existing endpoint conventions;
- feature-off output unchanged for the same underlying catalog;
- if a configured virtual ID collides with an unsuffixed concrete ID, the explicit virtual alias wins exact unsuffixed exposure and one bounded diagnostic explains the collision. Provider-qualified concrete entries remain available.

Update API/model serialization tests, including `models.collapse_models = true/false` combinations.

---

## 7. Semantic decision observability

Add bounded, privacy-preserving observability without a new DB subsystem.

At minimum track aggregate counters/latency for:

- virtual requests;
- selector decisions;
- affinity hits/misses;
- default fallbacks grouped by broad reason (`timeout`, `unavailable`, `invalid_output`, `repair_failed`);
- repair attempts/success;
- semantic resolution latency;
- configured virtual -> concrete selection counts if the existing metrics architecture can hold bounded labels safely.

Use low-cardinality labels. Do not label metrics with arbitrary session IDs, request text, raw selector output, or user-configured descriptions if that can explode cardinality.

Logging/diagnostic events may include:

- requested virtual ID;
- resolved concrete target;
- route label;
- source (`selector`, `affinity`, `default`);
- selector attempt count;
- bounded latency.

Do not persist selector prompts or the raw `X-EggPool-Route-Session` header. Existing selector child requests remain visible in ordinary request/cost/accounting data under their concrete selector model.

If existing routing-decision persistence is tightly coupled to provider/account scoring, do **not** overload it with semantic model decisions merely for convenience. Keep the two diagnostic concepts distinct.

---

## 8. Error/failure semantics

Prove the following end-to-end:

### Selector failure + healthy default

Client sees normal response from configured default. Selector error remains internal diagnostic information.

### Selector failure + unavailable default

Client receives the same model/account availability error that a direct request to the default concrete model would have produced. Do not return a synthetic "router failed" error unless no concrete default resolution can be constructed at all due to invalid configuration (which should have been prevented by Plan 163).

### Valid selection + target failure

Use ordinary account/provider failover for that concrete model. Do not semantically reroute to another configured target after upstream submission.

### Parent cancellation

Cancel selector/target work and run existing cleanup. Do not dispatch the default after cancellation.

### Invalid route output

Never treat output as a concrete model string. Only exact compiled IDs can resolve; exhausted repair -> configured default.

---

## 9. Session-header API behavior

Document and test:

```text
X-EggPool-Route-Session: <opaque stable session id>
```

This header is optional. It has no effect on concrete model requests. On a sticky virtual model it gives the strongest model-affinity guarantee.

For OpenAI Responses, where EggPool intentionally does not accept upstream/server-side conversation state, this is the recommended way to carry affinity across independent calls.

For Chat Completions/Messages with repeated transcript prefixes, EggPool may derive automatic affinity when the header is absent.

The header is EggPool-local and must never be sent to upstream providers.

Do not add cookies, server-side conversation creation APIs, or persisted conversation records.

---

## 10. Documentation implementation

Documentation is part of feature completeness, not a follow-up.

### `config.example.toml`

Add a clearly optional commented example near routing/model configuration. It should be copyable and include:

- client-visible virtual alias;
- small/local selector example;
- required default model;
- at least three routes showing difficulty **and** specialization, not only difficulty;
- sticky/TTL/input/timeout/repair controls;
- note that selector/targets use ordinary EggPool concrete model IDs and may be provider-qualified.

Suggested example:

```toml
# Optional semantic model routing. No [model_routers.*] table means disabled.
# [model_routers.implementer]
# selector_model = "qwen3-0.6b/llamacpp-local"
# default_model = "muse-spark-1.3"
# sticky = true
# affinity_ttl_s = 43200
# selector_timeout_s = 2.0
# max_input_bytes = 2048
# repair_attempts = 1
#
# [model_routers.implementer.routes.hard]
# model = "muse-spark-1.3"
# description = "Use for the most difficult implementation and reasoning tasks."
#
# [model_routers.implementer.routes.code]
# model = "gpt-5.6-luna"
# description = "Use for implementation, debugging, refactoring, and code review."
#
# [model_routers.implementer.routes.research]
# model = "research-model"
# description = "Use for research-heavy synthesis and long technical reading."
```

Do not imply these example model names/providers are universally available.

### New `docs/model-routing.md`

Write a focused operator guide with:

1. what model routing is and how it differs from EggPool's provider/account routing;
2. fully optional/default-off behavior;
3. complete TOML schema and multiple examples;
4. how to choose a selector model (small/local/low latency, follows exact-output instruction, enough domain understanding for configured distinctions);
5. how route descriptions should be written: short, mutually distinguishing, operator-intent focused;
6. deterministic minified prompt behavior and why full conversation/tool schemas are excluded;
7. default fallback semantics;
8. sticky affinity/cache-locality behavior;
9. `X-EggPool-Route-Session` examples and Responses-specific recommendation;
10. selector usage accounting/cost implications;
11. rehash behavior and when a session reclassifies;
12. provider-qualified targets;
13. troubleshooting malformed selector output/default fallbacks;
14. limitations: no nested routers, no semantic failover after target submission, no restart persistence, not an authorization boundary.

### `docs/configuration.md`

Add field-by-field configuration reference, validation/bounds, live-rehash status, collision semantics, and disabled-by-default statement.

### `docs/api-reference.md`

Document virtual model catalog metadata and `X-EggPool-Route-Session`. State that upstream receives the resolved concrete model.

### `docs/live-config-rehash.md`

Document router fingerprint behavior:

- unchanged router -> existing process affinity survives;
- changed semantic router -> next request reclassifies;
- removed router -> old affinity unreachable;
- process restart -> affinity is intentionally lost.

### Architecture documentation

Update `architecture/deep-dive-routing.md` to add a semantic model-selection stage **before** the current concrete model/provider/account diagram. Preserve and explicitly repeat the invariant that semantic cache affinity never enters `QuotaFairScorer`.

Update the request-lifecycle deep dive if needed to show that selector child requests use ordinary `RequestCoordinator.execute()` and that the parent target request resumes the normal path after resolution.

### `README.md` and `CHANGELOG.md`

Add a concise optional-feature example/link and implementation release note when the feature lands.

---

## 11. Regression test matrix

Use existing test organization; do not add a permanent CI matrix. Add focused unit/integration tests and then run the normal repo gates.

### Feature-off regression

Test all three public request surfaces with no routers configured:

- concrete unsuffixed model;
- concrete provider-qualified model;
- collapsed and non-collapsed catalog where existing suites support it;
- streaming and non-streaming representative paths;
- reasoning/tool request representative path;
- normal model-not-found/error path.

Assert selector/affinity service is not invoked and durable row behavior remains expected.

### Feature-on happy paths

- selector chooses each configured route;
- provider-qualified route target;
- selector unavailable -> default;
- selector invalid -> repair -> valid;
- selector invalid -> repair fails -> default;
- same explicit session -> affinity hit/no second selector;
- automatic transcript-prefix affinity where supported;
- sticky false -> selector each request;
- Responses + explicit session header;
- synthetic `/v1/models` entry.

### Cross-protocol/capability paths

- OpenAI client -> Anthropic-native target through existing transcoder;
- Anthropic client -> OpenAI-native target through existing transcoder;
- target context-limit rejection remains correct;
- target reasoning-control rejection/normalization remains correct;
- tools/modalities checked against target;
- selector's own protocol adaptation works independently of target protocol.

### Failure isolation

- selector timeout no leaked reservation/request attempt;
- parent cancellation no default work;
- target 429/5xx/transport failures use ordinary account/provider failover only;
- target failure does not delete/reselect semantic affinity;
- malformed selector output never becomes a model ID;
- selector failures do not poison unrelated target account health;
- target failures do not poison selector state beyond existing concrete model health handling;
- invalid rehash candidate leaves live traffic on old generation.

### Rehash/affinity

- enable routers live;
- disable routers live;
- unrelated config change retains affinity;
- route/description/selector/default change invalidates by fingerprint;
- concurrent generation leases finish on their leased generation;
- no mixed old-config/new-affinity semantics.

### Privacy/header

- raw session ID not in upstream headers;
- raw session ID not in DB rows/log capture/metric labels;
- selector prompt/raw output not persisted by new semantic-routing code;
- virtual aliases/descriptions do not become high-cardinality uncontrolled metrics.

### Existing routing invariants

Retain existing tests/audits proving:

- `QuotaFairScorer` input contract unchanged;
- cache metrics/policy do not enter account scoring;
- routing priority/fairness scope unchanged;
- wire negotiation/retry budget unchanged;
- upstream backoff semantics unchanged.

---

## 12. Performance/resource closure

For feature-off requests, measure/inspect that the new code is effectively one exact registry miss/false branch and does not allocate prompt/affinity state.

For affinity-hit virtual requests, semantic overhead should be bounded to:

- virtual registry lookup;
- session digest derivation or explicit header hash;
- in-memory affinity lookup;
- resolved concrete model handoff.

No selector inference occurs on the hit path.

For affinity-miss virtual requests, selector latency is expected, but local EggPool work remains bounded by Plan 164's byte limits.

Do not introduce a microbenchmark CI threshold. A small local/manual timing characterization is sufficient if needed to guard an obvious accidental whole-transcript/DB hot-path regression.

---

## 13. Final verification gates

At implementation closure run, using the repository's existing environment/tooling:

```bash
ruff format --check src tests
ruff check src tests
pyright
pytest
```

If the repository's actual CI invokes narrower smoke commands, run those as well; do not replace them with a plan-specific workflow.

Also inspect:

- migration list/version unchanged unless an unrelated concurrent change changed it;
- dependency list unchanged;
- background task inventory unchanged feature-off;
- generated/example TOML parses;
- documentation examples match actual field names/bounds;
- `git diff` contains no accidental provider/account routing/scorer rewrite.

Live-provider verification is optional/manual and must not become mandatory for this feature. Mock/local-compatible integration tests are the regression boundary.

---

## Acceptance criteria

Plan 166 and the parent roadmap are complete when:

1. Configured virtual IDs work across Chat Completions, Responses, and Messages.
2. Missing/empty router configuration preserves pre-feature concrete behavior and resource profile.
3. Resolved concrete models go through all existing context/capability/transcoding/provider/account correctness checks.
4. Virtual `/v1/models` entries are truthful and feature-off output is unchanged.
5. Selector/default/affinity decisions are observable without persisting sensitive prompts/session IDs.
6. Explicit session affinity is documented and never forwarded upstream.
7. No semantic target spray occurs after target submission.
8. Rehash semantics are atomic and fingerprint-correct.
9. `config.example.toml`, configuration/API/rehash/architecture docs, new model-routing guide, README, and changelog are updated with working examples.
10. Focused regression tests plus existing Ruff/Pyright/Pytest gates pass without a new dependency, DB migration, or permanent CI job attributable to this feature.
