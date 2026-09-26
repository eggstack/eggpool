# Request Admission and Wire Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/003-sans-io-wire-kernel-extraction-and-eggpool-cutover.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-003--sans-io-wire-kernel-extraction-and-eggpool-cutover`
- `plans/subsystems/request-admission-wire-roadmap.md#13-wire-kernel-extraction-extension`

Repository baseline reviewed: `61470ef788e287c49b4a51062eaba439049d46dc`

Implementation commits or pull requests:

- `5f373c98` — Implement request-admission-wire M003 crate extraction and cutover

## 1. Executive finding

M003 moves the M002-qualified pure wire kernel into one internal workspace
crate (`eggpool-wire 0.1.0`, `publish = false`) and cuts EggPool over to
that single implementation. Root `rust/src/wire/{ir, adaptation, codec,
codecs, additional_codecs, decode, registry, stream}.rs` are now 6–9-line
facades (`pub use eggpool_wire::...`) with zero definitions; `wire::adapters`
and `wire::runtime` remain EggPool-owned. All five upstream surface families
and all three public client endpoints retain current finite/streaming
behavior with type-identical canonical types (no JSON bridge). The crate has
no runtime/network/persistence dependency and remains unpublished. M004 is
unblocked.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Workspace crate `publish=false`, edition 2024, MSRV 1.89, `unsafe forbid`, minimal deps | `rust/crates/eggpool-wire/Cargo.toml`; `cargo tree -p eggpool-wire -e normal` shows only serde/serde_json/sha2/thiserror/toml | pass | No feature matrix; no tokio/axum/hyper/tls/sqlite/config/network |
| Source-of-truth move, no copy | Root 8 kernel files are facades (6–9 lines, all contain `eggpool_wire`, zero `pub struct/enum/fn/trait/const/type`); crate owns all markers | pass | `registry.rs` → `profile.rs` rename is the only file rename |
| Type identity, no serde bridge | Facades are `pub use` only; `adapters.rs`/`runtime.rs` resolve through facades to crate types; full suite green | pass | `pub(crate)`→`pub` widening on 3 native-observation items only (cross-crate visibility, no logic change) |
| EggPool adapters (catalog/routing/config/native ownership) | `wire::adapters` unchanged in ownership; `request::admission` still owns parse/stateless/tokens/routing projection | pass | Conversions operate on crate types without copying prompt/body content |
| M002 corpus consumable, no divergent copies | `wire_extraction_contract` 14/14; `wire_kernel_boundary` 3/3 (now scans crate sources + asserts facades) | pass | Root integration remains authoritative |
| Finite codecs + decoder single source | `codecs.rs`/`additional_codecs.rs`/`decode.rs` facades; crate tests 2/2; `wire_codecs` 11, `wire_multimodal` 7, `canonical_request` 12, Codex 15+13 | pass | Current error mapping + ordered notices preserved |
| Stream state machines single source | `stream.rs` facade; `wire_stream` 18/18 incl. chunk/UTF-8/terminal; `wire_runtime` 8/8 | pass | Native-forwarded bytes exactly as before (decoder untouched) |
| Root runtime/admission/routing/config/transport/lifecycle unchanged in ownership | `wire/runtime.rs` stays root-owned; coordinator boundary/finalization/publication + C008/C009/C011 green | pass | — |
| All surfaces + native preservation + compact unchanged | Focused suites (§4) + full workspace default 746 / no-default 747, zero failures | pass | — |
| Default/no-default + release + audit + trees | §4 commands all pass; locked release builds; single `rust/target/release/eggpool` binary | pass | No new binary/artifact |
| Docs | `architecture/deep-dive-transcoder.md` workspace split; `rust/README.md` internal-library note; canonical semantics unchanged | pass | `ir.rs` remains canonical facade path |

## 3. Production implementation evidence

Source-of-truth map:

