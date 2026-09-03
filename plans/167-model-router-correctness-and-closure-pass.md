# Plan 167 — Model-Router Correctness and Closure Pass

Date: 2026-09-03
Status: ready for implementation
Planning baseline: `9b8815bfb6ea88cceb3911f8c3ffd2d8f7f488a3`
Parent work: `plans/162-optional-llm-model-router-selection-roadmap.md` through `plans/166-model-router-client-integration-docs-and-regression-closure.md`
Priority: P0 correctness / closure
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Objective

Close three correctness gaps found during post-implementation review of the optional semantic model-router feature, then requalify the exact resulting repository head with the normal project verification gates.

This is a **corrective pass, not a redesign**. Preserve the architecture that already landed:

- semantic virtual-model selection remains separate from provider/account routing;
- selector inference continues through `RequestCoordinator.execute()`;
- the normal concrete-model path remains behaviorally unchanged when no virtual alias matches;
- route affinity remains model-level only and never enters `QuotaFairScorer` or account fairness;
- nested virtual routers remain prohibited;
- no new dependency, DB migration, background task, persistent session store, or CI matrix is required.

Do not broaden this pass into capability-aware semantic route filtering, health-aware cross-model failover, embeddings, classifier persistence, or a second routing subsystem.

---

## Findings to correct

### Finding A — repair inference loses the request being classified

Current implementation:

- `compile_selector_prompt()` sends the static route policy plus the bounded semantic request view;
- when the selector returns a successful but invalid answer, `compile_repair_prompt()` creates a new request containing the static policy plus only a small `invalid;reply only:...` instruction;
- the original bounded semantic request is absent from the repair request.

As a result, a repair inference knows the valid IDs but no longer knows what request it is supposed to classify. A first answer such as `I choose 2` cannot be repaired meaningfully because neither the original semantic input nor the invalid output is present.

The intended contract from Plan 164 was a small deterministic repair, but it must still operate on the same bounded classification context.

### Finding B — automatic sticky-affinity identity can collapse long prompts

Current `automatic_session_identity()` uses a total byte budget and calls `_bounded_utf8(text, remaining)` before accounting for the per-field framing bytes. If a field consumes all `remaining`, the subsequent `field_size > remaining` check can reject the field entirely.

This creates two problematic cases:

1. a first user message larger than the automatic-prefix budget can contribute no user bytes to the digest;
2. a large common system/developer prefix can consume or invalidate the budget before the first user turn contributes.

For coding-agent workloads with large shared system prompts, unrelated conversations can therefore derive the same automatic affinity identity and be pinned to the same semantic model decision.

The automatic key is best-effort, but it must never degrade into a practically global key when a first user turn exists.

### Finding C — selector non-2xx responses are treated like malformed successful output

`RequestCoordinator.execute()` can return a `PreparedProxyResponse` with a non-2xx upstream status after ordinary concrete-model retry/failover is exhausted. `ModelRouterSelector` currently passes that body directly to `parse_route_id()` without first checking `status_code`.

A 4xx/5xx selector result therefore looks like invalid semantic output and can trigger the optional repair inference. That causes a second selector request even though the failure was transport/provider/model availability, not output formatting.

Required distinction:

- successful 2xx response + invalid route text -> eligible for the one repair attempt;
- non-2xx selector response -> selector unavailable, use configured default immediately;
- parent cancellation continues to propagate;
- timeout remains a timeout fallback after coordinator cleanup.

---

## 1. Preserve bounded semantic context during repair

Primary files expected:

- `src/eggpool/model_router/prompt.py`
- `src/eggpool/model_router/selector.py`
- `tests/unit/test_model_router_selector.py`

### Required behavior

The repair request must reuse the already-compiled, already-bounded initial selector context. Do **not** rebuild a semantic view from the full client payload and do not make a second canonicalization pass solely for repair.

Preferred shape:

1. `compile_selector_prompt()` produces the initial `SelectorPrompt` exactly as today.
2. If the first response is 2xx but the route ID is invalid, construct the repair from that `SelectorPrompt`.
3. Preserve:
   - the same static system policy;
   - the same bounded `variable_text` / semantic user content;
   - the same concrete selector model;
   - `stream = false`;
   - the same small output-token budget.
4. Add only one tiny fixed repair instruction, for example:

```text
invalid;reply only:0|1|2
```

A reasonable wire shape is:

```text
system: <same static policy>
user:   <same bounded semantic request>
user:   invalid;reply only:0|1|2
```

If avoiding consecutive user roles is preferable for compatibility, append the repair instruction to the already-bounded user message deterministically. Either approach is acceptable if it remains bounded and existing provider/transcoder compatibility is preserved.

### Do not include the invalid model output

The raw invalid answer is not needed to reclassify. Do not copy it into the repair prompt. This keeps repair content small and avoids expanding the prompt with arbitrary model output.

### Empty semantic view

If the initial selector prompt has no variable user message, the repair may contain the static policy plus the fixed repair instruction. Do not invent request context that was not present initially.

### API shape

Prefer a narrow signature such as:

```python
compile_repair_prompt(router, initial_prompt)
```

or an equivalent typed helper. Avoid storing mutable request context inside `ModelRouterSelector`.

### Tests

Add/adjust tests proving:

- initial invalid 2xx -> repair sees the exact same bounded semantic request text;
- route policy prefix is identical between initial and repair requests;
- repair instruction is fixed and bounded;
- raw invalid selector output is absent from the repair request;
- tool schemas/results, binary/base64 content, and excluded transcript history remain absent from repair just as they are from the first selector call;
- an oversized request remains within the original `max_input_bytes` semantic bound on repair;
- valid first-attempt output still performs one selector call only;
- `repair_attempts = 0` behavior is unchanged.

---

## 2. Make automatic affinity identity collision-resistant under long prefixes

Primary files expected:

- `src/eggpool/model_router/affinity.py`
- `tests/unit/test_model_router_affinity.py`

### Required invariant

For Chat Completions/Messages automatic affinity:

> If a non-empty first user turn exists and an automatic identity is returned, the digest must incorporate bounded bytes derived from that first user turn.

A very large system/developer prefix must not crowd the first user turn out of the digest, and framing overhead must never cause an otherwise selected field to be silently omitted after consuming the entire budget.

### Budget accounting

Fix byte accounting so field framing is included **before** deciding the text payload limit.

For each field, conceptually:

```text
available payload = remaining total budget - role/framing overhead
bounded text = truncate to available payload
write framing + bounded text
```

Never truncate to the whole remaining budget and then discover that the framing no longer fits.

### Reserve first-user entropy

Do not let arbitrary system/developer text consume the whole automatic-identity budget.

Use a deterministic bounded policy that guarantees space for the first user turn. One acceptable design:

- total semantic identity budget remains `AUTOMATIC_PREFIX_MAX_BYTES`;
- reserve a fixed minimum payload budget for the first user turn before consuming system/developer text;
- system/developer fields may use the unreserved portion;
- the first user turn then consumes the reserved amount plus any remaining unused capacity;
- all strings stay UTF-8 safe and bounded.

The exact split is an implementation detail. Favor enough first-user content to distinguish ordinary conversations over maximizing common system-prompt coverage. A 1–2 KiB guaranteed first-user allocation within the existing 4 KiB total is reasonable.

Alternative implementations are acceptable if they prove the invariant with tests and remain simple.

### Maintain stable-session behavior

Continue to use only:

- system/developer text;
- the first user turn;
- the canonical client surface.

Do not include later turns, tool schemas/results, generation settings, client IP, API key, request IDs, timestamps, or media bytes.

Responses continues to require the explicit session header for cross-request stickiness.

### No raw identity state

Store/hash only bounded derived bytes as today. Do not persist or log the raw first user/system text.

### Tests

Add deterministic regression cases:

1. **Long first-user text:** two >4 KiB first-user prompts with different beginnings/endings produce different identities rather than the same surface-only digest.
2. **Large common system prompt:** two requests with the same very large system/developer prefix but different first user turns produce different identities.
3. **Growing transcript:** later turns continue not to affect an existing automatic identity.
4. **UTF-8 boundary:** large multibyte user/system text remains valid and deterministic.
5. **Bound enforcement:** the hashing helper never materializes an unbounded concatenated prefix.
6. **No first user:** existing conservative `None` behavior remains where no trustworthy first-user identity can be formed.

If useful, expose a private/internal helper that produces bounded framed identity fields so these properties can be tested without inspecting hashes only. Do not make it public API.

