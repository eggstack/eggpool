# Plan 206: Coding-agent closure corrective policy

> **Status:** READY FOR IMPLEMENTATION
>
> **Parent:** Plan 203
>
> **Priority:** Conditional only
>
> **Scope:** define how to handle defects uncovered by the closure qualification without expanding the milestone into another broad architecture pass.

## Purpose

Plans 203–205 should be sufficient if the current implementation passes real-client qualification. This plan exists only to constrain corrective work if qualification reveals a genuine Eggpool defect.

Do **not** implement this plan proactively. Use it as the decision framework for failures found while executing Plan 204.

---

# Corrective rules

For every failed qualification case:

1. classify the failure;
2. determine whether Eggpool, the client, the provider, or the test harness owns it;
3. reproduce it deterministically where practical;
4. add a focused regression before or with the fix;
5. change the narrowest existing ownership boundary;
6. rerun the failed live case;
7. rerun focused and full quality gates before closure.

Do not use a live failure as justification for speculative redesign.

---

# Failure classes and preferred ownership

## Codex config/catalog schema drift

Preferred owner:

```text
rust/src/operations/integrations.rs
Codex renderer / managed lifecycle helpers
source-derived fixtures/tests
```

Do not modify routing or provider transport for a config-file schema change.

## OpenCode config schema/runtime drift

Preferred owner:

```text
OpenCode renderer/lifecycle integration
source-derived OpenCode fixture/tests
```

Keep provider-neutral capability facts stable unless the client change reveals a genuinely missing generic capability.

## Responses event/lifecycle incompatibility

Preferred owner:

```text
rust/src/wire/
existing Responses compatibility/conformance tests
```

Preserve native pass-through whenever possible. Do not normalize future/unknown native events merely to satisfy one observed client path.

## Tool-call identity or `tool_search` defect

Preferred owner:

```text
canonical tool semantics + translated encoder/decoder
codex_responses_compat
```

Keep reconstruction declaration-scoped. Do not classify by function name alone.

## Remote compaction defect

Preferred owner:

```text
compact admission/dispatch/result validation
codex_compaction_compat
```

Do not add translated summarization fallback or stateful conversation persistence as a quick fix.

## Managed config ownership/drift defect

Preferred owner:

```text
managed lifecycle merge/manifest code
operations_o005 / focused integration fixtures
```

Bias toward refusal rather than overwriting ambiguous user-owned content.

## `status`/readiness defect

Preferred owner:

```text
rust/src/operations/status.rs
shared readiness authority
status_command tests
```

Do not make `status` probe providers or mutate routing state to compensate for missing evidence.

## Provider-specific incompatibility

Preferred owner:

```text
provider-owned capability/config/transport boundary
```

Do not weaken the global canonical contract unless the behavior is actually generic across providers.

---

# Severity guidance

Use simple engineering severity for closure triage:

```text
BLOCKER
- current Codex/OpenCode cannot start from generated config
- text/tool loop cannot complete on an otherwise supported route
- managed config can destroy unrelated user configuration
- status/health observation mutates routing or leaks secrets

CORRECTIVE
- metadata/capability mismatch that causes client misbehavior
- optional `tool_search`/remote-compaction incompatibility on an advertised supported path
- status misclassification or exit-code defect

POLISH
- wording/spacing/documentation mismatch
- non-functional presentation issue
- qualification evidence formatting
```

A blocker/corrective defect prevents Plan 203 closure until resolved or explicitly reclassified as an unsupported/non-Eggpool limitation with evidence.

Polish does not justify reopening architecture.

---

# Regression requirement

Every Eggpool-owned blocker/corrective fix must leave deterministic coverage behind.

Preferred pattern:

```text
live failure
  -> minimal captured semantic fixture (secret-free)
  -> failing focused test
  -> implementation fix
  -> focused test green
  -> live qualification green
```

Do not commit full provider transcripts if a minimal semantic fixture is enough.

Redact credentials, prompts, account labels, cache keys, and private response content.

---

# Dependency/architecture guardrails

Corrective work must not add, merely for closure:

- Codex/OpenCode/Node as production dependencies;
- a new async runtime;
- server-side client plugin execution;
- persistent conversation storage;
- Responses WebSocket support unless current supported HTTP/SSE becomes impossible without it;
- a parallel health subsystem;
- active `status` probing;
- a separate client-specific model database independent of the shared projection.

If a real blocker appears to require one of these, stop treating it as a closure correction and write a new separately scoped roadmap/plan with explicit justification.

---

# Closure acceptance

This conditional plan is satisfied when either:

1. Plan 204 finds no Eggpool-owned blocker/corrective defects, so no implementation action is required; or
2. every discovered blocker/corrective defect is regression-tested, fixed narrowly, live-requalified, and reflected in Plan 205 closure evidence.

Do not create additional corrective plans for individual trivial fixes unless the fix is large enough to require its own handoff boundary.
