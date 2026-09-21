# Plan 230 — Residual Native Runtime Efficiency Roadmap

Date: 2026-09-21
Status: complete
Planning baseline: 3b9b63861e554161c152520491e0bd050c864f02
Parent context: completed Plans 225–229
Priority: P1 residual allocation/CPU efficiency with evidence-gated streaming and packaging follow-up

## Purpose

Continue performance work only where the current Rust runtime still performs demonstrably redundant local work, without reopening the architectural decisions closed by Plans 225–229.

The previous campaign already established the important production invariants:

- one bounded parse/depth-check at the inference endpoint;
- owned Bytes reuse for unchanged native request dispatch;
- borrowed synchronous AttemptPreparation before the provider-I/O await;
- immutable provider/account client topology with atomic close;
- a single serialized SQLite authority;
- current-thread Tokio runtime;
- bounded streaming handoff and terminal ownership;
- routing selection serialized only across synchronous in-memory claim work.

This roadmap therefore does not authorize another broad concurrency rewrite. It targets residual heap churn and repeated semantic materialization that remain visible in the current source.

## Current findings

### Definite low-risk allocation defects

1. rust/src/request/admission.rs — CompactAdmittedRequest::routing_facts constructs a temporary AdmittedRequest and clones NativeRequestPreservation, including the complete preserved serde_json::Value, even though routing_request_facts does not read native preservation.
2. rust/src/routing/eligibility.rs — build_eligible_candidates chooses the capability policy inside the account loop by cloning one BTreeMap per account. Routing also constructs several transient String/BTreeMap collections to feed scoring and then clones candidates back out.

These are local ownership defects. They can be corrected without changing HTTP, CLI, database, provider, routing-policy, or public request semantics.

### Evidence-gated residuals

3. Compact production execution retains both AdmittedRequest and CompactAdmittedRequest views, so the source-native preservation tree is cloned again when FiniteRequest::from_compact_admitted is built. Plan 229 deliberately retained that clone to preserve the public FiniteRequest shape. Production may be able to bypass the duplicate through a crate-private compact execution representation while leaving every public type and constructor intact.
4. Native Responses streaming forwards the original provider Bytes but still runs frames through canonical stream decoding and returns Vec<CanonicalEvent> values that are used only for terminal/accounting observation. A shared observer/fold path may reduce event/vector allocation, but only after equivalent terminal/error/usage behavior is proven.
5. Release artifacts are built unstripped and the last recorded release binary was about 30.4 MiB. Stripping and ThinLTO are reasonable experiments for SBC distribution, but they must remain qualification-driven and must not change runtime contracts.

## Non-goals

This campaign does not authorize:

- a multithread Tokio runtime;
- a second SQLite connection or pool;
- lock-free routing/claims;
- a new streaming queue or whole-stream buffer;
- a second SSE parser;
- a different JSON library;
- removal of serde_json preserve_order;
- provider transport replacement;
- Eggfetch/Eggress redesign;
- changing retry, health, quota, fairness, or routing policy;
- changing public HTTP endpoints, payloads, CLI commands, config keys, error mappings, or Rust API types;
- permanent benchmark-framework dependencies.

Plans 225–229 remain historical authority for why these boundaries are intentionally simple.

## Execution order

Implement in this order:

1. Plan 231 — Compact routing and selection allocation cleanup.
2. Plan 232 — Compact production dual-view memory cleanup, only if the production-only internal path can preserve the public FiniteRequest/CompactAdmittedRequest contract.
3. Plan 233 — Native stream observer efficiency, only after a repeatable stream workload shows local observer CPU/allocation is material.
4. Plan 234 — SBC performance and release-footprint qualification/closure.

Plan 231 is unconditional. Plans 232–234 are evidence-gated and may correctly close with no production change when their stop conditions are met.

## Shared measurement rules

Use release builds and a localhost deterministic upstream when measuring EggPool-local overhead. Do not use live provider latency as the primary comparison because network/provider variance hides local CPU and allocation effects.

Representative cases should include:

- finite OpenAI Responses native/no-rewrite;
- finite provider-qualified or virtual model rewrite;
- remote compaction with small and large input histories;
- native Responses SSE;
- translated SSE;
- routing with 1, 4, and 16 configured accounts where fixtures can represent them;
- concurrency levels 1, 4, 16, and 64 when practical.

Measure only dimensions available on the host:

- requests/s or fixed-work elapsed time;
- p50/p95/p99 local latency when the harness can isolate it;
- process CPU;
- peak RSS;
- output/body identity and byte counts;
- release binary size.

Do not establish arbitrary pass/fail percentages before seeing a baseline. A structural removal of an O(request-tree-size) deep clone is sufficient evidence for Plan 231 even if wall-clock noise masks the improvement.

## Compatibility invariants

Every child plan must preserve:

- one endpoint parse/depth boundary;
- stateless Responses validation;
- native source-envelope preservation;
- byte-exact native forwarding when no EggPool-owned field changes;
- exact model/provider-qualified/virtual resolution;
- provider/account eligibility, quota, health, fairness, retry, and claim ordering;
- native unknown SSE event preservation;
- terminal evidence and EOF-not-success behavior;
- request/attempt/finalization durable ownership;
- public Rust constructors/types unless a separate versioned API decision explicitly approves a break.

## Shared qualification

At campaign closure run:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1

git diff --check
~~~

Run narrower owner-specific targets in each child plan before the full suite.

## Completion criteria

This roadmap is complete when:

- Plan 231 removes the known redundant compact-routing and capability-policy deep clones without semantic change;
- Plan 232 either removes duplicate production compact preservation ownership behind an internal boundary or records reproducible evidence that doing so would require a public API change disproportionate to the measured benefit;
- Plan 233 either reduces native observer allocation while proving exact stream semantics or records evidence that observer cost is immaterial;
- Plan 234 records target-class measurements and makes an explicit keep/change decision for release stripping/LTO;
- no child introduces a new concurrency authority, persistence authority, transport stack, parser family, or public API regression;
- the final documentation states which residual boundaries remain intentionally unchanged.

## Handoff note

The correct result of this campaign may be small. EggPool is already past the stage where architectural complexity is justified by speculative throughput. Remove obvious ownership waste, measure the remaining hot spots, and stop when evidence no longer supports additional machinery.

## Closure summary

Plans 231–233 removed the proven compact routing and capability-policy clones,
gave production compaction a single preserved-tree owner behind a private
finite-input boundary, and replaced the native Responses stream's discarded
canonical-event batch with a shared decoder observation sink. Plan 234 retained
the current-thread runtime, SQLite gate, routing selection lock, bounded stream
handoff, unstripped reviewed release profile, and no ThinLTO after local
qualification. SBC and loopback percentile data were not measured on the
available host; this campaign added no benchmark framework, hardware CI,
second parser, or concurrency authority.
