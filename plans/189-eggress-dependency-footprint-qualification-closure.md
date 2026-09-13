# Plan 189: Eggress dependency and footprint qualification closure

> **Status:** complete (verified 2026-09-13; SSH facade exception documented)
>
> **Parent:** Plan 186 — Eggress embed consolidation roadmap
>
> **Depends on:** Plans 187 and 188
>
> **Phase:** 3 of 3
>
> **Scope:** Remove obsolete direct production Eggress dependencies, minimize only provably redundant features, qualify the final proxy behavior, and measure dependency/binary-footprint effects using comparable builds.

## Problem statement

Plans 187 and 188 change the ownership boundary: production proxy-chain construction moves behind `eggress-embed`, while the deterministic custom-root path and protocol fixtures become explicitly test-scoped.

The final step is not simply to delete dependency lines. `eggress-embed` legitimately pulls several Eggress implementation crates transitively, and the integration suite legitimately uses protocol crates to create real peers. The closure task must distinguish:

- obsolete direct production dependencies;
- intentional optional `test-support` dependencies;
- intentional dev-only fixture dependencies;
- transitive implementation crates owned by the stable facade;
- feature activations that are redundant versus feature activations required by Eggpool's supported proxy corpus.

Binary-size improvement must also be measured rather than inferred. Moving a crate from direct to transitive ownership improves maintenance boundaries even when the linker still retains much of the same code.

## Desired end state

The final manifest/source relationship should satisfy all of the following:

1. `eggress-embed` is the only Eggress crate ordinary provider-transport source imports directly.
2. Implementation crates needed by the deterministic custom-root adapter are optional and activated only by `test-support`.
3. Protocol/core crates used only to create integration peers are dev dependencies.
4. All Eggress crates explicitly selected by Eggpool use the same `1.0.6` release line.
5. Default features remain disabled on `eggress-embed`; Eggpool opts into only the capabilities required by its supported pproxy surface.
6. No feature is removed unless the existing protocol qualification proves equivalent behavior.
7. Before/after release size and dependency evidence is recorded with the same toolchain/profile.

## Workstream A — classify every direct Eggress dependency

Audit `rust/Cargo.toml` after Plans 187-188 and classify each remaining `eggress-*` declaration into one of four buckets:

### A1. Production facade

Expected:

- `eggress-embed`

This is the stable boundary for normal provider proxy construction.

### A2. Optional test-support implementation dependency

Retain only crates that the feature-gated custom-root adapter still imports directly. Each retained declaration must:

- be `optional = true` where applicable;
- be activated only by `test-support`;
- have a short comment explaining the deterministic verified proxy-TLS fixture requirement;
- remain pinned to the same Eggress release as the facade.

### A3. Dev-only fixture dependency

Keep protocol/core crates under `[dev-dependencies]` when they are used to build real local interoperability fixtures. Examples may include Shadowsocks/SSR and Trojan protocol support or low-level stream/target types used only by `rust/tests/provider_transport.rs`.

Do not promote fixture dependencies into normal dependencies.

### A4. Obsolete dependency

Delete any direct Eggress dependency with no remaining direct source use after the boundary refactor.

Prove removals using both source search and Cargo compilation; do not rely on visual manifest inspection alone.

## Workstream B — audit `eggress-embed` feature activation

The current Eggpool feature set is:

```toml
features = [
    "pproxy-compat",
    "extended",
    "pproxy-legacy",
    "legacy-crypto",
    "ssh",
]
```

Eggress `1.0.6` defines the relevant relationships such that:

- `pproxy-compat` exposes the pproxy compatibility constructor used by Eggpool;
- `ssh` enables SSH transport and the pproxy compatibility SSH path;
- `pproxy-legacy` enables `extended` plus legacy pproxy Shadowsocks behavior;
- `legacy-crypto` enables `extended` plus legacy Shadowsocks crypto;
- `extended` enables the extended protocol implementation set including Shadowsocks/Trojan-related server/runtime support.

Because `pproxy-legacy` and `legacy-crypto` already imply `extended`, the explicit `extended` feature is structurally redundant if either stronger feature remains enabled. Removing the redundant feature name is appropriate after the live protocol suite proves no feature-resolution surprise.

Do **not** assume `pproxy-legacy` or `legacy-crypto` themselves are redundant. Eggpool's supported corpus includes SSR and legacy compatibility behavior. Test first.

### Required procedure

1. Capture the post-Plan-188 feature graph:

   ```bash
   cargo tree --manifest-path rust/Cargo.toml -e features -i eggress-embed
   cargo tree --manifest-path rust/Cargo.toml -e features -i eggress-runtime
   cargo tree --manifest-path rust/Cargo.toml -e features -i eggress-server
   ```

