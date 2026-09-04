# EggPool Rust Migration — Long-Term Roadmap

Status: active canonical roadmap

Planning baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

This roadmap orders the migration by correctness dependencies. It is deliberately not a calendar estimate.

## End-state dependency chain

```text
M0 Foundation + oracle
  -> M1 Config/CLI/filesystem
  -> M2 SQLite/repositories
  -> M3 HTTP read/control plane + SSR
  -> M4 Provider HTTP + Eggress outbound
  -> M5 Catalog/routing/quota/health
  -> M6 Canonical request + codecs/transcoding/SSE
  -> M7 Coordinator/retry/finalization
  -> M8 Runtime generations/rehash/background lifecycle
  -> M9 Full operational CLI/lifecycle/update/deploy
  -> M10 Differential qualification + SBC characterization
  -> M11 Rust cutover
  -> M12 Python retirement
```

Some later milestones may proceed in parallel against stable interfaces, but no milestone may bypass its correctness dependencies for the purpose of declaring closure.

## M0 — Migration foundation and behavioral oracle

Primary class: infrastructure/invariant

Purpose: make the rewrite measurable before implementing large amounts of Rust behavior.

Required outcomes:

- `rust/` Cargo build scaffold producing an isolated `eggpool` binary;
- migration-specific Rust coding/dependency rules;
- black-box runner capable of invoking Python and Rust candidates separately;
- contract inventory covering config, CLI, API, DB, SSR/static assets, and operational surfaces;
- explicit normalization policy for incidental differences;
- deterministic fixture server/database/config helpers;
- minimal Rust verification commands and no broad CI matrix.

Exit condition: an implementation agent can add one Rust behavior and prove whether it matches Python without manual ad hoc comparison.

## M1 — Configuration, CLI parser, and filesystem contract

Primary class: capability/invariant

Port configuration models, defaults, validation, env resolution, filesystem/path ownership, config initialization, and the full CLI command tree skeleton.

The milestone may stub commands whose underlying subsystems are not yet implemented, but parser/help/validation/exit behavior must be explicit and differential tests must distinguish implemented from intentionally unavailable internals.

Exit condition: supported config corpus acceptance/defaulting/path resolution and CLI parser/help/exit classes match the compatibility contract.

## M2 — SQLite schema, migrations, and repository layer

Primary class: infrastructure/invariant

Reuse the existing numbered SQL migrations and checksums. Implement serialized SQLite access, transaction semantics, repositories, crash/startup compatibility, and read/write fixtures.

Exit condition: Python-created databases can be opened and queried by Rust; Rust writes remain readable by Python within the supported rollback window; schema/checksum behavior is identical.

## M3 — HTTP read/control plane and SSR dashboard

Primary class: capability

Introduce Tokio/Axum inbound HTTP, auth middleware, body limits, health/readiness, read-only stats/model-info/control endpoints that have dependencies available, SSR rendering, static asset copy/manifest, and dashboard routing.

Exit condition: the existing dashboard surfaces can be exercised against Rust without redesign, and the selected read/control APIs pass differential tests.

## M4 — Provider HTTP stack and Eggress outbound proxy integration

Primary class: infrastructure/capability

Subsystem roadmap: [Provider Transport](subsystems/provider-transport-roadmap.md).

Implement provider/account HTTP connection pools on Hyper/Hyper-util/Rustls and an Eggress-backed connector for per-account pproxy-style outbound URIs.

Qualify exactly the proxy URI/protocol features EggPool promises; use narrow Eggress features and fail closed for unsupported forms.

The completed implementation sequence is T001 contract/fixture freeze -> T002 direct Hyper/Rustls core -> T003 Eggress connector/proxy parity -> T004 provider/account client pool -> T005 differential qualification -> T006 extended proxy runtime corrective closure.

Exit condition: controlled direct and proxied provider HTTP fixtures match Python transport semantics and diagnostics. Satisfied after T006.

## M5 — Catalog, account registry, routing, quota, health, and model-router state

Primary class: capability/invariant

Subsystem roadmap: [Routing Domain and Catalog State](subsystems/routing-domain-roadmap.md).

