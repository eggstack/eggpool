# Request Admission and Wire Milestone 005 — Planning Reconciliation and Minor Wire Cleanup

Status: closed

Repository baseline: `109a50f35032a22e26e8eb5be687ac7e01637298`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-005--planning-reconciliation-and-minor-wire-cleanup`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` — canonical wire boundary, secret-free diagnostics, and no-default parity remain unchanged.
- `plans/000-long-term-specification.md#4-protocol-and-compatibility` — current public surfaces, adaptation semantics, and terminal-evidence behavior remain unchanged.
- `plans/001-terminology-and-domain-model.md` — canonical IR and admission ownership remain EggPool's established model.
- `plans/003-planning-process.md` — registry/roadmap truthfulness and corrective-plan lifecycle.

Applicable ADRs:

- None required. This is a corrective/polish milestone with no ownership, public protocol, configuration, persistence, transport, or release decision.

Primary class: polish

Corrects:

- `plans/implementation/request-admission-wire/004-fidelity-provenance-and-conformance-hardening.md`
- `plans/closure/request-admission-wire/004-status.md`
- the low-severity predicate duplication first recorded in `plans/closure/request-admission-wire/002-status.md`.

## 1. Objective

Reconcile the planning control surfaces after the completed M002–M004 wire-kernel extraction and remove the two small cleanup findings left by M004 without changing EggPool wire behavior.

The bounded result is: truthful roadmap/registry state, one shared semantic classifier for client-executed `tool_search` declarations, and an explicit documented disposition for the currently unemitted `Approximated` adaptation-effect variant.

## 2. Why this milestone is ready

M002, M003, and M004 are closed. Current HEAD `109a50f35032a22e26e8eb5be687ac7e01637298` has successful hosted CI and dependency-audit runs for the M004 closure. There is no external dependency and no blocked implementation prerequisite.

This is a corrective plan because closure identified low-severity cleanup debt and the M004 closure commit left contradictory planning metadata.

## 3. Current implementation evidence

At `109a50f35032a22e26e8eb5be687ac7e01637298`:

- `plans/closure/request-admission-wire/004-status.md` closes M004 and records no medium-or-higher finding.
- `plans/subsystems/request-admission-wire-roadmap.md` contains both a correct closed M004 milestone row and a stale duplicate blocked M004 row. Its completion text still describes M002–M004 as the terminal sequence even though a corrective pass is now explicitly requested.
- `plans/registry.md` correctly lists M002–M004 in Recently closed but its leading "Most recently closed" sentence still names M001. Before this registration it also described the request-admission-wire sequence as complete with no ready successor.
- `rust/crates/eggpool-wire/src/decode.rs` and `rust/src/request/admission.rs` each contain the same small `is_client_tool_search_declaration` predicate. M002 recorded this as low severity; M004 carried it forward.
- `rust/crates/eggpool-wire/src/fidelity.rs` exposes `AdaptationEffectClass::Approximated`, while M004 records that current request codecs do not emit it because unsupported reasoning controls are dropped with explicit loss rather than approximated. The variant is therefore intentional headroom only if the crate documents/tests that fact.
- `rust/tests/wire_extraction_contract.rs` and `rust/tests/wire_kernel_boundary.rs` already provide the regression harness needed for this cleanup.
- Root wire implementation remains facade/adapters around the single `eggpool-wire` source of truth.

### Why the prior verification missed these findings

The M004 verification concentrated on protocol equivalence, planner/encoder agreement, bounded provenance, stream conformance, package boundaries, and runtime no-regression. It did not include a structural assertion that the roadmap milestone table contains unique milestone IDs or that the registry's prose-level "most recently closed" summary matches its Recently closed table.

The duplicated `tool_search` classifier was already known and intentionally deferred because M004 was focused on fidelity/provenance semantics. M004 did not need to touch that preservation predicate to close its capability work, so the duplication remained.

