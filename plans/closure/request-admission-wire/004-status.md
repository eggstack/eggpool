# Request Admission and Wire Milestone 004 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/004-fidelity-provenance-and-conformance-hardening.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-004--fidelity-provenance-and-conformance-hardening`
- `plans/subsystems/request-admission-wire-roadmap.md#13-wire-kernel-extraction-extension`

Repository baseline reviewed: `61470ef788e287c49b4a51062eaba439049d46dc`

Implementation commits or pull requests:

- `72d6d442` — Implement request-admission-wire M004 fidelity and provenance

## 1. Executive finding

M004 hardens the extracted kernel around its real differentiation —
auditable fidelity, bounded source-native provenance, strict terminal
evidence, and reusable conformance vectors — without changing EggPool's
externally observable request/response/stream behavior or warn/reject
decisions. New APIs (`TranslationPlan`/`Fidelity`/`AdaptationEffect`,
`WireProvenance`, `conformance` vectors) are additive in `eggpool-wire`
with an EggPool compat shim (`adapters::provenance_from_preservation`).
The planner shares the encoding decision engine (plus a dry-run encode that
closed a real Responses-metadata gap), achieving strict planner/encoder
agreement across 13 fixtures × 5 surfaces. The subsystem sequence M002–M004
is now complete with no medium-or-higher finding.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Pure preflight translation plan + fidelity vocabulary | `eggpool-wire/src/fidelity.rs`: `TranslationPlan{source,target,fidelity,effects}`, `Fidelity{Exact,WireNormalized,SemanticallyEquivalent,Lossy,Unsupported}` (no `Ord`; explicit predicates), `plan_request_translation` | pass | Computable without transport/mutation; shares checks with encoding |
| Typed adaptation effects, compat retained | `AdaptationEffectClass{Rewritten,Omitted,Approximated,Synthesized,Blocked}` + `classify_notice` single table; old notice codes + `LossPolicy` untouched; effect codes == notice codes in order | pass | `Synthesized` never emitted on request path; `Approximated` reserved (codecs drop, not remap) |
| Bounded provenance separate from IR | `eggpool-wire/src/provenance.rs`: `WireProvenance` (surface/codec, fragments/shape, completeness/truncation), ceilings 32/256B/16KiB/8, redaction-safe `Debug`, `may_restore_exact` | pass | No credentials; same-request lifetime; incomplete can never claim `Exact` |
| Responses preservation migrated behind compat | `adapters::provenance_from_preservation`; native finite + cross-surface blocker tests unchanged | pass | Other surfaces start empty/partial, no false round-trip claims |
| Same-surface vs cross-surface contract | `may_restore_exact` (Complete + source==target only); cross-surface marker-free test | pass | Cross-surface never injects source extras blindly |
| Stream/native observation contract | `stream.rs` bounded-observation docs; `conformance.rs`: 32 vectors (5 dialects × success/incomplete/provider-error/malformed/EOF-before/EOF-after-partial/post-terminal); every vector at splits {0,1,mid,len-1,len} + 3-way + UTF-8-interior `🌍` splits with byte-identical events+summary | pass | No full-stream buffering; bytes caller-owned |
| Conformance/public API hardening | Protocol-only vectors + tests in crate; crate rustdoc (IR vs provenance vs fidelity, coverage, limits, MSRV 1.89, no-I/O, non-guarantees, comparison boundary); dep guard; Debug-bound guard | pass | `cargo test -p eggpool-wire` 31 passed |
| Crate stays sans-I/O, unpublished | Deps still only serde/serde_json/sha2/thiserror/toml; `publish=false`; `cargo tree` clean; `forbid(unsafe_code)` | pass | No publication, no semver-1.0 commitment |
| EggPool decisions unchanged | No routing/dispatch/admission/config change; planner not wired into selection; full suites green | pass | Additive only |

## 3. Production implementation evidence

Added (no existing behavior edited):

- `rust/crates/eggpool-wire/src/fidelity.rs` (~1200 lines incl. tests):
  planner + taxonomy + predicates + agreement/compat tests.
- `rust/crates/eggpool-wire/src/provenance.rs` (~600 lines): provenance
  types, ceilings, builders, restore contract, bounds/redaction/restore
  tests.