Port deterministic domain logic before inference dispatch. Preserve eligibility, priority tiers, fairness, claims, durable backoffs, capability filtering, quarantine, catalog/model-info identity, and bounded affinity/learned state.

The implementation sequence is D001 contract/fixture freeze -> D002 account registry/catalog cache -> D003 catalog refresh/normalization/persistence -> D004 quota/claims/scoring plus D005 health/backoff/circuit/quarantine -> D006 eligibility/routing/fairness/local claims -> D007 model-router compilation/affinity -> D008 differential qualification/closure. D006 requires both D004 and D005. Only explicitly registered dependency-ready plans may move.

M5's local selection claim stops before durable inference persistence. Semantic model-router selector calls that invoke `RequestCoordinator` remain M7 work. Optional generic external catalog polling/background scheduling remains M8 work.

Exit condition: deterministic state snapshots produce parity-equivalent candidate sets, selections, exclusions, local claim ownership, and durable M5 effects under concurrency/restart tests.

## M6 — Canonical request boundary, wire codecs, transcoding, and SSE

Primary class: capability/invariant

Port request body limits/parsing, canonical source intent, OpenAI/Anthropic/Gemini codec behavior, reasoning controls, media/document limits, SSE framing/translation, usage extraction, and terminal evidence.

Exit condition: non-dispatch codec fixtures and stream traces match Python's supported client/wire transformations.

## M7 — Coordinator, retry/failover, and durable finalization

Primary class: invariant/capability

This is the highest-risk migration milestone. Port request persistence, account claim ownership, upstream submission, failure classification/effects, bounded retry, alternate-wire negotiation, downstream handoff, streaming ownership, cancellation, retained finalization, and terminal cleanup.

Exit condition: the failure-mode corpus proves parity for success, retry, rejection, cancellation, partial stream, premature EOF, malformed provider behavior, and crash-recovery ownership.

## M8 — Runtime generations, rehash, background tasks, and process lifecycle

Primary class: infrastructure/capability

Replace Python/Granian generation/process machinery with Rust-native immutable generation snapshots, reference-counted leases, atomic publication, retained terminal ownership, process-level state, signal/shutdown handling, and bounded background tasks.

Exit condition: live rehash does not interrupt in-flight work; shutdown/restart semantics converge; runtime diagnostics remain compatible.

## M9 — Operational CLI and lifecycle completeness

Primary class: capability

Complete `serve`, daemon/foreground behavior, stop/restart, deploy helpers, croncheck/ensure-running, backup/recover, migration commands, update/version, onboarding/connect/logout, config editing/key management, diagnostics, uninstall, and other documented commands.

Packaging/install behavior is migrated only when the corresponding binary behavior exists.

Exit condition: documented CLI workflow parity is complete on supported deployment targets.

## M10 — Full differential qualification and SBC characterization

Primary class: invariant/polish

Run the complete contract matrix, targeted live-provider smoke tests, visual dashboard review, database rollback/upgrade tests, failure/restart tests, and same-host resource characterization on at least one representative ARM64 SBC profile.

Do not invent hard performance gates unsupported by evidence. Record RSS/process/thread/local-dispatch observations separately from upstream inference latency.

Exit condition: all mandatory compatibility gaps are closed or approved as supported differences by ADR.

## M11 — Rust cutover

Primary class: capability

Make Rust the canonical install/release/runtime implementation, update installer/package/release docs, preserve existing filesystem/config/database locations, and publish rollback instructions to the final Python reference release where schema compatibility permits.

Exit condition: new installs and upgrades use Rust by default with no Python runtime dependency.

## M12 — Python retirement

Primary class: polish/invariant

After a defined stabilization window, remove Python production/runtime packaging and migration-only dual-run machinery. Preserve a reference tag/branch and retain only differential fixtures that remain useful as regression assets.

Exit condition: repository and release pipeline are pure Rust for production while historical parity evidence remains traceable.

## Cross-cutting constraints

At every milestone:

- Python remains usable until cutover;
- no dashboard redesign is folded into migration work;
- no database reset is allowed;
- no broad new CI matrix without demonstrated need;
- provider secrets and proxy credentials remain redacted;
- unsupported behavior fails closed rather than silently bypassing routing/proxy/security semantics;
- implementation plans remain bounded and closure evidence is mandatory.