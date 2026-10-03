# Provider Profile Metadata Planning Reconciliation C001 — Closed-Roadmap and Source-Truth Reconciliation

Status: active

Repository baseline: `43c987ea458bd563d5108fd8051ad31185704bb0`

Source roadmap:

- `plans/subsystems/provider-profile-metadata-planning-reconciliation-corrective-roadmap.md`

Primary class: polish / documentation.

## 1. Objective

Bring EggPool's provider-profile metadata planning/documentation control surfaces into exact agreement with accepted M001 closure: the workstream is closed with no successor, Together `.ai` is canonical, `.xyz` is a legacy alias, OpenCode Go remains `/zen/go/v1`, and no production template change is pending.

## 2. Why this is needed

The technical work is complete, but current docs contain lifecycle/source-truth contradictions:

- predecessor roadmap top-level status is still active;
- registry active table still calls the predecessor active despite M001 being recently closed with no successor;
- roadmap/implementation plan retain the planning-time assumption that Together `.ai` was stale;
- later closure evidence correctly refutes that assumption.

The correction is documentation-only and must not reopen M001 technically.

## 3. Scope

In scope:

- `plans/subsystems/provider-profile-metadata-corrective-roadmap.md`;
- `plans/implementation/provider-profile-metadata/001-provider-template-endpoint-and-source-reconciliation.md`;
- `plans/registry.md`;
- architecture docs only if contradictory current text is found;
- new closure record for this reconciliation.

Out of scope:

- `rust/assets/providers/_templates.toml`;
- Rust/tests/config;
- provider endpoint changes;
- re-audit of all templates;
- resolution of deferred Fireworks/Alibaba uncertainties.

## 4. Required edits

### A. Lifecycle truth

- predecessor roadmap: `Status: closed`;
- explicitly state M001 is terminal for this roadmap and no successor is registered;
- registry predecessor row: closed/no successor, not active;
- preserve M001 in Recently closed.

### B. Together premise supersession

Current-authority roadmap text must state:

- canonical Together prefix: `https://api.together.ai/v1`;
- legacy alias: `https://api.together.xyz/v1`;
- the plan-time proposed `.xyz` correction was rejected by execution-time first-party evidence;
- zero template diff was the correct M001 outcome.

Preserve enough historical context to explain why the plan originally proposed a correction.

### C. Historical implementation-plan annotation

Immediately after the M001 implementation-plan status/header, add a concise post-closure note:

- planning-time Together statements are historical;
- closure `plans/closure/provider-profile-metadata/001-status.md` is authoritative;
- `.ai` was retained;
- no endpoint correction landed.

Do not rewrite the full closed plan.

### D. Registry chronology

Reconcile the original registry paragraph that opens provider-profile metadata M001 so it no longer leaves the false `.ai`-is-stale statement as current truth. It may retain chronology by explicitly saying that premise was later refuted at closure.

## 5. Verification

Required:

```bash
git diff --check
```

Plus explicit inspection proving:

- roadmap top-level status is closed;
- no active/no-successor contradiction remains in registry;
- current-authority text consistently uses `.ai` as canonical;
- implementation-plan supersession note exists;
- M001 closure file and `_templates.toml` have zero diff;
- no non-Markdown file changed.

## 6. Acceptance criteria

- planning lifecycle is internally consistent;
- accepted source truth is obvious without reading contradictory paragraphs in sequence;
- historical intent remains traceable;
- predecessor technical closure remains immutable;
- no runtime/template behavior changes;
- closure record is added and registry reconciled.

## 7. Stop conditions

Stop if any non-documentation change becomes necessary or if new provider evidence changes the accepted M001 technical disposition. Register that separately.

## 8. Handoff note

Do not use this plan to refresh provider metadata. It exists solely to make the already-accepted M001 outcome and lifecycle unambiguous.
