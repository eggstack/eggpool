# Request Admission and Wire Milestone 005 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/005-planning-reconciliation-and-minor-wire-cleanup.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-005--planning-reconciliation-and-minor-wire-cleanup`
- `plans/subsystems/request-admission-wire-roadmap.md#13-wire-kernel-extraction-extension`

Repository baseline reviewed: `4a1315a13234da8058432ca3460d3e128fd50b43`

Implementation commits or pull requests:

- `4a1315a1` — Implement request-admission-wire M005 reconciliation and wire cleanup

## 1. Executive finding

M005 closes the post-M004 corrective pass without changing EggPool wire
behavior. Planning control surfaces are truthful again (one milestone-table
row each for M001–M005, registry summaries matching the tables, no stale
blocked M004 row), the duplicated client-executed `tool_search` declaration
predicate has one neutral owner in `eggpool-wire` consumed by both structural
decode and EggPool native preservation, and the unemitted `AdaptationEffectClass::Approximated`
variant is an explicit documented/tested reservation. No medium-or-higher
finding remains; the request-admission-wire subsystem is closed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Roadmap/registry reconciliation, one row per M001–M005 | `request-admission-wire-roadmap.md` milestone table; `registry.md` Active/Ready/Recently closed; structural inspection (unique IDs, no stale blocked M004 row, "Most recently closed" == table) | pass | M001–M004 closure records untouched |
| M005 lifecycle tracked, subsystem closable | Plan `ready` → `active` in `4a1315a1`; this record closes plan + roadmap + registry | pass | Roadmap `active` → `closed`; subsystem leaves Active table |
| One shared `tool_search` classifier owned by `eggpool-wire` | `eggpool-wire/src/decode.rs::is_client_tool_search_declaration` (`pub`, rustdoc); `request/admission.rs` imports it, local duplicate deleted | pass | Single semantic implementation; no body serialize/clone; pure over parsed object |
| Positive/negative classifier equivalence | Kernel matrix test (client/server/missing/null/wrong-type/non-`tool_search`); contract test proving decode + preservation agreement per case | pass | Codex deferred-search behavior unchanged (compat suites green) |
| `Approximated` deliberate disposition, no reclassification | Extended `AdaptationEffectClass::Approximated` rustdoc (reserved; future remap only with proven intent preservation); `approximated_stays_reserved_on_the_request_path` invariant test over corpus × all targets + known codes | pass | No fixture fidelity/effect change; planner/encoder agreement strict |
| Narrow planning regression guard | `wire_kernel_boundary::shared_tool_search_classifier_has_one_owner` (one owner, crate-visible, no admission duplicate, admission consumes) | pass | Smallest useful check on an existing surface; no new planning framework |
| No dependency/config/storage/release/routing/provider/protocol change | `git diff --name-only`: 8 files (3 planning, 5 Rust); no `Cargo.toml`/`Cargo.lock`, config, migration, or feature change | pass | `cargo deny`/release requalification not required per plan §11 |

## 3. Production implementation evidence

Ownership (unchanged except the single predicate):

- `rust/crates/eggpool-wire/src/decode.rs`: private
  `is_client_tool_search_declaration` → `pub` with neutral-ownership rustdoc;
  new `decode::tests::client_tool_search_classifier_matrix` (10 cases).
- `rust/src/request/admission.rs`: local duplicate deleted; imports
  `crate::wire::decode::is_client_tool_search_declaration` for native
  preservation/feature summarization. Preservation lifetime and stateless
  product policy remain EggPool-owned; the kernel helper only classifies.
- `rust/crates/eggpool-wire/src/fidelity.rs`: `Approximated` rustdoc extended
  (reserved; unsupported reasoning controls stay `Omitted`; future emission
  only for provably intent-preserving remaps; no reclassification of existing
  outcomes); new `approximated_stays_reserved_on_the_request_path` test.
- `rust/tests/wire_extraction_contract.rs`:
  `contract_client_tool_search_classifier_is_shared` (6 declarations × decode
  tool count + `admit_request` native summary agreement).
- `rust/tests/wire_kernel_boundary.rs`:
  `shared_tool_search_classifier_has_one_owner` duplication guard.
- Root `rust/src/wire/decode.rs` facade unchanged (`pub use
  eggpool_wire::decode::*` re-exports the now-public helper; still 6 lines).

Source map (one classifier): definition in
`rust/crates/eggpool-wire/src/decode.rs`; call sites in the same file
(structural `decode_tools`) and `rust/src/request/admission.rs`
(`native_feature_summary`). `grep` finds no other
`fn is_client_tool_search_declaration` definition.

Semantics preserved exactly: portable iff declaration `type == "tool_search"`
with `execution == "client"`; server-owned, missing, null, wrong-type, and
non-`tool_search` forms stay native-only/non-portable in both paths.

## 4. Verification executed

### Commands run

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

### Results

Local (serial where required):

- `cargo fmt --check` — pass.
- `cargo clippy --workspace --all-targets -- -D warnings` — pass.
- `cargo check --workspace --all-targets --no-default-features` — pass.
- `cargo clippy --workspace --all-targets --no-default-features` — pass.
- `cargo test -p eggpool-wire` — 33 passed (incl. new classifier matrix +
  `Approximated` reservation tests), 0 failed; doc-tests 0.
