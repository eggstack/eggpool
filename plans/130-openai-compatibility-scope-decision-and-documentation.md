# Plan 130 — OpenAI Compatibility Scope Decision and Documentation

Date: 2026-08-14
Status: ready
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Depends on: Plan 123 semantics correction before final docs
Priority: P2 product-scope truthfulness
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Make EggPool's supported OpenAI compatibility contract explicit and ensure public
claims match implementation.

EggPool currently exposes OpenAI-compatible Chat Completions behavior and bridges
OpenAI Chat Completions ↔ Anthropic Messages. The package/README may also use the
broader phrase "OpenAI-compatible endpoint," which can reasonably be read as
support for more of the current OpenAI API surface, including `/v1/responses`.
Modern OpenAI reasoning/tool workflows increasingly use the Responses API, but
adding a complete Responses↔Anthropic translator would be a major product
milestone, not a documentation correction.

This plan therefore makes a product-scope decision and updates documentation.
It does **not** opportunistically implement `/v1/responses`.

The expected default for EggPool's local/SBC scope is truthful narrowing to
"OpenAI Chat Completions-compatible" unless repository consumers/tests/docs show
that broader current OpenAI API compatibility is already an explicit product
requirement.

## Governing constraints

1. Verify current official OpenAI API guidance/endpoints at implementation time.
   Provider API scope changes over time.
2. Inspect actual EggPool routes, clients, docs, examples, issues/tests, and known
   project consumers before deciding.
3. Do not infer that the phrase "OpenAI-compatible" obligates full OpenAI API
   parity if current behavior and intended clients only require Chat Completions.
4. Do not add `/v1/responses` or a new translator in this plan.
5. Do not add embeddings/audio/images/batches/fine-tuning/files/assistants or
   unrelated OpenAI endpoints for completeness.
6. Preserve current `/v1/chat/completions`, `/v1/models`, Anthropic Messages, and
   current transcoding behavior.
7. Preserve Plan 123's corrected reasoning semantics.
8. Do not add a compatibility framework, protocol registry, SDK dependency, DB
   migration, CI job, or generated OpenAPI parity suite.
9. Prefer precise claims over broad marketing language.
10. If broader Responses compatibility is selected, produce a bounded future
    requirements record only; do not start implementation in this plan.

## Workstream A — Inventory actual public API surface

Inspect current FastAPI routes and docs for:

- `/v1/chat/completions`;
- `/v1/models`;
- Anthropic `/v1/messages` or equivalent exposed route;
- health/readiness/stats/dashboard/operator endpoints;
- any existing `/v1/responses` route or compatibility stub;
- headers/error formats advertised as OpenAI-compatible;
- streaming SSE compatibility;
- tool/reasoning/structured-output/cache behavior documented for Chat
  Completions.

Record a concise current-surface table in this plan's closure, not a permanent
API inventory artifact.

## Workstream B — Inventory intended consumers

Search repository docs/examples/tests and current project guidance for named or
implied clients:

- OpenCode or other Chat-Completions clients;
- OpenAI Python/JS SDK use through `chat.completions`;
- agent harnesses that call `/v1/responses`;
- direct Anthropic Messages clients;
- generic "OpenAI base URL" consumers;
- code examples that instantiate a client and assume broader endpoint parity.

Where repository evidence is ambiguous, favor the smallest truthful supported
contract. Do not invent external requirements.

If issue/commit history is consulted, distinguish explicit requested features
from incidental wording.

## Workstream C — Verify current official OpenAI distinction

At implementation time, consult official OpenAI documentation only for the
following facts:

- current status of Chat Completions;
- current role/recommendation of Responses API for reasoning/tools;
- major request/response semantic differences relevant to EggPool;
- whether current SDK "OpenAI-compatible base URL" expectations commonly route
  through distinct endpoint paths.

Record verification date and official links in this plan closure or active docs.
Do not copy large provider documentation excerpts.

This is a product-scope check, not a parity research project.

## Workstream D — Select one product contract

Record exactly one of:

```text
openai_scope: chat_completions
```

or

```text
openai_scope: broader_responses_required
```

### Select `chat_completions` when

- primary supported clients use `/v1/chat/completions`;
- no current supported workflow requires `/v1/responses`;
- current OpenAI↔Anthropic translator is intentionally message/chat oriented;
- adding Responses would materially expand complexity beyond local proxy goals;
- precise documentation can eliminate ambiguity without feature loss.

### Select `broader_responses_required` only when

- a current intended/supported EggPool consumer actually requires
  `/v1/responses` or equivalent modern OpenAI semantics;
- the requirement is explicit enough to justify a future implementation
  milestone;
- merely documenting Chat Completions would misrepresent intended product goals.

Ambiguous evidence defaults to `chat_completions`.

## Workstream E — Documentation changes for `chat_completions`

If selected, update active public/technical wording consistently:

- `pyproject.toml` project description if it says only "OpenAI-compatible
  endpoint" without qualification;
- README introduction/features/API examples;
- provider/transcoder docs;
- architecture docs;
- packaged config comments if they imply generic OpenAI API parity;
- CLI/help text only if currently broad;
- `AGENTS.md` protocol compatibility summary.

Preferred wording should be close to:

- "OpenAI Chat Completions-compatible endpoint";
- "bridges OpenAI Chat Completions and Anthropic Messages";
- "does not currently claim full OpenAI API/Responses parity" where useful.

Do not overfill docs with disclaimers. One clear compatibility statement near API
usage plus precise protocol sections is sufficient.

Preserve use of "OpenAI-compatible provider" where it specifically means an
upstream implementing the Chat Completions-compatible contract and context makes
that clear.

## Workstream F — Requirements record if Responses is genuinely required

If `openai_scope: broader_responses_required`, do **not** implement it here.
Instead append a compact bounded requirements section identifying only the
minimum future milestone:

### Required discovery

- exact `/v1/responses` request shapes used by intended clients;
- response object and streaming event types required;
- function/tool call representation;
- reasoning summaries/content fields required;
- multi-turn `previous_response_id`/conversation state expectations;
- file/image input/output forms actually needed;
- error/status compatibility;
- usage/cache accounting fields;
- mapping boundaries to Anthropic Messages;
- which semantics are unrepresentable/lossy;
- whether EggPool should proxy native Responses upstream unchanged when provider
  supports it versus transcode only when necessary.

### Future milestone constraints

A future plan must:

- start with native pass-through where possible;
- avoid implementing every Responses feature for completeness;
- support only actual intended client workflows;
- preserve stateless proxy architecture unless explicit response-state semantics
  require otherwise;
- avoid storing provider conversation state in EggPool merely to emulate an API;
- define a bounded loss policy rather than pretending Responses and Messages are
  fully isomorphic;
- include streaming correctness before claiming support.

Do not create the future implementation plan automatically unless repository
planning convention/user direction requires it after this decision. This plan's
job is to establish scope truthfully.

## Workstream G — Tests for public compatibility claims

Do not add tests that assert README prose verbatim.

If only documentation wording changes, existing route/transcode tests are enough.
Verify current supported endpoints remain unchanged.

If a small route/documentation constant changes in code/help text, add at most one
focused CLI/API contract test if needed.

Protect:

- `/v1/chat/completions` native request;
- streaming Chat Completions;
- `/v1/models`;
- Anthropic Messages route;
- OpenAI↔Anthropic body/stream translation;
- Plan 123 reasoning semantics.

Do not add expected-failure `/v1/responses` suites solely to document absence.

## Workstream H — Packaging and examples

Ensure install/package metadata does not overclaim protocol scope.

Check:

- PyPI description sourced from `pyproject.toml`/README;
- code examples using OpenAI SDK base URL;
- model/provider examples;
- badges/keywords only if they imply a specific API surface.

Keywords like `openai` remain appropriate; this is wording precision, not
removal of discoverability.

## Verification

Required checks if docs/metadata only:

```bash
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

If Python/config code changes, run ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

No new API compatibility CI matrix.

## Explicit acceptance criteria

- [ ] Actual EggPool public routes/protocol surface is inventoried.
- [ ] Intended consumers are searched for in current repository evidence.
- [ ] Current official OpenAI Chat Completions/Responses distinction is verified
  at implementation time and verification date/source is recorded.
- [ ] Closure records exactly one `openai_scope: chat_completions` or
  `openai_scope: broader_responses_required`.
- [ ] Ambiguous evidence defaults to the narrower truthful Chat Completions scope.
- [ ] If Chat Completions scope is selected, package/README/docs/AGENTS wording is
  precise and no longer implies full OpenAI API parity.
- [ ] If broader Responses scope is selected, this plan records only bounded
  future requirements and does not implement `/v1/responses`.
- [ ] Existing Chat Completions, Models, Anthropic Messages, transcoding,
  streaming, and Plan 123 reasoning behavior remain unchanged.
- [ ] No embeddings/audio/images/batches/assistants/files/fine-tuning or unrelated
  endpoint work is introduced.
- [ ] No protocol registry/framework, SDK dependency, DB migration, or CI
  expansion is introduced.
- [ ] Relevant config/ordinary checks pass for any code touched.
- [ ] Decision, evidence, changed wording, and exact verification are appended to
  this plan; no separate closure plan is created.

## Rejection conditions

Reject implementation if it:

- interprets "OpenAI-compatible" as a requirement for every OpenAI endpoint;
- starts implementing `/v1/responses` before the scope decision;
- adds a stub endpoint that falsely signals support;
- adds generic protocol abstraction solely to prepare for hypothetical parity;
- removes current Chat Completions/Anthropic functionality while narrowing docs;
- adds unrelated OpenAI features for completeness;
- creates a large parity test matrix or new CI job;
- leaves package metadata broad while docs quietly narrow the actual contract.

## Handoff sequence

1. Read Roadmap 122, completed Plan 123, this plan, README, pyproject, API routes,
   transcoder docs, `AGENTS.md`, and representative client examples/tests.
2. Inventory actual endpoints and intended consumers before deciding wording.
3. Verify current official OpenAI Chat Completions/Responses guidance.
4. Apply the decision criteria; default to Chat Completions when evidence is
   ambiguous.
5. Update active docs/metadata consistently or record bounded future Responses
   requirements if genuinely required.
6. Run relevant config/ordinary checks.
7. Append decision/evidence/verification to this file and stop.