| Kernel piece | Now owned by | Root remainder |
|---|---|---|
| Canonical IR + Presence/reasoning/tool/media/usage/event types | `eggpool-wire/src/ir.rs` | `wire/ir.rs` facade (canonical boundary path) |
| Neutral adaptation (`NativeSummaryFacts`, neutral capability, notices, loss policy, stable IDs) | `eggpool-wire/src/adaptation.rs` | `wire/adaptation.rs` facade; wrappers in `wire/adapters.rs` |
| Codec contract (`WireCodecId`, notices, errors, `WireCodec` trait, compat path) | `eggpool-wire/src/codec.rs` | `wire/codec.rs` facade |
| Chat/Messages finite codecs | `eggpool-wire/src/codecs.rs` | `wire/codecs.rs` facade |
| Responses/Gemini finite codecs | `eggpool-wire/src/additional_codecs.rs` | `wire/additional_codecs.rs` facade |
| Structural decoder + `DecodeLimits` + media validators | `eggpool-wire/src/decode.rs` | `wire/decode.rs` facade |
| Neutral profile vocabulary + static registry | `eggpool-wire/src/profile.rs` | `wire/registry.rs` facade (bridges `profile`) |
| SSE framing/event decode/client encode/usage/terminal/native observation | `eggpool-wire/src/stream.rs` | `wire/stream.rs` facade |
| EggPool joins | — (stays root) | `wire/adapters.rs`, `wire/runtime.rs`, `request/*`, `coordinator/*`, `config.rs` |

Manifest/lock: `rust/Cargo.toml` gains workspace member + path dep;
`Cargo.lock` regenerated normally. Package dependency list (exact):
`serde 1` (derive), `serde_json 1` (preserve_order), `sha2 0.10`,
`thiserror 2`, `toml 0.8` — each justified (codec/registry types, canonical
JSON, stable call IDs, typed errors, registry parsing). No other normal
dependency. Crate `cargo tree` confirms no runtime/network/persistence
packages. Only non-path change: `pub(crate)`→`pub` on
`NativeStreamObservation` + 2 methods (root runtime calls across crates).

Planned-but-absent (by design): no crates.io publication, no semver-1.0
promise, no repository split, no package rename, no new provider features,
no provenance/fidelity API (M004), no microcrate split, no optional
Tokio/Axum features, no release artifact count change.

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
- `cargo test -p eggpool-wire` — 2 passed, 0 failed; doc-tests 0.
- Focused root suites: `wire_extraction_contract` 14, `wire_kernel_boundary`
  3, `wire_codecs` 11, `wire_stream` 18, `wire_adaptation` 6,
  `wire_profiles` 9, `wire_runtime` 8, `wire_multimodal` 7,
  `wire_qualification` 16, `canonical_request` 12,
  `codex_responses_compat` 15, `codex_compaction_compat` 13 — all pass.
- Full default workspace suite — 746 passed, 0 failed.
- Full `--no-default-features` workspace suite — 747 passed, 0 failed.
- `cargo deny check` — advisories/bans/licenses/sources ok.
- `cargo build --locked --release` — pass; single binary
  `rust/target/release/eggpool` confirmed; no new binary/artifact.
- `cargo tree -e features` / `--duplicates` — ran; crate subtree minimal;
  duplicates limited to pre-existing `webpki-roots`/`universal-hash` style
  entries; no second codec implementation in graph.
- No Python tooling change; `uv`/Ruff/Pyright/pytest not re-run. CI remains
  the hosted truth; no CI run claimed here.

## 5. Invariant review

- Exactly one implementation after cutover: facade line counts (6–9) + zero
  root definitions + crate ownership of all 25 implementation markers
  (guarded by `root_kernel_modules_are_facades_without_duplicate_implementations`).
- Canonical request identity type-identical across admission/routing/runtime/
  codecs/tests: facades re-export crate types; no JSON bridge (grep-proven).
- Native finite forwarding stays on preservation path; translated targets
  consult blockers/notices first: runtime path unchanged; contract + Codex
  suites green.