- Root focused: `wire_extraction_contract` 15 (incl. new shared-classifier
  test), `wire_kernel_boundary` 4 (incl. new one-owner guard),
  `canonical_request` 12, `codex_responses_compat` 15,
  `codex_compaction_compat` 13, `wire_codecs` 11, `wire_runtime` 8 — all pass.
- Full default workspace suite — 781 passed, 0 failed (M004 777 + 4 new tests).
- Full `--no-default-features` workspace suite — 782 passed, 0 failed (M004
  778 + 4 new tests).
- `git diff --check` — pass.
- Planning structural inspection — pass: milestone rows
  `[001, 002, 003, 004, 005]` unique; no stale blocked M004 row; registry
  "Most recently closed" matched the Recently closed table at each stage.
- No `Cargo.toml`/`Cargo.lock` change, so `cargo deny`, release build, and
  dependency-tree requalification were not re-run, per plan §11. No Python
  tooling change; `uv`/Ruff/Pyright/pytest not re-run. CI remains hosted
  truth; no CI run claimed here.

## 5. Invariant review

- M001–M004 closure records immutable: `git diff --name-only` touches no
  `plans/closure/request-admission-wire/00{1,2,3,4}-status.md` file.
- `eggpool-wire` remains the single source of truth for canonical IR,
  decoding, adaptation/fidelity, finite codecs, provenance, conformance, and
  stream state machines; root `rust/src/wire/*` files remain facades
  (`wire_kernel_boundary` green).
- Classifier semantics exact (client-only portability); native Responses
  preservation, cross-surface blockers, deferred-search wrapping, stable
  notice ordering, and `LossPolicy` behavior unchanged (contract + Codex
  suites green).
- No fidelity reclassification: corpus plans emit only established classes;
  `Approximated` (and `Synthesized`) absent on the request path by test.
- No fabricated provider-owned values; no request-limit, stateless-policy,
  routing, provider-profile, compact, streaming-terminal, or HTTP change.
- No new dependency in `eggpool-wire`; no config, migration, release,
  publication, or repository-split change; `publish = false` retained.
- `--no-default-features` parity green (782 passed).

## 6. Failure and recovery review

The shared classifier is a pure synchronous predicate over an already parsed
JSON object: no state, no unbounded allocation, no I/O, no cancellation,
restart, retry, locking, or contention behavior. No error-precedence,
admission, preservation, or adaptation change was observed for any fixture;
per the plan stop condition, none was "fixed" by editing fixtures. Full
coordinator boundary/finalization/publication coverage ran inside the green
workspace suites.

## 7. Migration and compatibility review

No user/operator migration. No public endpoint or JSON shape change; no
`WireSurface`, `WireCodecId`, notice-code, effect-code, or fidelity change;
no configuration or persisted-state change; no dependency/feature change; no
release artifact change. Planning correction is append-only: M001–M004
plans/closure records intact; only M005 lifecycle/closure metadata added.

## 8. Security review

No auth change. The classifier inspects only structural `type`/`execution`
discriminators, never prompts, bodies, credentials, or cache keys. No new
persistence/logging of request content. DoS bounds unchanged (existing
decode/stream ceilings; classifier is O(1) field comparison).

## 9. Documentation and operations

- `plans/subsystems/request-admission-wire-roadmap.md` — M005 closed with
  closure link; roadmap `closed`.
- `plans/registry.md` — "Most recently closed" → M005; M005 in Recently
  closed; subsystem out of the Active table; unblock audit extended.
- `eggpool-wire` rustdoc — classifier ownership + `Approximated` reservation.
- No architecture deep-dive change (no ownership change; the helper moves no
  admission/preservation responsibility into the kernel).

## 10. Unresolved findings

No medium-or-higher findings. No low findings carried forward: both M004 low
items (predicate duplication, `Approximated` disposition) are closed above
with regression tests. No residual scope opened.

## 11. Roadmap disposition

Milestone closed. The request-admission-wire subsystem is closed: M001 body
admission, M002–M004 extraction and fidelity/provenance hardening, and M005
planning reconciliation plus minor wire cleanup are all evidenced with no
unresolved medium-or-higher finding. No blocked plan is promoted by this
closure: provider-transport M002 remains blocked on upstream Eggfetch typed
classification API work, unaffected by this pass; no new ready successor
exists in this subsystem.

## 12. Registry updates

Changes applied in the same commit:

- `plans/implementation/request-admission-wire/005-...md`: `active` → `closed`.
- `plans/closure/request-admission-wire/005-status.md`: created (this file).
- `plans/subsystems/request-admission-wire-roadmap.md`: `active` → `closed`;
  M005 row `active` → `closed` with this closure link.
- `plans/registry.md`: "Most recently closed" → M005; M005 active ready-row
  removed and recorded in Recently closed; subsystem removed from the Active
  subsystem roadmaps table; unblock audit notes M005 closure and that no
  blocked work is promoted.