The `Approximated` variant was not a correctness failure: M004 deliberately reserved it while current codecs classify their behavior as omission or rewrite. The cleanup gap is documentation/test intent, not a missing runtime path.

## 4. Invariants that must not regress

- M001–M004 closure records remain immutable historical evidence; do not rewrite them to hide the cleanup.
- `eggpool-wire` remains the single source of truth for canonical IR, decoding, adaptation/fidelity, finite codecs, provenance, conformance, and stream state machines.
- Root `rust/src/wire/*` facade/boundary rules remain enforced by `wire_kernel_boundary`.
- Client-executed `tool_search` recognition semantics remain exactly: declaration type `tool_search` with `execution == "client"`; server-owned or missing/other execution forms must not become portable client declarations.
- Native Responses preservation, cross-surface blockers, deferred-search wrapping, stable notice ordering, and `LossPolicy` behavior remain unchanged.
- No fidelity classification changes merely to make `Approximated` used.
- Provider-owned signatures, encrypted reasoning, IDs, or terminal events remain non-fabricated.
- No request limit, stateless Responses policy, routing selection, provider profile, compact behavior, streaming terminal rule, or HTTP behavior changes.
- No new runtime/network/persistence dependency enters `eggpool-wire`.
- No config, database migration, release artifact, crate publication, or repository split.
- `--no-default-features` parity remains green.

## 5. Scope

### In scope

- Correct request-admission-wire roadmap/registry status drift created around M004 closure.
- Ensure the milestone table has one row per milestone and that M005 lifecycle/status is represented consistently.
- On M005 closure, transition the request-admission-wire roadmap out of Active subsystem roadmaps and make registry summaries truthfully identify the latest closure.
- Replace the two duplicated `is_client_tool_search_declaration` implementations with one neutral classifier owned by `eggpool-wire`, consumed by both structural decode and EggPool preservation/admission.
- Add focused equivalence tests covering `execution = "client"`, `"server"`, missing, null, wrong type, and non-`tool_search` declarations.
- Resolve the `Approximated` finding by documenting and testing its intentional reservation. Removing it is permitted only if current crate documentation/API intent clearly shows no future semantic role and exhaustive-match fallout remains purely internal.
- Add the smallest useful planning regression check if one already fits an existing validator/test surface; do not create a broad new planning framework solely for this incident.

### Explicitly out of scope

- Any new LLM/provider surface or codec.
- Changes to `TranslationPlan` fidelity results or adaptation notice mappings.
- Routing based on fidelity.
- General redesign of provenance, tool search, request admission, or Responses handling.
- Publishing `eggpool-wire` to crates.io or splitting it into another repository.
- Renumbering historical milestones or editing M001–M004 closure evidence.
- Broad plan-lint infrastructure, planning schema migration, or cleanup of unrelated subsystem records.
- Dependency upgrades or Cargo feature changes.

## 6. Required production changes

### Planning reconciliation

Make `plans/subsystems/request-admission-wire-roadmap.md` and
`plans/registry.md` internally consistent.

During implementation:

- remove any stale duplicate M004 row still present;
- retain M001–M004 as closed with their existing closure links;
- track M005 as active/closing while implementation is underway;
- on accepted closure, set the roadmap to `closed`, mark M005 closed with
  `plans/closure/request-admission-wire/005-status.md`, remove the subsystem
  from the Active subsystem roadmaps table, and record M005 in Recently
  closed;
- update prose summaries such as "Most recently closed" to match the table;
- leave Provider transport M002 and all unrelated planning untouched.

If an existing planning-validation surface can cheaply assert unique milestone
IDs or registry/roadmap status consistency, add the narrow assertion there.
Otherwise closure evidence must include an explicit structural inspection;
do not invent a new general validator.

### Shared client tool-search classifier

Establish one neutral helper in `eggpool-wire` for deciding whether a tool
declaration is the portable client-executed `tool_search` form. Prefer the
existing decode semantics as the authoritative behavior.