2. Remove only feature entries whose activation is already implied and whose removal does not change the resolved feature graph.

3. Run the full provider transport suite after each meaningful feature reduction.

4. Do not enable Eggress `full`, default features, `operations`, `reverse`, `quic`, or other unrelated surfaces merely to make a test pass. Investigate the exact missing capability instead.

The target is **smallest feature set consistent with current Eggpool behavior**, not smallest possible Eggress build after deleting behavior.

## Workstream C — verify source ownership

Run:

```bash
rg 'eggress_[A-Za-z0-9_]+' rust/src rust/tests
```

Classify every result:

- production source should use `eggress_embed` only;
- test-support implementation-crate uses must be visibly feature-gated;
- integration-test protocol/core uses must correspond to dev dependencies.

Also inspect the provider module specifically:

```bash
rg 'eggress_' rust/src/providers
```

Any ordinary production import other than `eggress_embed` is a closure blocker unless the implementation notes document a specific facade gap that cannot reasonably be avoided.

Do not hide a dependency behind a local re-export simply to satisfy this search. Ownership, not spelling, is the goal.

## Workstream D — dependency-tree qualification

Capture and compare the dependency graph after pruning:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -d
```

Use inverse queries for the major Eggress crates:

```bash
cargo tree --manifest-path rust/Cargo.toml -i eggress-embed
cargo tree --manifest-path rust/Cargo.toml -i eggress-core
cargo tree --manifest-path rust/Cargo.toml -i eggress-config
cargo tree --manifest-path rust/Cargo.toml -i eggress-server
cargo tree --manifest-path rust/Cargo.toml -i eggress-pproxy-compat
cargo tree --manifest-path rust/Cargo.toml -i eggress-transport-ssh
```

Evaluate these results by dependency path:

- `eggpool -> eggress-embed -> ...` is expected facade ownership;
- `eggpool -> optional test-support implementation crate` is acceptable only when documented by Plan 188;
- an unconditional direct `eggpool -> implementation crate` edge is a closure failure unless explicitly justified.

Do not make Cargo.lock crate count a success metric by itself.

## Workstream E — same-profile footprint measurement

Measure the actual executable before declaring a footprint win.

### E1. Use comparable builds

The baseline and final measurements must use:

- the same Rust toolchain;
- the same target triple;
- the same release profile;
- the same feature set;
- the same stripping/LTO settings;
- a clean/reproducible build environment when practical.

If Plan 187 captured a pre-migration baseline, reuse it. Otherwise reconstruct the baseline from the pre-Plan-187 commit with the same toolchain rather than comparing unrelated release artifacts.

### E2. Record executable size

At minimum:

```bash
cargo build --manifest-path rust/Cargo.toml --release
ls -lh rust/target/release/eggpool
```

Use the repository's actual release packaging/strip procedure if it differs from a raw Cargo release build. Compare like with like.

### E3. Attribute crate contribution when tooling is available

If `cargo-bloat` is already installed on the implementation host, run:

```bash
cargo bloat --manifest-path rust/Cargo.toml --release --crates
```

Do not add `cargo-bloat` as a project dependency or CI requirement for this plan.

If it is unavailable, binary size plus `cargo tree -e features` is sufficient closure evidence.

### E4. Interpret results honestly

Expected possibilities:

- **binary smaller:** record the delta and likely removed feature/code contributors;
- **binary roughly unchanged:** still accept the migration if direct ownership/source complexity materially decreased and behavior is preserved;
- **binary larger:** identify whether the cause is the Eggress 1.0.6 upgrade, feature activation, duplicate versions, or an accidental default-feature expansion before closing.

A maintenance-boundary win is valid even if most implementation code remains transitively linked, but a meaningful unexplained binary regression is not.

## Workstream F — protocol and failure qualification

The final dependency/feature state must pass the real proxy matrix, not only compile.

Mandatory behavioral gates from `rust/tests/provider_transport.rs` include:

- mandatory pproxy URI construction corpus;
- explicit `direct://` operation;
- live SOCKS/HTTP proxy behavior covered by current tests;
- live Shadowsocks transport;
- live SSR transport;
- Trojan verified with the deterministic test CA;
- Trojan rejection without the custom root;
- SSH success;
- SSH authentication failure classification;
- SSH timeout/cancellation behavior;
- ordered HTTP -> SOCKS5 multi-hop transport;
- no proxy-to-direct fallback;
- credential redaction;
- provider/account client isolation;
- preserved pool/timeout settings.

Any feature minimization that breaks one of these behaviors must be reverted unless the repository separately decides to remove that protocol from Eggpool's supported contract.

## Workstream G — documentation and closure evidence