---

## 3. Treat non-2xx selector results as availability failure, not repairable output

Primary files expected:

- `src/eggpool/model_router/selector.py`
- `tests/unit/test_model_router_selector.py`
- potentially focused proxy/integration tests using the existing fake coordinator/provider fixtures

### First selector attempt

Immediately after `RequestCoordinator.execute()` returns:

```text
2xx -> parse route ID
non-2xx -> selector unavailable -> configured default
```

Do not parse an upstream error body for route IDs.

Do not launch the repair inference after a non-2xx first selector response.

`fallback_reason = "unavailable"` is the preferred existing bounded taxonomy for this case. Do not add status-code cardinality to metrics.

### Repair attempt

If the first response was 2xx but invalid and repair is enabled:

- issue exactly one repair request with the preserved semantic context from Section 1;
- if repair response is 2xx, parse it normally;
- if repair response is non-2xx, stop and use the configured default;
- classify terminal non-2xx repair failure as `unavailable`, not `repair_failed`;
- reserve `repair_failed` for a successful repair response whose semantic text is still invalid.

This preserves a useful distinction:

```text
invalid_output / repair_failed = classifier followed the HTTP contract but not the route-ID contract
unavailable                    = selector concrete inference did not complete successfully
```

### Do not alter coordinator health semantics

The coordinator already owns retryability, account/provider failover, backoff, health mutation, attempt persistence, and error shaping. The semantic selector should only interpret the final prepared response class.

Do not add separate retry logic around non-2xx responses.

### Tests

Add explicit cases for at least:

- first selector returns 400 -> default, one selector call, no repair;
- first selector returns 429 -> default, one selector call, no repair;
- first selector returns 500/503 -> default, one selector call, no repair;
- first selector returns 200 invalid, repair returns 500 -> default with `unavailable`, two calls total;
- first selector returns 200 invalid, repair returns 200 invalid -> default with `repair_failed`;
- first selector returns 200 invalid, repair returns 200 valid -> selected route;
- timeout and `asyncio.CancelledError` behavior remain unchanged.

Where current fake coordinator fixtures always return status 200, extend them minimally rather than introducing a parallel test harness.

---

## 4. Cross-surface and sticky-routing regression closure

The three fixes above are mostly internal, but semantic routing is invoked by all supported client surfaces. Re-run and, where needed, extend integration coverage for:

- OpenAI Chat Completions;
- stateless OpenAI Responses;
- Anthropic Messages.

Required regression assertions:

- feature-off concrete requests still never invoke selector or affinity work;
- sticky virtual requests still select once and reuse the same concrete target;
- `sticky = false` still invokes selection each request;
- explicit `X-EggPool-Route-Session` still hashes locally and is never forwarded upstream;
- Responses still requires explicit session identity for cross-request affinity;
- provider-qualified selector and route targets remain provider-qualified through normal concrete parsing;
- context/capability/transcoding validation still applies to the selected concrete target after semantic resolution;
- target failure after semantic selection does not invoke the selector again or switch to a different route;
- account/provider failover for the selected concrete target remains unchanged.

Do not move semantic affinity into the concrete router to make these tests easier.

---

## 5. Feature-off/resource discipline

No mandatory architectural change is required for the current empty process-local `ModelRouterAffinity` and `ModelRouterMetrics` objects. They are bounded/inert, create no background tasks, perform no DB work, and add negligible memory overhead.

Do **not** spend this corrective pass adding lazy lifecycle machinery solely to eliminate those empty objects unless the implementation is trivial and reduces code rather than adding branches/ownership complexity.

The important feature-off contract remains:

- empty registry exact miss;
- no prompt compilation;
- no selector request;
- no affinity identity/hash/lookup;
- no semantic metrics recording;
- no additional DB/network work;
- no changed concrete request semantics.

Preserve the sentinel feature-off tests in `tests/unit/test_proxy.py`.

---

## 6. Documentation consistency

The existing operator documentation is already broadly correct. Update docs only where the corrected implementation changes a statement materially.

Expected small updates, if needed:

- `docs/model-routing.md`: clarify that repair retries classification with the **same bounded semantic request context**, and that only successful-but-invalid selector responses are eligible for repair;
- architecture/development guidance if it currently implies every selector failure is repaired.

