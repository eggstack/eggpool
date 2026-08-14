# Roadmap 122 — Post-Audit Correctness and SBC Simplification

Date: 2026-08-14
Status: ready
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Scope: bounded follow-up to completed Roadmap 113 and Plan 121
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Close the small set of meaningful issues that remain after EggPool's recent
SBC/hot-path reduction work without starting another open-ended optimization
program.

Current `main` already has the intended product shape: a local/LAN-oriented
multi-account LLM router, OpenAI Chat Completions ↔ Anthropic Messages
transcoding, bounded provider retry/failure isolation, SQLite-backed accounting,
low-wear SBC defaults, one-worker Granian runtime, minimal ordinary CI, and
optional heavy features disabled by default. Roadmap 113 and Plan 121 removed
most known duplicate request walks/copies and several optional subsystems.

The remaining work is narrower:

1. correct current OpenAI reasoning-effort semantics so unknown/new effort names
   cannot silently acquire the wrong Anthropic thinking budget;
2. remove the remaining reviewed full-request copy in cross-protocol recompute
   and reduce avoidable multimodal validation peak memory where this can be done
   without a new streaming framework;
3. resolve the globally suppressed `aiosqlite` closed-event-loop teardown
   warning instead of treating it as permanently harmless;
4. obtain one representative provider-backed SBC characterization using only
   existing tools;
5. decide whether EggPool truly requires durable in-flight
   request/reservation/attempt ownership across process death;
6. only if that decision proves the durability invariant is unnecessary,
   simplify it through a separately gated deletion plan;
7. further reduce retained test/planning maintenance surface after the behavior
   has settled, while leaving ordinary CI intact;
8. make the public OpenAI compatibility scope explicit: Chat Completions
   compatibility versus broader OpenAI API/Responses compatibility.

This roadmap is deliberately finite. It must not become a framework rewrite,
benchmark program, protocol-completeness project, database redesign, or another
closure-plan chain.

## Product constraints

EggPool is primarily intended for private/local/LAN deployment, including
Raspberry Pi/SBC-class hosts. Preserve the security and correctness properties
that matter for that environment:

- malformed client input or provider failures must not poison unrelated routing;
- provider/account failures remain scoped and recover without database reset;
- retries occur only where the existing pre-handoff contract permits them;
- one request cannot mutate canonical/prepared state used by another path;
- credentials/request content remain redacted;
- non-loopback exposure still requires the existing API-key protections;
- SQLite remains local and WAL-backed unless the durability decision explicitly
  removes a request-time write, not SQLite itself;
- rehash must preserve active-generation correctness;
- no production-grade distributed coordination, HA, tracing stack, or hardware CI
  is required.

## Governing constraints

1. Do not replace FastAPI, Granian, HTTPX/httpcore, aiosqlite, Pydantic, Click,
   SQLite, or the existing JSON abstraction.
2. Do not add a runtime dependency unless a child plan explicitly demonstrates
   that no existing/stdlib implementation can satisfy a correctness requirement;
   the expected outcome is **no new dependency**.
3. Do not add benchmark, soak, profiling-service, allocation-telemetry,
   scheduled-full-suite, hardware-CI, fuzzing, mutation-testing, or coverage-gate
   infrastructure.
4. Keep `.github/workflows/ci.yml` materially unchanged: one Python 3.11 job,
   Ruff format/check, Pyright, and the existing smoke suite.
5. Do not lower provider pool sizes, change SQLite pragmas, add SQLite
   connections, or tune OS/kernel parameters from generic performance advice.
6. Do not resurrect deleted DNS cache, synthetic cache insertion, compression
   tuner, static-prefix compression, or other Roadmap 113 deletions.
7. Do not create new modes solely to preserve both old and simplified
   architectures. Prefer one product decision and deletion where safe.
8. Child plans record their own completion evidence. Do not create a new
   "closure plan" merely to rerun acceptance criteria.