- `rust/crates/eggpool-wire/src/conformance.rs` (~800 lines):
  `StreamConformanceVector`, `stream_conformance_vectors()` (32),
  `sse_split_points`, matrix/terminal tests.
- `rust/crates/eggpool-wire/src/lib.rs`: crate rustdoc + re-exports.
- `rust/crates/eggpool-wire/src/stream.rs`: docs only on native
  observation seam.
- Root facades `rust/src/wire/{fidelity,provenance,conformance}.rs` (glob
  re-exports); `rust/src/wire/mod.rs` re-exports new types;
  `rust/src/wire/adapters.rs` gains only `provenance_from_preservation` + 2
  unit tests.

Fidelity/effect taxonomy (single `classify_notice`):

| Fidelity | Codes | Effect |
|---|---|---|
| Exact | (empty) | — |
| WireNormalized | `image_detail_not_representable` | Rewritten |
| SemanticallyEquivalent | `freeform/deferred_tool_search_wrapped_as_function`, `tool_order_collapsed`, `reasoning_capability_uncertain` | Rewritten |
| Lossy | `metadata_*`, all `reasoning_*`, `structured_*`, `tool_call_id_*`, `audio/document_*`, all `cache_*/provider_extension_*`, `parallel_tool_calls_*`, `native_extension(s)_*`, unknown future codes | Omitted (only `refusal_not_representable` maps Rewritten) |
| Unsupported | `Err` only → single Blocked effect | Blocked |

Precedence `Unsupported > Lossy > Equivalent > Normalized > Exact`.
No fabrication: provider-owned signatures/encrypted reasoning/IDs/terminal
events never synthesized (request path emits no `Synthesized`).