Do not rewrite the whole documentation set.

Keep the documented high-level contracts unchanged:

- selector failures fall back to configured default;
- selector child requests consume ordinary usage/accounting;
- affinity is process-local and bounded;
- no semantic failover after target submission;
- no nested routers.

---

## 7. Verification against the exact resulting head

The prior affinity phase recorded a successful full suite before the final client-integration commit. The current planning baseline `9b8815b...` has no attached GitHub status checks, so this corrective pass must produce fresh verification evidence for the **exact final commit**.

Run the repository's normal local gates, matching current project conventions:

```bash
ruff format --check src/ tests/ scripts/
ruff check src/ tests/ scripts/
pyright src/ scripts/
pytest tests/smoke/ -q --tb=short --maxfail=1
pytest
```

If the repository's current canonical commands differ, use the repo-defined equivalents and record the exact commands/results in this plan's closure evidence.

Before the full suite, run the focused model-router regression set for fast feedback, including at minimum:

```bash
pytest tests/unit/test_model_router_selector.py -q
pytest tests/unit/test_model_router_affinity.py -q
pytest tests/unit/test_model_router_metrics.py -q
pytest tests/unit/test_proxy.py -q
pytest tests/unit/test_api_models.py -q
```

Also run the existing rehash/runtime tests that cover process-owned affinity surviving unchanged router fingerprints and invalid candidate publication. Use existing test names/files rather than creating a new permanent CI matrix.

### Verification is not optional

Do not mark this plan complete solely because focused unit tests pass. The changes touch request classification, process-local affinity, and the shared proxy path; final closure requires the normal full suite on the exact final head.

If any unrelated pre-existing test is failing, record it precisely and determine whether it is reproducible on the planning baseline before declaring the corrective work complete.

---

## 8. Acceptance criteria

This corrective pass is complete only when all of the following are true:

1. Repair inference retains the same bounded semantic request context used by the initial classifier call.
2. Repair never includes the raw invalid selector output or unbounded client content.
3. A non-2xx initial selector response immediately resolves to configured default without a repair call.
4. A non-2xx repair response resolves to default as `unavailable`; `repair_failed` is reserved for 2xx-but-invalid repair output.
5. Parent cancellation still propagates and does not dispatch default work.
6. Automatic Chat/Messages affinity identities always incorporate bounded first-user content whenever an automatic identity is returned.
7. Very large common system prompts cannot cause distinct first-user conversations to collapse to one automatic affinity key.
8. Automatic affinity remains stable as later transcript turns are appended.
9. Responses stateless/session-header behavior remains unchanged.
10. Feature-off concrete requests continue to bypass semantic prompt/selector/affinity work.
11. Existing provider/account fairness, retry, health, quota, backoff, wire negotiation, and scorer contracts are unchanged.
12. No new mandatory dependency, DB migration, background task, external service, or CI matrix is introduced.
13. Focused model-router tests pass.
14. Ruff, Pyright, smoke tests, and the full repository test suite pass on the exact final commit.
15. Closure evidence is written into this plan with the final commit SHA and exact verification results.

---

## Suggested implementation order

1. Add failing selector-status and repair-context tests.
2. Correct repair prompt construction and selector status handling together.
3. Add failing long-prefix affinity identity tests.
4. Correct affinity byte budgeting/reservation.
5. Run focused selector/affinity/proxy tests.
6. Make only necessary documentation corrections.
7. Run formatting/type/smoke/full-suite gates.
8. Record closure evidence and final SHA in this plan.

This order keeps each defect independently observable and avoids mixing affinity changes with request-dispatch changes before tests characterize both.

---

## Explicit non-goals

Do not use this pass to add:

- semantic target failover after target submission;
- health/quota-aware route descriptions or route filtering;
- virtual-router nesting;
- persistent affinity in SQLite;
- cross-worker/distributed affinity;
- embeddings or vector similarity routing;
- selector temperature/reasoning configuration expansion;
- arbitrary classifier response parsing/fuzzy model-name recovery;
- a larger automatic-affinity cache;
- a new model-router database table;
- a new HTTP loopback path;
- benchmark/CI infrastructure beyond the existing repo gates.

The desired result is a smaller, more correct closure patch—not a broader routing platform.