Review documentation/architecture files for statements that describe Eggpool as directly owning Eggress chain parsing/execution. Update only stale statements introduced by the new boundary.

Add a concise changelog entry if the repository's release convention records internal dependency upgrades. Do not advertise new proxy functionality if externally visible behavior is intentionally unchanged.

Before marking this plan complete, append a short `## Closure evidence` section to this file containing:

- Eggress version adopted;
- final direct `eggress-*` dependency classification;
- final `eggress-embed` feature list;
- targeted/full test results;
- before/after release binary sizes, if reproducibly measurable;
- whether the custom-root implementation seam remains and why;
- any deferred upstream Eggress API opportunity.

This makes the dependency decision auditable without reconstructing it from commit history.

## Mandatory verification

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --all-targets
cargo check --manifest-path rust/Cargo.toml --all-targets --features test-support
cargo check --manifest-path rust/Cargo.toml --all-targets --all-features
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport
cargo test --manifest-path rust/Cargo.toml --all-features
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -d
```

Then perform the same-profile release measurement described above.

## Completion criteria

Plan 189 is complete when:

- obsolete unconditional direct Eggress implementation dependencies are removed;
- remaining optional/test/dev dependencies have explicit ownership reasons;
- normal provider source uses only `eggress-embed` as the Eggress API boundary;
- all explicit Eggress pins are on the same qualified release line;
- the embed feature set contains no known redundant explicit activations;
- protocol behavior and failure isolation remain fully qualified;
- dependency tree and release footprint are measured and interpreted;
- any binary-size regression is understood rather than ignored;
- closure evidence is appended to this plan;
- Plan 186's roadmap criteria are satisfied.

## Non-goals

Do not in this phase:

- remove supported proxy protocols to make the binary smaller;
- replace live interoperability tests with mocks;
- eliminate crates that are legitimately transitive through `eggress-embed`;
- introduce workspace patches/path overrides for Eggress;
- broaden the audit into unrelated Eggpool dependencies;
- replace the provider HTTP client with Eggfetch;
- replace Axum with Eggserve;
- add a new benchmarking dependency or CI burden;
- claim a size reduction without comparable measurements.

## Approval checklist

- [x] Every direct Eggress dependency classified as production, optional test-support, dev fixture, or removed.
- [x] Ordinary provider source imports only `eggress-embed` for Eggress functionality, with the documented 1.0.6 SSH facade fallback exception.
- [x] Test-support internals remain isolated and documented.
- [x] All explicit Eggress crates use one release line.
- [x] Feature graph audited and redundant explicit activation removed where safe.
- [x] No unrelated Eggress full/default features accidentally enabled.
- [x] Full proxy interoperability/failure suite passes.
- [x] Full Rust fmt/check/clippy/test qualification passes.
- [x] Same-profile release footprint comparison recorded.
- [x] Any size regression explained or corrected; the measured result is a 525,184-byte reduction.
- [x] Closure evidence appended.
- [x] Plan 186 is marked complete.

## Closure evidence

Verified 2026-09-13 at exact implementation/closure head `d648df8`. Direct Eggress declarations are all on `1.0.6`; the
explicit `extended` facade feature was removed because it is implied by the
retained legacy compatibility features. `cargo tree -e features`, inverse
queries for the major Eggress crates, and duplicate-version inspection show a
single 1.0.6 Eggress line. The resolved graph retains implementation crates
transitively through `eggress-embed` and directly through the documented
default `eggress-ssh-fallback`; this is intentional and not counted as a
facade failure.

Comparable release builds used the same host/toolchain/profile. The baseline
from the pre-change implementation head was `30,096,008` bytes and the final
binary was `29,570,824` bytes, a reduction of `525,184` bytes (`1.75%`).
`cargo-bloat --release --crates` reported approximately `18.3 MiB` baseline
versus `17.9 MiB` final `.text`. The normal dependency tree was 409 unique
entries at baseline and 410 final, demonstrating why the binary result is
recorded separately from manifest/source ownership.

The full proxy matrix and all-features suite passed. No later plan depends on
Plan 189; the only follow-up opportunity is an upstream Eggress release that
lets `OutboundConnector` accept/install an SSH session cache, at which point
the fallback and its direct implementation dependencies can be removed in a
separate corrective change.

Formal closure revalidated on 2026-09-13 at pre-closure head `18fa4dfe`:
default, `test-support`, and all-features checks passed; strict all-features
Clippy passed; the provider transport suite passed all 35 tests; the full
all-features Rust suite passed; the locked release build produced
`29,570,824` bytes; `cargo-bloat --release --crates` reported `17.9 MiB` of
`.text`; and `cargo deny` passed advisory, license, source, and duplicate
policy checks. No plans exist after 189, so there is no downstream plan to
unblock or update.
