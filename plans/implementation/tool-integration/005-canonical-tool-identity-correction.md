# Tool Integration M005 — Canonical tool identity correction

Status: ready

Repository baseline: `3fcbd712` (M002 closure)

Source roadmap: plans/subsystems/tool-integration-roadmap.md (M005 corrective)

Related closure: plans/closure/tool-integration/002-status.md

Primary class: corrective

## 1. Objective

Reconcile the tool-policy allowlist with the exact ten Eggsearch tool names
that M001's validated `/healthz`, MCP initialize and `tools/list` contract
accepts. Keep one canonical name list shared by readiness and authorization.

## 2. Readiness and dependency

M002 is closed, so the affected code and tests are available. The live M001
contract already records the supported Eggsearch version and ten canonical
names. This correction must close before M003 can consume `authorize_tool`.

## 3. Finding

`rust/src/operations/tools.rs` validates the Eggsearch catalog against one
ten-name set, while `rust/src/tool_policy.rs` currently recognizes a different
ten-name set. The overlap is only `web_search` and `web_fetch`; eight verified
Eggsearch names cannot receive a grant and eight non-catalog names can be
stored as grants. Although unknown calls fail closed, M003 would expose an
incorrectly narrowed catalog and policy administration would accept ghost
tool names.

## 4. Invariants

- The readiness catalog and grant identity list are one source of truth.
- Backend identity remains the fixed `eggsearch` value.
- Tool IDs remain exact, case-sensitive, finite and wildcard-free.
- No migration or policy row rewrite is needed because M002 shipped no public
  gateway and its allowlist is only consulted by internal policy operations.
- Unknown names continue to fail closed.

## 5. Scope and production changes

Move the verified ten-name list into the tool-policy authority and make the
M001 readiness checker consume that list. Remove duplicate static copies.
Add a cross-contract test that compares readiness acceptance and grant
validation for every canonical name and representative unknown, case-folded,
Unicode-lookalike and wildcard names. Do not change the service protocol or
the downstream tool catalog.

## 6. Verification

Run formatting, strict default/no-default Clippy, the focused tool backend and
tool grants tests, database/CLI compatibility tests, and the serial default
and no-default workspace suites. Re-run the explicitly requested live M001
Eggsearch readiness probe after the shared-list change.

## 7. Stop conditions

Stop if a different tool list is found in the pinned Eggsearch 0.4.3 live
catalog, or if reconciling names would require a migration or silently rename
persisted M002 grants. No rename migration is currently needed: M002 grants
were not exposed through a public route.

## 8. Closure evidence

Record the exact canonical list, live version, focused cross-contract tests,
default/no-default verification, and registry update. M003 remains blocked
until this corrective milestone and live initialize/list/call qualification
close.