Both:

- canonical structural tool decoding; and
- EggPool native preservation/feature summarization

must call the same helper. Do not serialize/clone request bodies to cross the
boundary. Keep the helper pure over the already parsed JSON object/value.

The root admission layer continues to own preservation lifetime and stateless
product policy; this helper does not move those responsibilities into the
kernel.

### Approximated effect disposition

Preserve the M004 fidelity contract. The default preferred outcome is to keep
`AdaptationEffectClass::Approximated` and add clear rustdoc stating that it is
reserved for a future codec that intentionally preserves semantics via an
approximation; current codecs must not emit it merely to eliminate an unused
variant.

Add a test pinning the current invariant that all existing request-planning
fixtures emit only their already-established effect classes. Do not reclassify
an `Omitted` or `Rewritten` effect as `Approximated` without a separate
semantic change plan.

## 7. Ordered work packages

### Work package A — Reconcile planning control surfaces

Intent:

Make the roadmap and registry truthful without rewriting history.

Required changes:

- Remove stale duplicate/obsolete M004 planning state.
- Establish M005 lifecycle in roadmap + registry.
- Prepare the closure transition so the subsystem can become `closed` once
  code/docs verification succeeds.
- Update the "most recently closed" summary from M001 to the actual latest
  accepted closure.

Acceptance evidence:

- one milestone-table row each for M001–M005;
- all plan/closure links resolve;
- registry Active/Ready/Recently closed sections agree with roadmap status;
- Provider transport entries are byte-for-byte unchanged except incidental
  surrounding line movement.

### Work package B — Deduplicate the tool-search semantic predicate

Intent:

Remove the only known duplicated semantic decision carried across the
extraction seam.

Required changes:

- Add/export one neutral classifier from `eggpool-wire`.
- Replace both current predicate implementations with calls to it.
- Delete the duplicate root/kernel logic.
- Add table-driven negative/positive tests, including malformed and
  server-owned execution forms.

Acceptance evidence:

- grep/source inspection finds one semantic implementation;
- canonical decode and native-preservation tests retain identical outcomes;
- Codex deferred-search behavior remains unchanged.

### Work package C — Make the reserved effect class intentional

Intent:

Resolve the "unused variant" cleanup finding without manufacturing behavior.

Required changes:

- Document `Approximated` semantics and why current codecs do not emit it.
- Add an invariant test over the current corpus/effect mapping.
- Remove the variant only if doing so is strictly internal and clearly better
  than an explicit reservation; if removal would narrow intended reusable API
  semantics, keep it.

Acceptance evidence:

- no existing fixture changes fidelity/effect result;
- request planner/encoder agreement remains strict;
- closure records a deliberate keep/remove disposition.

### Work package D — Closure and status normalization

Intent:

Finish the corrective pass with planning state that matches repository state.

Required changes:

- create `plans/closure/request-admission-wire/005-status.md`;
- mark the M005 implementation plan closed;
- set the roadmap to closed if no successor is simultaneously approved;
- update registry current/recent status and unblock audit;
- record any residual low finding rather than silently opening a new scope.

Acceptance evidence:

- no contradictory request-admission-wire status remains in roadmap/registry;
- closure evidence maps every M005 requirement to code/docs/tests.

## 8. Failure, cancellation, restart, contention semantics

No runtime lifecycle behavior should change. The shared classifier is a pure
synchronous predicate over an already parsed JSON object. It owns no state,
allocates no unbounded data, performs no I/O, and creates no cancellation,
restart, retry, locking, or contention behavior.

If deduplication changes error precedence, request admission, native
preservation, or cross-surface adaptation for any fixture, revert that code
change and stop rather than "fixing" the fixture.

## 9. Compatibility and migration

No user/operator migration.

Compatibility requirements:

- no public endpoint or JSON shape changes;
- no `WireSurface`, `WireCodecId`, notice-code, effect-code, or fidelity
  changes;
