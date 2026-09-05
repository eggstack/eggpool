# W006 — Reasoning, Tools, Structured Output, and Loss Policy

Status: closed; see [closure record](../../closure/canonical-wire/006-status.md)

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w006--reasoning-tools-structured-output-and-loss-policy`

Primary class: capability/invariant

Hard dependencies: W004 and W005 accepted closure.

## 1. Objective

Centralize advanced cross-wire semantic adaptation that is currently distributed across Python codecs/transcoders: reasoning controls/content, tools and tool linkage, structured output, and explicit loss/warning/rejection policy. Ensure no supported conversion silently drops a material client intent.

## 2. Python oracle

Use W001 observations and current `transcoder` policy/context/error/prepared modules plus wire codecs. Preserve production behavior where it is deliberate; where Python contains duplicate paths, converge them behind one Rust semantic policy without changing observable outcomes.

## 3. Reasoning/thinking intent

Implement provider-neutral canonical reasoning adaptation for the fields frozen by W001/W002, including:

- explicit disable vs omitted control;
- effort levels and provider-specific effort names;
- token/budget controls;
- boolean/toggle controls;
- historical reasoning content vs a request for new reasoning;
- provider responses containing reasoning/thinking content or summaries;
- capability status supplied by caller/M5, including supported/unsupported/unknown/mixed where relevant;
- configured capability-policy outcomes already frozen in config/M5.

Codec policy may transform control representation. It must not infer provider capability by making network calls or alter M5 health/catalog state.

## 4. Reasoning loss rules

Freeze deterministic result classes for examples such as:

- exact control mapping;
- normalized equivalent effort/budget;
- safe omission because source explicitly requests no reasoning;
- warning when target cannot preserve non-material metadata;
- rejection when target cannot honor material requested reasoning and policy says reject;
- explicit downgrade only when current configured policy permits it.

Do not silently erase requested reasoning because the target profile lacks a field.

## 5. Tool semantics

Centralize:

- function/tool declarations and JSON-schema parameters;
- names/descriptions;
- tool choice modes and forced-tool choice;
- parallel tool-call intent;
- assistant tool/function calls;
- tool result linkage and stable IDs;
- ordering of mixed text/tool content;
- provider restrictions on tool names/IDs represented by current policy;
- malformed/duplicate/missing tool-call IDs.

Conversions must retain enough canonical identity for streaming deltas in W008 to assemble the same logical call.

## 6. Structured output

Port deterministic mapping for the currently supported response-format / JSON-object / JSON-schema controls. Preserve:

- whether structure is mandatory vs advisory where current surfaces distinguish it;
- schema/name/strictness intent;
- target-profile representation;
- warning/rejection when target cannot express the constraint.

Do not treat “prompt the model to output JSON” as equivalent to a formal structured-output constraint unless the current Python policy explicitly does so.

## 7. Loss policy architecture

Implement a single small adaptation outcome type shared by all codecs, for example:

- `Exact`;
- `Adapted { warnings }`;
- `Rejected { reason, field }`.

If the frozen Python contract has configured modes (reject/warn/drop/etc.), represent them as explicit policy input. Policy decisions must be deterministic and pure.

Every warning/loss item should contain stable semantic codes and source/target profile IDs, not raw request content.

## 8. No retry/negotiation coupling

A conversion rejection may later cause M7 to try another already-permitted wire/account. W006 only returns a typed rejection. It must not:

- mutate rejected-wire sets;
- choose another profile;
- call provider transport;
- mark health/quarantine;
- persist failure state;
- map the rejection to a retry count or downstream HTTP status.

## 9. Provider-sensitive pure adaptation

Provider kind/profile may be an explicit read-only input when current behavior differs by provider contract. Keep such logic localized and table/typed-policy driven where practical. Do not key behavior on account name, API key shape, or runtime error history.

W007 owns media/document/cache-specific adaptation; avoid duplicating it here.

## 10. Required differential tests

Build a cross-product focused on semantic risk rather than every syntactic permutation:

1. reasoning omitted/disabled/low-medium-high/custom budget across all representable source/target families;
2. unsupported/unknown/mixed capability policy decisions;
3. historical reasoning content vs new-reasoning controls;
4. single/multiple/parallel tool definitions/calls/results;
5. forced/auto/none tool choice;
6. malformed/duplicate/missing tool IDs;
7. mixed text + tool output ordering;
8. structured JSON object/schema/strictness conversions;
9. source/target combinations that must warn;
10. source/target combinations that must reject;
11. no silently dropped material feature—assert canonical semantic coverage before/after;
12. warning/error records contain no raw message/schema content beyond safe field/category identifiers;
13. native conversions remain warning-free where Python does.

## 11. Security/resource posture

Schema objects can be large. Apply W002 body/nesting/collection limits; do not clone full schemas merely to generate diagnostics. Tool/reasoning warning traces must be bounded. No new dependency should be necessary.

## 12. Verification

Run all Rust tests, W001 migration observations, targeted Python transcoder/capability/tool/reasoning/structured-output tests, format/lint/type checks, and `git diff --check`.

## 13. Acceptance criteria

W006 closes only if:

- all four family codecs use one loss/adaptation policy;
- requested reasoning/tools/structured constraints cannot disappear without an oracle-approved warning/outcome;
- tool-call/result identity survives supported conversions;
- capability/policy behavior matches Python semantically;
- conversion rejection is pure and does not trigger M7 behavior;
- W007 can add media/document/cache semantics without reopening the core loss model.

## 14. Stop conditions

Do not close if a material feature is silently dropped, provider capability is inferred from runtime errors here, tool IDs are regenerated nondeterministically, structured schema is reduced to prompt text without explicit policy, or codecs maintain separate inconsistent warning/error systems.

## 15. Closure evidence

Create `migration-rs/closure/canonical-wire/006-status.md` with cross-wire semantic matrix, loss/rejection inventory, supported differences, verification, and registry transition promoting W007.
