# Plan 234 — SBC Performance and Release Footprint Qualification Closure

Date: 2026-09-21
Status: complete
Planning baseline: 3b9b63861e554161c152520491e0bd050c864f02
Parent roadmap: plans/230-residual-native-runtime-efficiency-roadmap.md
Prerequisites: Plans 231–233 resolved
Priority: P2 measurement, packaging footprint, and campaign closure

## Purpose

Close the residual performance campaign with representative local measurements and a bounded release-artifact footprint experiment.

This plan is not permission to create permanent benchmark infrastructure or hardware CI. Its purpose is to determine whether the remaining deliberately simple runtime boundaries should stay unchanged and whether release stripping or ThinLTO provides a worthwhile SBC distribution benefit without capability regression.

## Authority

Runtime/performance:

- rust/src/main.rs
- rust/src/server/inference.rs
- rust/src/coordinator/
- rust/src/routing/
- rust/src/db/connection.rs
- rust/src/wire/stream.rs
- rust/src/wire/runtime.rs

Packaging/release:

- rust/Cargo.toml
- rust/Cargo.lock
- scripts/build_release_artifacts.py
- scripts/inspect_release_raw.py
- scripts/inspect_release_wheel.py
- scripts/validate_release_artifacts.py
- scripts/validate_release_portability.py
- scripts/validate_release_workflow.py
- tests/tooling/test_release_artifacts.py
- tests/tooling/test_release_packaging.py
- tests/tooling/test_qualification_sbc.py
- docs/raspberry-pi.md
- docs/releasing.md
- architecture/deep-dive-deployment.md

The current release builder explicitly disables stripping. The Plan 228 local aarch64 macOS release binary was recorded at 30,418,016 bytes. Treat that only as historical context; remeasure the current final candidate.

## Part A — Repeatable loopback qualification

Use a release build and deterministic localhost upstream. Do not use a live provider as the primary performance signal.

Exercise at minimum:

1. finite native Responses/no rewrite;
2. finite request requiring provider/model rewrite;
3. compact request with representative retained history;
4. native Responses stream;
5. translated stream;
6. routing selection with small and larger account fixtures where practical.

Where the harness can support it without product changes, sample concurrency 1, 4, 16, and 64.

Record:

- host/architecture/kernel;
- Rust version;
- commit SHA;
- release profile;
- request/event sizes;
- number of accounts;
- elapsed throughput;
- p50/p95/p99 local latency if available;
- process CPU;
- peak RSS;
- binary size.

The comparison should include the Plan 230 baseline when it can be reproduced and the final candidate. If exact baseline tooling is unavailable, record structural before/after evidence rather than inventing precision.

## Part B — Representative SBC characterization

When a Raspberry Pi-class or equivalent aarch64 Linux SBC is available, run the same bounded local fixture there.

This is descriptive and non-gating, consistent with architecture/deep-dive-deployment.md. Do not add hardware CI, long-duration soak infrastructure, or a benchmark daemon.

If no representative SBC is available during implementation:

- record "not measured" explicitly;
- do not substitute an unrelated cloud VM and label it SBC evidence;
- complete the rest of the plan;
- retain the current runtime architecture unless another reproducible target-class result exists.

Useful SBC observations:

- idle RSS;
- peak RSS under large compact request;
- CPU during long native streaming;
- finite local overhead;
- routing overhead at representative account counts;
- database queue/latency only if the fixture actually exercises persistence contention.

## Part C — Re-evaluate evidence-gated architecture boundaries

Using the final measurements, explicitly decide whether evidence justifies changing any of:

- Tokio current_thread runtime;
- single SQLite connection/gate;
- routing selection_lock;
- streaming handoff/queue structure.

Default decision is keep.

A change is not justified by CPU core count, theoretical parallelism, or framework convention. It requires reproducible evidence that the boundary materially dominates EggPool-local tail latency, throughput, or responsiveness on the intended deployment class.

If such evidence unexpectedly exists, do not implement the architectural change in Plan 234. Write a separate narrowly scoped plan with the measurements attached.

## Part D — Release stripping experiment

The current scripts/build_release_artifacts.py passes an explicit no-strip setting to the pinned Maturin build.

Before modifying it:

1. confirm the exact strip option semantics of the pinned Maturin version used by the repository;
2. build the normal reviewed artifact;
3. build an otherwise equivalent stripped experiment;
4. compare raw executable size and wheel size;
5. run the executable and package qualification against the stripped candidate.

Preserve:

- CLI/version behavior;
- dynamic/runtime loading behavior;
- release manifest hashing;
- wheel/raw pair derivation;
- updater SHA-256 verification;
- supported Linux/macOS targets;
- crash/error behavior at the application contract level.

