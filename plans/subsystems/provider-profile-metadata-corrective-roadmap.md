# Provider Profile Metadata Corrective Roadmap

Status: closed

Repository baseline reviewed: `c17a55218b2810791fcf6f3136b8805becfa27c1`

Long-term references:

- `plans/000-long-term-specification.md` — provider configuration must remain deterministic, secret-safe, and compatible with the selected provider protocol.
- `plans/001-terminology-and-domain-model.md` — provider identity, wire surface, and model-catalog authority.
- `plans/002-long-term-roadmap.md` — sustaining provider/catalog correctness without widening runtime ownership.
- `plans/003-planning-process.md` — evidence-backed corrective planning and closure.

Related ADRs:

- None required. This corrective updates bundled secret-free provider metadata and verification evidence. It does not change provider/account routing, credential storage, persistence schema, public HTTP APIs, retry ownership, or wire-kernel architecture.

## 1. Purpose and ownership boundary

This workstream corrects and qualifies EggPool's bundled provider-template facts in
`rust/assets/providers/_templates.toml`.

The templates own secret-free bootstrap facts such as:

- provider ID/display name;
- canonical base URL;
- supported protocol families/wire surfaces;
- endpoint-specific authentication/header shape;
- model-discovery path/shape;
- verification model/protocol;
- conservative static capability hints.

They do not own live provider availability, account credentials, provider health, routing preference, quota, pricing freshness, or model-selection policy.

Current provider documentation is the external authority for endpoint/auth/protocol facts. CodeGG is a sibling consumer with overlapping metadata and useful comparison evidence, but neither repository may be treated as the source of truth for the other.

## 2. Corrective trigger and disposition

The planning-time trigger proposed an evidence-backed audit after apparent cross-repository provider-metadata drift. Its Together premise was refuted during M001 execution: current first-party documentation confirms `https://api.together.ai/v1` as canonical and `https://api.together.xyz/v1` as a legacy alias. No template correction was warranted, and the zero template diff was the correct outcome.

OpenCode Go uses the confirmed `https://opencode.ai/zen/go/v1` prefix; EggPool already carried it correctly. The differing repository values demonstrate why first-party provider documentation, rather than sibling-repository copying, is authoritative. See the immutable M001 closure record for reviewed sources and the full disposition.

## 3. Invariants

- Provider-template changes must be based on current provider documentation or an equally authoritative first-party source, not on another local repository.
- Credentials, keys, account IDs, prompt data, and provider response bodies never enter template evidence.
- Existing provider IDs remain stable unless a separately reviewed compatibility migration is required.
- A base URL change must be qualified together with every configured path template/model-discovery path so version segments are neither duplicated nor dropped.
- Wire-surface declarations must match the endpoint family actually exposed by that provider.
- Verification-model changes must not be inferred from marketing names alone.
- Model/capability hints remain conservative; unknown capability stays unknown.
- This work does not add a second provider catalog owner or a network freshness checker to runtime/CI.
- Provider/account routing, retries, health/quarantine, quota, and transport are unchanged.

## 4. Non-goals

- Extracting a reusable provider-profile crate in this corrective.
- Making EggPool the metadata authority for CodeGG.
- Live provider probing from CI.
- Automatic web scraping or periodic endpoint freshness checks.
- Broad model-pricing refresh.
- Adding providers merely because the audit discovers them.
- Reworking provider transport, wire codecs, routing, persistence, or credential handling.
- Treating stale marketing/model lists as a reason to remove working live-discovery support.

## 5. Current state

At the baseline:

- `rust/assets/providers/_templates.toml` contains the bundled provider definitions used by setup/bootstrap and provider catalog behavior.
- Several templates already carry protocol-specific wire surfaces, auth overrides, model endpoints, and verification metadata.
- OpenCode Go currently uses the externally confirmed `https://opencode.ai/zen/go/v1` prefix.
- Together uses the canonical `https://api.together.ai/v1` prefix; `.xyz` is a legacy alias (M001 closure).
- There is no single committed review matrix recording which bundled endpoint/auth/model-discovery facts were checked against which first-party source at the current correction boundary.
- Existing catalog/provider tests prove parsing/runtime behavior but do not by themselves prove that externally mutable provider facts are current.