9. Historical completed plans remain historical records; reduction work targets
   active maintenance/navigation surface, not cosmetic mass deletion.
10. Stop when the listed child-plan acceptance criteria are met.

## Child plans and ordering

### Plan 123 — Reasoning-Effort and Thinking-Semantics Correction

Highest-priority client-visible correctness work.

Correct the hard-coded three-effort fallback so `none`, `xhigh`, `max`, or
future provider-recognized values cannot silently become an unrelated medium
Anthropic budget. Provider/model capability facts remain authoritative and must
be verified against current official API documentation at implementation time.

Can execute first and independently.

### Plan 124 — Cross-Protocol Request-Memory Reduction

Remove the remaining reviewed `provider_payload_copy()`/recursive `deepcopy`
from protocol-transcode recompute if caller/ownership audit confirms the
transcoder already creates its own output graph. Also examine base64 image/PDF
validation for avoidable simultaneous encoded+decoded retention.

Do not build a streaming JSON/base64 framework. If a safe bounded validator
cannot materially reduce memory with simple code, retain current validation and
record why.

Depends on Plan 123 only where the same transcoder code is touched; otherwise it
may execute in parallel.

### Plan 125 — aiosqlite Teardown and Warning-Suppression Correction

Reproduce the Pytest worker-thread/closed-loop warning, identify the actual
resource ownership/teardown race, fix lifecycle ordering or fixture ownership,
and remove the global warning suppression plus stale `bugs.md` reference when
safe.

This is test/runtime-lifecycle correctness work, not a database architecture
project.

### Plan 126 — Provider-Backed SBC Characterization

Run one short real workload on a representative SBC with configured provider
accounts after Plans 123–125. Use existing `runtime-status`, standard OS tools,
and simple client calls only. Characterize native, native-streaming,
cross-protocol, and small concurrent-stream workloads. Record observations, not
performance gates.

No permanent harness or CI artifact.

### Plan 127 — Durable In-Flight Lifecycle Necessity Decision

Audit the product value and write/complexity cost of creating durable pending
request, attempt, and reservation ownership before upstream dispatch. Produce a
binary decision:

- **retain** because crash-perfect/accounting invariants are real product
  requirements; or
- **simplify** because process death may legitimately terminate in-flight work
  and completed accounting is sufficient for the intended local appliance.

Do not partially refactor durability in this plan.

### Plan 128 — Conditional Durable Lifecycle Simplification

**Blocked unless Plan 127 records `decision: simplify`.**

If unblocked, remove only durability machinery made unnecessary by the explicit
new process-death contract. Preserve completed usage/accounting, provider
suppression, startup DB integrity, rehash correctness, and ordinary request
failure isolation.

If Plan 127 says retain, mark Plan 128 `not applicable` without implementation.

### Plan 129 — Retained Test and Planning Surface Reduction

After behavior settles, reduce duplicated historical tests/fixtures and active
planning-navigation burden. Protect regressions for routing poisoning,
streaming handoff/EOF/cancellation, transcode semantics, database ambiguity,
rehash, auth/redaction/body limits, and any lifecycle retained after Plan 127/128.

Do not weaken ordinary CI or chase a numerical test-count target.

### Plan 130 — OpenAI Compatibility Scope Decision and Documentation

Determine whether the supported product contract is:

- OpenAI **Chat Completions-compatible** routing/proxying plus Anthropic Messages;
  or
- broader current OpenAI API compatibility including `/v1/responses`.

Default to truthful narrowing of claims unless repository consumers/docs/tests
show broader compatibility is an actual product requirement. Do not implement a
full Responses translator inside this plan. If broader scope is selected,
record a future milestone with a bounded requirements inventory rather than
starting implementation opportunistically.

## Sequencing

Recommended order:

```text
123 reasoning correctness
 ├─> 124 request-memory cleanup
 └─> 125 teardown correction
          |
          v
126 real SBC characterization
          |
          v
127 durable-lifecycle decision
          |
          +--> retain ------> 129 reduction
          |
          +--> simplify --> 128 deletion --> 129 reduction

130 API-scope decision may run after 123 and before final documentation cleanup.
```

Plan 126 should characterize the corrected request path, not an obsolete
pre-correction state. Plan 127 should use measured write/resource observations
from Plan 126 when available but must not treat a single hardware run as proof by
itself.

## Verification philosophy

Use three layers only:

1. focused deterministic tests around the changed invariant;
2. the ordinary repository gate;
3. short manual/live checks only where they add information unavailable from
   deterministic tests.

Ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

A full retained-suite run is optional/manual unless a child plan changes such a
broad shared fixture that focused selection is not credible. Do not turn that
exception into a permanent gate.

## Handoff rules for GPT-5.6 Luna

For each child plan:

1. read this roadmap, that child plan, `AGENTS.md`, and the owning source/tests;
2. inspect current production callers before editing; do not implement from plan
   prose against stale code;
3. keep commits confined to the child-plan boundary;
4. prefer deletion and reuse of existing abstractions over adding wrappers;
5. preserve existing public behavior unless the child plan explicitly changes
   the product contract;
6. add the smallest behavioral regression that would have caught the defect;
7. run focused verification, then ordinary gate;
8. append implementation SHA, exact commands/results, deviations, and final
   disposition to the same plan file;
9. stop. Do not create a follow-up closure plan unless a genuinely new defect is
   discovered outside the current acceptance criteria.

## Roadmap acceptance criteria

- [ ] Plan 123 prevents unsupported/new reasoning effort names from silently
  acquiring semantically unrelated Anthropic thinking behavior.
- [ ] Plan 124 removes the remaining reviewed full-request protocol-transcode
  copy when safe, or records concrete ownership evidence for retaining it.
- [ ] Plan 124 addresses avoidable multimodal validation peak memory without a
  new parsing/streaming framework, or records a justified no-change result.
- [ ] Plan 125 removes the global aiosqlite closed-loop warning suppression after
  fixing/reproducing teardown ownership, or documents a narrowly scoped upstream
  limitation with no stale repository reference.
- [ ] Plan 126 records real provider-backed SBC observations or, only if no
  suitable hardware/credentials exist at execution time, explicitly records the
  unavailable dimensions without fabricating results.
- [ ] Plan 127 records an explicit retain/simplify decision for durable in-flight
  ownership with code/write-path evidence.
- [ ] Plan 128 executes only if Plan 127 selects simplify; otherwise it is marked
  not applicable.
- [ ] Plan 129 reduces maintenance surface while preserving protected behavioral
  regressions and current CI shape.
- [ ] Plan 130 makes OpenAI compatibility claims precise and does not silently
  expand protocol scope.
- [ ] No new core framework, runtime dependency, permanent performance harness,
  or production-grade deployment machinery is introduced.
- [ ] Ordinary CI remains one Python 3.11 Ruff/Pyright/smoke job.
- [ ] Each child plan carries its own closure evidence; no automatic closure-plan
  chain is created.

## Roadmap rejection conditions

Reject implementation of this roadmap if it:

- turns reasoning-effort correction into a generic provider semantics engine;
- introduces another request payload/COW framework;
- replaces SQLite/aiosqlite or changes DB pragmas without direct evidence;
- adds hardware or performance gates to CI;
- uses a one-off SBC measurement as a numerical SLA;
- creates "legacy" and "simplified" durability modes instead of making one
  explicit product decision;
- deletes durable lifecycle machinery before Plan 127 authorizes it;
- adds `/v1/responses` opportunistically without Plan 130 selecting that scope;
- weakens provider failure isolation, stream handoff rules, rehash safety,
  authentication/redaction, or database ambiguity handling that remains in the
  selected product contract;
- creates another roadmap/closure chain solely because these plans completed.