- Native streaming stays source-byte forwarding + shared decoder observation:
  stream state machine moved verbatim; `wire_stream`/`wire_runtime` green;
  no buffering/re-encoding added.
- `WireRuntime`, generation/request ownership, profile selection, compaction
  execution, routing/catalog facts, config reload, retries, transport,
  publication/finalization, HTTP mapping remain root-owned: only facades
  moved; `runtime.rs`/`adapters.rs`/`request`/`coordinator` ownership intact.
- Crate has no credential/env/fs/network/clock/random/async/db/logging side
  effects: dependency list + `cargo tree` prove it; forbidden-import scan of
  all 8 crate sources passes.
- MSRV 1.89 preserved (`rust-version = "1.89"` in both packages).
- `unsafe_code = "forbid"` in both packages.
- All M002 invariants remain binding and green via the same corpus.

## 6. Failure and recovery review

No new runtime work in the crate: stream decoder stateful only per
request/stream object, bounded, no tasks/sockets/retries/sleeps/clocks/env/
persistence. Parse/adaptation errors still map through EggPool
`WireRuntimeError`/`error.rs`; no crate-level HTTP status semantics added.
Cancellation/restart/contention behavior is exactly EggPool's current
behavior (coordinator boundary/finalization/publication + C008/C009/C011
green). Rollback: revert `5f373c98` restores in-tree kernel; lockfile
regenerates normally.

## 7. Migration and compatibility review

Internal code-location migration with zero public migration. Root module
re-exports preserve existing internal paths; serialized forms of
public/stored types unchanged (no new serialization promises); ordered
adaptation codes, typed error categories, `WireCodecId`/`WireSurface`
strings, native same-surface behavior, and compact semantics preserved
(contract + Codex + coordinator evidence). Release binary/package behavior
preserved (single binary confirmed). No external consumer change required
(`publish = false`). Rollback limits: revert the one commit.

## 8. Security review

Auth, secret handling, redaction, privilege bounds, DoS bounds unchanged.
Crate exposes no credentials, environment, filesystem, network, or logging;
`Debug` impls moved verbatim (redaction-safe). Bounded adaptation/SSE/marker
ceilings unchanged. `cargo deny` passes (license/advisory/source policy).

## 9. Documentation and operations

- `architecture/deep-dive-transcoder.md`: workspace ownership split
  (crate deps, facades, adapters/runtime/admission ownership, no new
  artifact).
- `architecture/README.md`/overview: no index change needed (paths preserved
  via facades).
- `rust/README.md`: internal workspace library note (shipped build, not
  independently published).
- Canonical long-term semantics unchanged; `rust/src/wire/ir.rs` documented
  as the EggPool canonical boundary facade over crate types.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Duplicate `is_client_tool_search_declaration` predicate (kernel `decode` vs EggPool preservation) carried into the crate/root split | Same bounded predicate in two owners | M004 provenance work to unify; no behavior risk |
| low | `MediaLimitError` (crate) vs `LimitError` (EggPool) mapping shim retained | Two error types, one mapping function | M004 or later crate-API pass to decide; no acceptance change |

No medium-or-higher compatibility or maintenance finding remains.

## 11. Roadmap disposition

Milestone closed; hard dependency for M004 satisfied. M004
(`004-fidelity-provenance-and-conformance-hardening.md`) may proceed against
the extracted crate and proven EggPool adapters — promote from `blocked` to
`ready` in the same commit. No provider-transport blocked work is promoted.

## 12. Registry updates

Changes applied in the same commit:

- `plans/implementation/request-admission-wire/003-...md`: `active` → `closed`.
- `plans/registry.md`: M003 ready → closed; M004 blocked → ready; unblock
  audit notes M003 closure unblocking M004.
- `plans/subsystems/request-admission-wire-roadmap.md`: M003 ready → closed;
  M004 blocked → ready.