## 6. Target architecture

The templates remain EggPool-owned data. The corrective adds a bounded review/qualification layer around them:

```text
first-party provider documentation
          |
          v
bounded provider-fact review matrix
          |
          +--> _templates.toml correction
          +--> focused static/runtime fixture assertions
          `--> closure evidence + review date/source
```

No runtime web dependency is introduced.

## 7. Milestones

### Milestone 001 — Provider template endpoint and source reconciliation

Status: closed (`plans/closure/provider-profile-metadata/001-status.md`).

Class: invariant / polish.

Objective: audit every bundled provider template's endpoint/protocol/auth/model-discovery facts against current first-party documentation, correct confirmed drift, and leave deterministic regression evidence that protects the resulting local contract.

Implementation plan:

- `plans/implementation/provider-profile-metadata/001-provider-template-endpoint-and-source-reconciliation.md`

Dependencies:

- none. The existing provider templates, wire surfaces, and catalog tests are stable.
- operational coordination with CodeGG's provider-backend post-closure metadata corrective is useful but not a hard dependency.

Exit conditions:

- Together's canonical `.ai` endpoint is retained as confirmed by M001 closure;
- OpenCode Go's already-correct `/zen/go/v1` prefix is retained and explicitly qualified;
- every bundled provider receives an evidence disposition: confirmed, corrected, intentionally provider/operator-configurable, or blocked on insufficient first-party evidence;
- base URL + path-template composition is tested for corrected providers;
- model-discovery path/auth/wire-surface facts are checked together with the base URL;
- focused provider/catalog tests, full default/no-default checks required by the development workflow, and static tooling pass;
- no runtime/provider-routing/public-API change is introduced.

## 8. Cross-cutting requirements

Security: evidence stores URLs/document references only; no credentials or live responses.

Compatibility: provider IDs and config keys remain stable. If a corrected endpoint makes a historical configuration ambiguous, preserve explicit operator overrides and document the migration rather than silently rewriting user config.

Performance: no runtime network check or additional background task.

Documentation: architecture/catalog/provider docs must state that bundled templates are reviewed bootstrap facts, not an automatically fresh provider registry.

## 9. Verification strategy

Qualification must include:

- TOML parse/registry construction for all bundled templates;
- provider-template validation tests;
- corrected-provider request/path composition fixtures;
- model-discovery endpoint fixtures where a template declares one;
- wire-surface/auth mapping tests for touched entries;
- current catalog/provider integration targets;
- default and no-default workspace checks/Clippy appropriate to data/config changes;
- a closure matrix containing first-party source, review date, old value, final value, and disposition.

No live credentialed provider request is required.

## 10. Risks and decision points

- Provider documentation can expose multiple endpoint families. The plan must preserve protocol-specific surfaces instead of collapsing them into one base URL when that would lose semantics.
- Some providers intentionally support regional or operator-configured endpoints. Record that rather than inventing a universal default.
- If fixing a template would require a config/storage/public-API migration, stop and register a separately scoped plan.
- If a provider's first-party documentation is unavailable or contradictory, leave the current fact unchanged and mark it blocked/uncertain in closure evidence; do not guess.

## 11. Completion definition

The roadmap closes after M001 records an evidence-backed disposition for every bundled provider template, corrects confirmed drift, leaves focused regression coverage for corrected path composition, and reports no unresolved medium-or-higher correctness finding.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — provider template endpoint and source reconciliation | closed | `plans/implementation/provider-profile-metadata/001-provider-template-endpoint-and-source-reconciliation.md` | `plans/closure/provider-profile-metadata/001-status.md` | none |


M001 is terminal for this roadmap. It is closed with no successor registered; any future provider-template review requires a new bounded plan.