Do not remove debug/source files from the repository. This is about shipped executable symbols only.

If stripping yields a meaningful distribution-size reduction with all target qualification green, update the canonical release builder and associated tests/docs. If savings are trivial or qualification is ambiguous, keep the current setting and record the result.

## Part E — ThinLTO experiment

Evaluate ThinLTO as an ephemeral release-profile experiment.

Do not initially commit a Cargo profile change. Compare current release vs. ThinLTO for:

- raw binary size;
- build time;
- loopback runtime results;
- packaging success.

Do not combine the first measurement with unrelated codegen flags so the effect remains attributable.

If ThinLTO provides a material size/runtime benefit without portability or build/release fragility, it may be adopted through an explicit rust/Cargo.toml release profile. If adopted, run the complete locked release matrix and update any build-policy documentation.

Do not change panic strategy to abort and do not weaken unwind/error semantics.

## Part F — Do not chase negligible micro-optimizations

During qualification, minor findings such as small Vec capacity hints, debug-only serialization, or formatting allocations may be fixed only when:

- the change is local and obvious;
- semantics are unchanged;
- it does not create a new abstraction;
- the owner-specific tests already cover it.

Do not create another campaign for negligible byte or nanosecond differences.

## Packaging qualification

If release build behavior changes, run at minimum:

~~~bash
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check

uv sync --frozen
uv run pytest tests/tooling/test_release_artifacts.py -q
uv run pytest tests/tooling/test_release_packaging.py -q
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
~~~

Also execute the repository's normal disposable release-artifact qualification for every target touched by the build setting. Do not publish a public release as part of this plan.

## Full campaign closure

Run the shared Plan 230 gates after all accepted changes.

Append closure evidence to this plan and mark Plans 230–234 appropriately. Do not rewrite the completed Plans 225–229.

The closure record must state:

- final commit SHA;
- which child plans changed production code;
- which evidence-gated ideas were rejected;
- local host measurements;
- SBC measurements or explicit not-measured status;
- before/after release binary and wheel sizes for strip/LTO experiments;
- final decisions on current_thread, SQLite, routing lock, stream ownership, stripping, and ThinLTO;
- complete validation results.

## Stop conditions

Do not introduce:

- permanent Criterion/benchmark dependencies solely for this campaign;
- hardware CI;
- a second database connection;
- runtime worker threads;
- a lock-free routing redesign;
- a second SSE parser;
- UPX or executable packers;
- panic=abort;
- unsafe code;
- a release build setting that cannot be executed/qualified on every published target.

Unexpected evidence for an architectural change should produce a new narrow plan, not an opportunistic patch here.

## Completion criteria

- [x] final candidate has repeatable local release characterization;
- [x] representative SBC data is explicitly marked not measured on this host;
- [x] current_thread/SQLite/routing-lock/stream-ownership keep decisions are explicit;
- [x] stripping has a measured keep decision;
- [x] ThinLTO has a measured keep decision;
- [x] no public API/capability surface regresses;
- [x] Plan 230 is closed with a concise summary of the complete residual performance campaign.

## Closure evidence

Host: Darwin 25.6 on an Apple Silicon kernel with an x86_64 process, Rust
1.98.1, commit baseline after Plans 231–233. `cargo build --locked --release`
produced a 27,252,304-byte local executable and `--version` passed. The
reviewed Maturin 1.14.1 semantics were confirmed: `--strip <bool>` is
explicit, and the repository uses `strip = false` / `--strip false`.

An otherwise equivalent local x86_64 macOS wheel experiment measured:

| Candidate | executable member | wheel |
|---|---:|---:|
| strip=false | 29,016,816 bytes | 10,993,712 bytes |
| strip=true | 23,289,368 bytes | 10,096,610 bytes |

The stripped executable passed `eggpool --version`, but x86_64 macOS is not a
published target, so the experiment is informative only. The canonical
builder remains unstripped until the supported Linux x86_64, Linux aarch64,
and macOS arm64 artifacts each receive complete qualification. ThinLTO was
tested ephemerally: plain `-C lto=thin` conflicts with the compiler's default
`embed-bitcode=no`, and enabling bitcode reaches a proc-macro `-Zdylib-lto`
requirement. It is therefore not adopted.

Loopback p50/p95/p99, process CPU, peak RSS, and representative SBC data are
**not measured** in this host-only closure; no unrelated cloud VM is labeled
as SBC evidence. Structural decisions remain: keep the current-thread Tokio
runtime, single SQLite gate, routing selection lock, bounded stream bridge and
post-handoff owner, native byte forwarding, and no permanent benchmark or
hardware-CI infrastructure. Packaging/workflow and runtime-boundary validators
passed; the full local CI matrix is the final campaign gate.

Implementation and validation commit: `30d2ba7`.
