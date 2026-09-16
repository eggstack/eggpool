# Plan 208: Plan-status and evidence closure

> **Status:** complete
>
> **Parent:** Plan 206
>
> **Renamed:** 2026-09-16 from `plans/205-plan-status-and-evidence-closure.md`
> to resolve duplicate numbering with the per-plan closure record
> `plans/205-status-command-and-provider-health-summary-closure.md` (which
> closes Plan 202). Executed together with Plans 206–207.
>
> **Baseline:** Eggpool `main` after Plans 206–207 handoff
>
> **Priority:** Documentation/closure hygiene
>
> **Scope:** close stale planning metadata and attach implementation/qualification evidence for Plans 198–202 once Plan 206 qualification is complete, without rewriting historical rationale or reopening completed implementation work.

## Purpose

The implementation for Plans 199–202 has already landed, but Plans 198–202 still contain planning-era status text such as `READY FOR IMPLEMENTATION`. That creates an avoidable handoff hazard: future agents may treat already-landed work as pending and duplicate or conflict with the current implementation.

This plan isolates the documentation/evidence cleanup from the live-client qualification in Plans 203–204. It should execute only after the relevant qualification evidence exists.

---

# 1. Review repository closure convention

Before editing any historical plan, inspect recently completed plans and follow the repository's established convention.

Preferred behavior:

- preserve original scope/rationale;
- update the top status field if that is established practice;
- append a closure/evidence section rather than rewriting the original implementation instructions;
- cite actual implementation commits and tests;
- distinguish deterministic CI evidence from live/manual qualification evidence;
- retain explicit intentional deferrals.

Do not delete or substantially rewrite historical design reasoning solely because implementation is complete.

---

# 2. Close Plan 199 — Responses remote compaction

Attach at minimum:

```text
Implementation commit: 94b710e6d1b7fc82cb4c3ff5765441e05a008b33
Primary focused target: codex_compaction_compat
CI result: success on implementation commit
```

Record the implemented boundary accurately:

- `/v1/responses/compact` is bounded and finite-only;
- only natively compact-capable Responses routes participate;
- no translated summarization fallback;
- no persisted conversation/response state;
- v2 trigger semantics remain capability-gated/native-only.

After Plan 203/204, add the live/current-client compaction qualification result or state that the tested custom Codex path used local compaction and therefore remote compaction remained an optional provider capability.

---

# 3. Close Plan 200 — Agent catalog/config lifecycle

Attach at minimum:

```text
Implementation commit: 7499c15eec977e3c0bbce1481b247e3f50e36fc4
CI result: success on implementation commit
```

Record:

- provider-neutral `AgentModelProjection`/capability layer;
- conservative alias aggregation;
- generated Codex `model_catalog_json`;
- Responses-capable OpenCode renderer;
- managed `--apply`/`--sync`/`--check`/`--remove`/`--dry-run` lifecycle;
- standard `/v1/models` unchanged;
- no Codex/OpenCode production dependency added.

Append current Codex/OpenCode live qualification evidence from Plan 204 before marking fully closed.

---

# 4. Close Plan 201 — Deferred tool compatibility

Attach at minimum:

```text
Implementation commit: 27890a19f9c78099027e383b73ada0680ee3efb6
Primary focused target: codex_responses_compat
CI result: success on implementation commit
```

Record:

- client-executed `tool_search` represented as deferred search;
- function-style wrapper only where needed;
- declaration-scoped reconstruction;
- ordinary function named `tool_search` remains ordinary;
- hosted/server search stays native-only;
- Eggpool never becomes the tool executor.

If live `tool_search` cannot be forced reliably in current Codex, explicitly record deterministic conformance as the closure authority and explain the live limitation rather than leaving ambiguous TODO wording.

---

# 5. Close Plan 202 — `eggpool status`

Attach at minimum:

```text
Implementation commit: 3dc9ece9d713a49f56c9dd2f7aeba9b0c04e68e1
Primary focused targets: status_command and operations::status
CI result: success on implementation commit
```

Record:

- top-level `eggpool status [--json]`;
- authenticated `/api/status`;
- shared readiness evaluation with `readyz`;
- one provider row per configured provider;
- `ready`/`degraded`/`unavailable`/`disabled`/`unknown` provider states;
- observation evidence prevents freshly registered providers being falsely labeled ready;
- offline fallback and documented exit semantics;
- no outbound probes, quota usage, or health mutation;
- bounded/redacted output.

Add Plan 204 healthy/degraded/unready/unreachable qualification evidence before marking fully closed.

---

# 6. Close Plan 198 umbrella roadmap

