# Provider Profile Metadata — Planning and Documentation Reconciliation Corrective Roadmap

Status: closed

Repository baseline reviewed: `43c987ea458bd563d5108fd8051ad31185704bb0`

Predecessor work:

- `plans/subsystems/provider-profile-metadata-corrective-roadmap.md`
- `plans/implementation/provider-profile-metadata/001-provider-template-endpoint-and-source-reconciliation.md`
- `plans/closure/provider-profile-metadata/001-status.md`
- implementation `805d6f70`

Canonical references:

- `plans/003-planning-process.md`
- `plans/registry.md`
- `architecture/deep-dive-providers.md`
- `architecture/deep-dive-catalog.md`

No ADR is required. This corrective changes no provider template, runtime, wire surface, routing, account, credential, catalog-refresh, persistence, or public HTTP behavior.

## 1. Corrective trigger

Provider-profile metadata M001 is technically closed with strong evidence, but its current planning control surfaces retain stale pre-implementation lifecycle and source assumptions.

Current contradictions:

1. `plans/subsystems/provider-profile-metadata-corrective-roadmap.md` still has top-level `Status: active` even though its only milestone is closed, its completion definition is satisfied, and no successor is registered.
2. `plans/registry.md` still lists the provider-profile metadata corrective under Active subsystem roadmaps with status `active` while the same registry lists M001 in Recently closed and says no successor is registered.
3. The original roadmap/implementation-plan trigger states that EggPool's Together `.ai` endpoint is stale and should move to `.xyz`. M001's execution-time first-party review explicitly refuted that premise:
   - canonical current Together prefix: `https://api.together.ai/v1`;
   - `https://api.together.xyz/v1` is a legacy alias;
   - no template change was warranted.
4. The closed implementation plan is a valid historical handoff artifact, but it lacks a prominent supersession note telling readers that its Together premise was rejected by accepted closure evidence.
5. Registry prose near the original opening entry still states the refuted premise before a later unblock-audit paragraph corrects it. A compact current control surface should not require readers to reconcile contradictory chronology themselves.

No technical corrective is required. The accepted closure found no unresolved medium-or-higher provider-template issue.

## 2. Corrective milestone

### C001 — Closed-roadmap and source-truth reconciliation

Status: closed (`plans/closure/provider-profile-metadata-planning-reconciliation/001-status.md`).

Implementation plan:

- `plans/implementation/provider-profile-metadata-planning-reconciliation/001-closed-roadmap-and-source-truth-reconciliation.md`

Primary class: polish / documentation.

Dependencies:

- provider-profile metadata M001 closed — satisfied;
- no code/package/runtime dependency.

## 3. Invariants

C001 MUST preserve:

- M001 closure record as immutable historical evidence;
- `rust/assets/providers/_templates.toml` byte-for-byte;
- Together at canonical `https://api.together.ai/v1`;
- OpenCode Go at `https://opencode.ai/zen/go/v1`;
- the two low deferred discovery uncertainties (Fireworks and Alibaba) as deferred, not silently "fixed";
- no runtime freshness checker;
- no CodeGG-as-authority relationship;
- all provider IDs/config/storage/protocol behavior.

## 4. Non-goals

- Re-auditing all 23 provider templates.
- Live provider verification.
- Changing Together or any other endpoint.
- Resolving Fireworks/Alibaba low findings without new evidence.
- Reopening request-admission-wire, provider transport, routing, catalog runtime, or persistence.
- Editing M001 closure evidence.
- Building a provider-profile crate or cross-repo sync mechanism.

## 5. Required reconciliation

C001 must:

- set the predecessor provider-profile metadata roadmap top-level status to `closed`;
- keep M001 closed with its existing closure pointer and explicitly state that no successor is registered;
- revise current-authority roadmap trigger/current-state prose so the accepted Together result is stated directly rather than leaving the refuted `.xyz` assumption as present-tense truth;
- preserve the historical planning context by labeling the original Together correction as a planning-time premise superseded by M001 closure;
- add a prominent post-closure supersession note near the top of the closed M001 implementation plan pointing to `plans/closure/provider-profile-metadata/001-status.md`;
- avoid rewriting the historical implementation-plan body line-by-line;
- change the provider-profile metadata row in `plans/registry.md` from active to closed/no-successor state;
- reconcile the original registry opening paragraph so it no longer states the refuted Together premise as current truth;
- preserve the Recently closed M001 row and closure evidence;
- update architecture docs only if a contradiction is discovered; current `deep-dive-providers.md` already states the accepted `.ai`/legacy-`.xyz` disposition.

## 6. Verification

Documentation-only verification:

```bash
git diff --check

grep -n "^Status:" plans/subsystems/provider-profile-metadata-corrective-roadmap.md
grep -n "api.together" plans/subsystems/provider-profile-metadata-corrective-roadmap.md
grep -n "api.together" plans/implementation/provider-profile-metadata/001-provider-template-endpoint-and-source-reconciliation.md
grep -n "Provider profile metadata corrective" plans/registry.md
grep -n "api.together" architecture/deep-dive-providers.md
```

Explicit inspection must confirm:

- no Rust/TOML/Cargo/config/script file changed;
- M001 closure record has zero diff;
- all referenced plan/closure links resolve;
- Active subsystem roadmaps no longer claims this closed/no-successor predecessor workstream is active after C001 closure.

No Cargo test/Clippy run is required for a Markdown-only diff. If implementation touches production/test/config files, stop and register a technical corrective instead.

## 7. Acceptance criteria

C001 closes when:

- predecessor roadmap top-level state is closed;
- registry lifecycle matches the closed/no-successor state;
- present-tense planning text does not claim Together `.ai` is stale;
- historical planning intent remains visible with a clear supersession pointer;
- implementation plan explicitly points to closure evidence as final source truth;
- architecture docs, roadmap, registry, implementation-plan annotation, and closure record agree;
- M001 closure evidence is unchanged;
- no non-Markdown file changes;
- `git diff --check` passes.

## 8. Stop conditions

Stop and create separate work if:

- a currently bundled provider fact is newly proven wrong;
- Fireworks or Alibaba uncertainty becomes an actionable correctness defect;
- reconciliation would require changing `_templates.toml`, runtime code, config, or public behavior;
- historical closure evidence itself is materially false.

## 9. Closure evidence

Create:

- `plans/closure/provider-profile-metadata-planning-reconciliation/001-status.md`

Record:

- files changed;
- before/after roadmap/registry lifecycle state;
- Together planning-premise supersession;
- confirmation that M001 closure and templates are unchanged;
- reference-resolution and `git diff --check` results;
- zero production/config/test diff;
- unblock audit (expected: nothing unblocked).


C001 is complete. The predecessor roadmap is terminal and closed, and no successor is registered.