Provenance bounds: `MAX_FRAGMENTS=32`, `MAX_NAME_BYTES=256`
(char-boundary), `MAX_TOTAL_BYTES=16KiB` (defense-in-depth; 32×256=8KiB max
reachable, pinned by headroom test), `MAX_DEPTH=8`. First-truncation-reason
wins; upstream `extensions_truncated`→`FieldBudget`. `Debug` counts-only.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo test --manifest-path rust/crates/eggpool-wire/Cargo.toml
```

### Results

Local (serial where required):

- `cargo fmt --check` — pass.
- `cargo clippy --workspace --all-targets -- -D warnings` — pass.
- `cargo check --workspace --all-targets --no-default-features` — pass.
- `cargo clippy --workspace --all-targets --no-default-features` — pass.
- `cargo test -p eggpool-wire` — 31 passed, 0 failed; doc-tests 0.
- Root focused: `wire_extraction_contract` 14, `wire_kernel_boundary` 3
  (extended to new facade/crate modules + impl markers), `wire_codecs` 11,
  `wire_stream` 18 — all pass; `adapters` lib 2 pass.
- Planner/encoder agreement — strict on all 13 fixtures × 5 surfaces
  (effect codes == notice codes in order; Unsupported ⇔ encode Err).
- Full default workspace suite — 777 passed, 0 failed.
- Full `--no-default-features` workspace suite — 778 passed, 0 failed.
- `cargo deny check` — advisories/bans/licenses/sources ok.
- `cargo build --locked --release` — pass.
- `cargo tree -e features` / `--duplicates` — ran; crate subtree still only
  serde/serde_json/sha2/thiserror/toml (+transitive); no new binary.
- Package-only doc tests — 0 (no doctest examples with runtime deps; API
  examples compile without Tokio/HTTP/config/provider clients by
  construction — no such imports in crate).
- No Python tooling change; `uv`/Ruff/Pyright/pytest not re-run. CI remains
  hosted truth; no CI run claimed here.

## 5. Invariant review

- All M002/M003 invariants remain binding and green via the same corpus +
  full suites (777/778).
- Semantic IR stays provider-neutral (no arbitrary JSON bag; provenance is
  the separate bounded object).
- Provenance bounded, separately typed, redaction-safe `Debug`, never
  persisted/logged by EggPool (EggPool only builds it transiently via the
  additive adapter; no persistence/logging call added).
- One semantic decision engine: planner calls `request_notices` +
  `native_summary_notices` plus a dry-run `encode_request` (same functions
  codecs use); preflight can never claim exactness when encoding warns (proven
  by agreement tests, including the Responses-metadata gap the dry run closed).
- `LossPolicy::Warn/Reject` stable (same notices through
  `apply_adaptation_policy`; compat asserted).
- Unknown/native fields never silently dropped on exact paths (exact requires
  empty notices + complete provenance).
- Unsupported native items stay blockers (native summary `Err` → Unsupported/
  Blocked; never converted to text/functions).
- No fabricated signatures/encrypted reasoning/IDs/terminal events (no
  `Synthesized` on request path; stream terminal evidence strict).
- Native same-surface forwarding preferred where required (restore contract
  + unchanged runtime path).
- Transport EOF distinct from success (conformance EOF vectors).
- Provenance/plan limits deterministic and attacker-bounded (ceilings +
  truncation facts, never silent discard-while-exact).

## 6. Failure and recovery review

Planner/provenance API is synchronous and pure; EggPool
cancellation/retry/restart semantics unchanged. Provenance lifetime follows
the request/response object (owned value; dropping releases fragments). No
background task, global cache, lock, I/O, or persistence. On budget
exhaustion: explicit truncation/incompleteness facts (`Truncated` +
`TruncationReason`); exactness is refused (`may_restore_exact` false).
Coordinator boundary/finalization/publication suites remain green via the
full workspace runs.

## 7. Migration and compatibility review

Additive for the crate; compatibility-preserving for EggPool. EggPool keeps
calling existing compat methods (delegating internally where noted); routing
does not select on `Fidelity`. Kept: `AdaptationNotice` values, `LossPolicy`
semantics, `CodecReasonCode` categories, native preservation/rejection
decisions, `WireSurface`/`WireCodecId` strings, terminal semantics. No
config, persistence, or wire migration. No publication or repository split.

## 8. Security review

No auth change. Provenance carries no credentials and retains no raw
payloads (counts/shapes/names only, bounded). `Debug` redaction-tested with
32 extensions (names absent). No new persistence/logging of provenance.
DoS bounds: provenance ceilings + existing decode/stream ceilings;
deterministic and immune to unbounded growth. `cargo deny` passes.

## 9. Documentation and operations

- Crate-level rustdoc + examples: semantic IR vs provenance vs
  adaptation/fidelity; guarantees/non-guarantees; limits; MSRV; no-I/O;
  unsupported semantics; comparison boundary (protocol kernel, not
  SDK/client/router/agent).
- `architecture/deep-dive-transcoder.md`: same three-layer model + EggPool
  ownership + comparison boundary + no-routing-on-fidelity rule.
- `architecture/deep-dive-request-lifecycle.md`: unchanged (provenance
  lifetime follows request/response; no ownership description change needed).
- No publication badges/claims (still `publish = false`).

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | `Approximated` effect class reserved but unemitted (codecs drop rather than remap reasoning controls) | Taxonomy has an unused variant | Later codec work may emit it or remove it; no behavior impact |
| low | `is_client_tool_search_declaration` predicate still duplicated (kernel `decode` vs EggPool preservation) | Carried from M002 | Unify under provenance vocabulary in a later pass if touched; no behavior risk |

No medium-or-higher findings. No external publication required for closure.

## 11. Roadmap disposition

Milestone closed. The request-admission-wire roadmap M002–M004 sequence is
complete: EggPool owns the same admission/runtime boundaries, consumes one
canonical codec implementation from `eggpool-wire`, preserves every existing
public wire capability, and carries no unresolved medium-or-higher
compatibility finding. No blocked plan is promoted by this closure (M004 was
the terminal milestone of the sequence; provider-transport M002 remains
blocked on upstream Eggfetch API work, unaffected).

## 12. Registry updates

Changes applied in the same commit:

- `plans/implementation/request-admission-wire/004-...md`: `active` → `closed`.
- `plans/registry.md`: M004 ready → closed; current milestone reflects
  sequence completion; unblock audit notes no newly-ready plans.
- `plans/subsystems/request-admission-wire-roadmap.md`: M004 ready → closed.
