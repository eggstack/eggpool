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

Historical implementation sequence: D001 contract/fixture freeze -> D002 account registry/catalog cache -> D003 catalog refresh/normalization/persistence -> D004 quota/claims/scoring plus D005 health/backoff/circuit/quarantine -> D006 eligibility/routing/fairness/local claims -> D007 model-router compilation/affinity -> D008 differential qualification/initial closure -> D009 selection-fairness and frozen-routing-trace corrective pass. D009 is closed.

D009 made configured random fairness affect the actual accepted claim path and froze the exact accepted score/fairness/candidate snapshot on the local claim so later routing traces do not rescore after pending/active publication. D001-D008 closure records remain append-only historical evidence.

M5's local selection claim stops before durable inference persistence. Semantic model-router selector calls that invoke `RequestCoordinator` remain M7 work. Optional generic external catalog polling/background scheduling remains M8 work.

Exit condition: deterministic state snapshots produce parity-equivalent candidate sets, selections, exclusions, local claim ownership, durable M5 effects, and accepted selection/fairness trace evidence under concurrency/restart tests. Satisfied after D009.

## M6 — Canonical request boundary, wire codecs, transcoding, and SSE

Primary class: capability/invariant

Subsystem roadmap: [Canonical Request and Wire Codec Runtime](subsystems/canonical-wire-roadmap.md).

Port the deterministic semantic transformation layer while keeping inference orchestration out of scope: bounded request admission, canonical source intent/IR, static wire-profile registry, OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, Gemini generateContent, reasoning/tools/structured-output loss policy, multimodal/documents/cache controls, SSE framing/event conversion, normalized usage, terminal evidence, and one caller-selected-profile runtime facade for M7.

Historical implementation sequence:

W001 contract/fixture freeze -> W002 canonical IR/admission/limits/M5 bridge -> W003 static wire profiles/codec contract -> W004 OpenAI Chat + Anthropic codecs -> W005 OpenAI Responses + Gemini codecs -> W006 reasoning/tools/structured-output/loss policy -> W007 multimodal/documents/cache/provider adaptation -> W008 SSE/events/usage/terminal evidence -> W009 selected-profile runtime facade -> W010 integrated differential qualification/closure.

Post-W010 review reopened aggregate M6 for two bounded correctness/qualification findings. The corrective sequence is W011 SSE EOF UTF-8 finalization correction -> W012 full Python-derived cross-surface request/finite/stream differential requalification and M6 re-closure.

W010 remains append-only historical closure evidence. Its aggregate conclusion is superseded only for the W011/W012 findings: Rust can silently drop an incomplete UTF-8 suffix at SSE EOF, and the W010 15-pair cross-surface assertions do not compare the complete semantic fields/client encodings required by the plan even though the Python oracle exposes richer transformation observations.

Only the registry's dependency-ready table authorizes implementation. W011 is the current ready corrective plan; W012 is blocked on accepted W011 closure.

M6 deliberately does **not** port Python `wire.resolver` runtime negotiation state. Learned/preferred wire selection, rejected-wire candidates, alternate-wire retry, provider HTTP submission, response handoff, cancellation/timeouts, failure effects, and durable finalization remain M7 because they depend on attempt ownership.

Exit condition: non-dispatch request/finite/stream transformations across all supported client/profile pairs match Python under explicit exact-vs-semantic rules, resource/security bounds are explicit, invalid UTF-8 and malformed/incomplete streams cannot become false success, and M7 can consume one stable selected-profile codec runtime. M6 is not closed for successor handoff until accepted W011 and W012 closure re-establish this condition.

## M7 — Coordinator, retry/failover, and durable finalization

Primary class: invariant/capability

This is the highest-risk migration milestone. Port request persistence, account claim ownership, upstream submission, failure classification/effects, bounded retry, alternate-wire negotiation, downstream handoff, streaming ownership, cancellation, retained finalization, and terminal cleanup.

M7 consumes the closed M4 provider transport, M5 local selection/claim state, and M6 selected-profile codec runtime. It owns the dynamic wire negotiation/retry lifecycle deliberately excluded from M6.

M7 planning/implementation handoff remains blocked while M6 W011/W012 corrective work is open. Accepted W012 closure may make M7 eligible for its own planning review; it does not promote M7 automatically.

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