- no configuration or persisted-state changes;
- no dependency/feature changes;
- no release artifact changes;
- `eggpool-wire` remains `publish = false`.

The planning correction is append-only with respect to closed evidence:
M001–M004 plans/closure records remain intact.

## 10. Required tests

At minimum:

- package-level `eggpool-wire` tests;
- table-driven classifier tests in the kernel;
- request/native-preservation tests covering client/server/missing execution;
- `rust/tests/wire_extraction_contract.rs`;
- `rust/tests/wire_kernel_boundary.rs`;
- `rust/tests/canonical_request.rs`;
- `rust/tests/codex_responses_compat.rs`;
- `rust/tests/codex_compaction_compat.rs`;
- `rust/tests/wire_codecs.rs`;
- `rust/tests/wire_runtime.rs`;
- full default and no-default workspace suites.

Planning reconciliation must also be inspected for duplicate M005/M004 rows,
dangling closure links, and registry/roadmap disagreement.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test wire_extraction_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_kernel_boundary -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
git diff --check
```

If Cargo manifests/lockfile do not change, `cargo deny`, release build, and
dependency-tree requalification are not required solely for this cleanup. If
implementation changes any dependency or feature, stop and expand verification
to the repository's dependency-change gates before closure.

## 12. Documentation updates

- `plans/subsystems/request-admission-wire-roadmap.md` — unique/current
  milestone status and final closed state.
- `plans/registry.md` — current/recent closure truth.
- `rust/crates/eggpool-wire/src/fidelity.rs` rustdoc for
  `Approximated` if retained.
- Minimal `eggpool-wire` decode rustdoc for the shared client tool-search
  classifier if it becomes part of the crate-visible API.
- No architecture deep-dive update unless implementation changes ownership;
  such an ownership change is a stop condition, not expected work.

## 13. Acceptance criteria

- Roadmap and registry have no contradictory M004/M005 state.
- M001–M004 historical plan/closure files remain unchanged.
- One semantic implementation decides client-executed `tool_search`
  declarations.
- Structural decode and native preservation agree for all positive/negative
  classifier cases.
- Current Codex tool-search/deferred-search behavior is unchanged.
- `Approximated` has a deliberate documented/tested disposition without
  changing current fixture fidelity.
- No dependency, config, storage, release, routing, provider, or protocol
  capability changes.
- Required focused and full default/no-default verification passes.
- M005 closure can mark request-admission-wire closed with no
  medium-or-higher unresolved finding.

## 14. Stop conditions

Stop and report rather than improvise if:

- unifying the classifier changes accepted/rejected request forms or notice
  ordering;
- the shared helper would require moving EggPool admission/preservation
  ownership into the kernel;
- resolving `Approximated` would require reclassifying existing conversions;
- a dependency/feature addition appears necessary;
- cleanup expands into new provider support, fidelity-driven routing, broader
  provenance redesign, or publication;
- current repository state no longer matches the M002–M004 closure evidence.

## 15. Closure evidence required

The closure record must contain:

- before/after roadmap and registry status summary showing the stale duplicate
  M004 row and stale M001 "most recently closed" text are resolved;
- explicit statement that M001–M004 closure records were not rewritten;
- source map proving one `tool_search` declaration classifier remains;
- positive/negative classifier test matrix;
- `Approximated` keep/remove rationale and invariant-test evidence;
- focused wire/Codex test results;
- full default and no-default workspace results;
- clippy/check/fmt and `git diff --check` results;
- statement that no Cargo dependency/feature, config, database, release,
  routing, provider, or public wire behavior changed;
- residual findings and unblock audit.

## 16. Handoff notes

This is a cleanup pass, not a new wire milestone. Prefer deleting duplicate
logic and contradictory planning state over introducing abstractions.
Do not touch closed M001–M004 evidence except to link to it from M005.
Preserve user changes and run Rust tests serially with `--test-threads=1`.