Plan 198 should close last.

Its closure section should summarize the final state rather than duplicate every child plan.

Required statements:

1. Plans 199–202 implemented and individually evidenced.
2. Current Codex managed provider/catalog path live-qualified.
3. Current OpenCode managed provider path live-qualified.
4. Real text and ordinary tool loops qualified through Eggpool.
5. Current compaction behavior identified and compatible with Eggpool's stateless HTTP/SSE boundary.
6. `eggpool status` operator behavior qualified.
7. Standard `/v1/models` retained.
8. No conversation persistence, WebSocket Responses, OpenCodex-private backend parity, or server-side tool execution was introduced.
9. Any remaining limitations are explicit deferrals, not unfinished acceptance criteria.

Mark Plan 198 closed only if Plan 203 acceptance criteria are satisfied.

---

# 7. Update related tracking/docs only where stale

Review for stale statements such as:

```text
Codex requires an explicit model because model discovery is unavailable
rich Codex model discovery is deferred
status command is planned
remote compaction is missing
OpenCode uses a generic Chat Completions compatibility runtime
```

Likely review targets:

- `README.md`;
- `docs/agent-configuration.md`;
- `docs/codex-compatibility-smoke.md`;
- `docs/stateless-responses.md`;
- architecture integration/request-lifecycle/transcoder/health/deployment docs;
- `AGENTS.md` and `.opencode/skills/` only if their current guidance contradicts the final qualified behavior.

Do not perform broad prose churn. Change only stale or misleading statements.

---

# 8. Final evidence block format

Use a compact consistent evidence block where repository convention permits, for example:

```markdown
## Closure evidence

- Implemented: `<commit>` — `<summary>`
- Focused tests: `<targets>`
- CI: `<run/commit>` — success
- Live qualification: `<client version>` — PASS / NOT LIVE EXERCISABLE WITH REASON
- Intentional deferrals: `<short list>`
```

Avoid embedding raw terminal logs or credentials.

---

# Acceptance criteria

This plan is complete when:

1. Plans 199–202 no longer advertise implemented work as `READY FOR IMPLEMENTATION`;
2. each child plan contains actual implementation/test/CI evidence;
3. live qualification evidence from Plans 203–204 is attached where applicable;
4. Plan 198 is marked closed only after its child and live criteria are satisfied;
5. historical rationale remains intact;
6. no stale documentation tells users that implemented Codex/OpenCode/status/compaction features are still missing;
7. intentional deferrals are clearly distinguished from unfinished work;
8. no source code changes are made solely for plan-status cleanup unless qualification uncovered a real defect.

---

# Handoff note

Execute this after the live qualification matrix, not before. The objective is to leave the planning tree truthful and unambiguous for the next agent: implemented work should read as implemented, qualified work should carry evidence, and deferred work should be explicitly deferred rather than looking accidentally incomplete.

---

## Closure evidence (2026-09-16)

Executed with Plans 206–207.

- Renamed duplicate-numbered active plans to next free numbers: `203-...-qualification`
  → `206-...`, `204-...-matrix` → `207-...`, `205-...-evidence` → `208-...`.
  Historical complete records `203-agent-...`, `204-codex-...`,
  `205-status-...` left untouched (append-only).
- Updated statuses: Plans 198–202 and 206–208 now read `complete`; original
  scope/rationale preserved, evidence appended (not rewritten).
- Child evidence: Plan 199 (`94b710e6`), Plan 200 (`7499c15e` + Plan 206 catalog
  fix), Plan 201 (`27890a19`), Plan 202 (`3dc9ece9`); live qualification from
  Plan 206 attached where applicable (Codex `0.154.0`, OpenCode `1.18.30`).
- Docs: `architecture/deep-dive-integrations.md` (required catalog fields),
  `docs/codex-compatibility-smoke.md` (isolated qualification + versions).
  Verified no stale `Codex requires explicit model` / `rich discovery deferred` /
  `status planned` / `OpenCode generic runtime` statements remain in `README.md`,
  `docs/agent-configuration.md`, `docs/stateless-responses.md`,
  `architecture/overview.md`; `AGENTS.md` and skills unchanged (no new module
  boundary; existing guidance accurate).
- Acceptance mapping: 1 done (all `complete`); 2 done (commits + focused targets
  cited); 3 done (Plan 206 evidence); 4 done (Plan 198 closed last with full
  criteria); 5 done (append-only); 6 done (reviewed); 7 done (deferrals explicit);
  8 satisfied (one narrow renderer fix from real qualification defect, with
  regression test — permitted corrective loop).
