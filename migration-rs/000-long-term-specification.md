# EggPool Rust Migration — Long-Term Specification

Status: normative migration specification

Planning baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

This document defines the intended end state of the EggPool Rust migration. It is not an implementation checklist. The keywords MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, and MAY are normative.

## 1. Product identity

EggPool remains a lightweight, LAN-oriented multi-provider LLM aggregation proxy. The Rust migration MUST preserve the product that existing EggPool users, clients, configuration files, databases, and operator workflows already observe.

The migration is successful when an existing supported EggPool deployment can move from the final Python release to the Rust release without redesigning its configuration, resetting its database, changing client endpoint configuration, relearning the CLI, or accepting materially different routing/failure behavior.

## 2. Side-by-side migration invariant

Until migration closure:

- the Python implementation remains runnable and supported in its current repository locations;
- Rust production source lives beneath `rust/` and does not replace Python modules in place;
- differential fixtures can execute both implementations from one checkout;
- migration work MUST preserve unrelated Python behavior unless an explicit cross-implementation contract correction is approved;
- Python remains the behavioral oracle where the public contract is not already specified independently.

No migration milestone may require users to run a hybrid Python/Rust production process. The intended end state is one pure Rust EggPool process.

## 3. Public compatibility surfaces

### 3.1 Configuration

Rust MUST accept the same supported TOML surface, field names, defaults, aliases, environment-variable indirections, provider/account structures, proxy references, validation constraints, and config path resolution as the Python implementation at the compatibility freeze point.

Config acceptance/rejection is contractual. Exact Pydantic wording is not inherently contractual; EggPool-owned error category, field attribution, exit status, and operator usefulness are.

Existing configuration files MUST NOT require a migration rewrite solely because the implementation becomes Rust.

### 3.2 CLI

The final Rust binary MUST expose the same `eggpool` command hierarchy and supported flags. Commands MUST preserve meaningful stdout/stderr behavior, exit-code classes, side effects, path resolution, daemon/foreground intent, and safety checks.

During side-by-side development the Rust binary is invoked by explicit build path and MUST NOT displace the installed Python executable.

### 3.3 HTTP API

Rust MUST preserve supported methods, routes, query behavior, authentication behavior, status codes, EggPool-owned response headers, JSON field names/types, error envelopes, model identifiers, and SSE event semantics for:

- OpenAI Chat Completions;
- stateless OpenAI Responses;
- Anthropic Messages;
- model listing;
- health/readiness;
- stats and diagnostics;
- model-info APIs;
- dashboard routes and operational endpoints.

Transport segmentation, TCP packet boundaries, framework-generated date/server headers, and other implementation artifacts are not contractual unless separately documented.

### 3.4 Database

The existing SQLite database is a migration compatibility boundary.

Rust MUST:

- open databases created by supported Python releases;
- preserve SQLite WAL and durability semantics required by EggPool;
- use the same numbered schema migrations and checksum expectations unless a later accepted migration adds new schema;
- preserve row meaning, units, nullability, terminal-state semantics, and durable recovery assumptions;
- permit a controlled rollback to the final Python implementation while the schema remains within the documented compatibility window.

A Rust rewrite MUST NOT reset or fork the database merely to simplify implementation.

### 3.5 Dashboard and SSR

The current dashboard's visual design is considered complete for migration purposes.

Rust MUST mirror the existing server-rendered dashboard routes, page structure, content semantics, escape behavior, CSS classes/IDs relied on by scripts, themes, static resources, and JSON-backed dynamic behavior closely enough that existing visual inspection and browser workflows remain valid.

Static assets SHOULD be copied byte-for-byte into the Rust packaging boundary once the migration asset baseline is frozen. A manifest/hash guard SHOULD detect accidental drift between the frozen Python asset set and the Rust copy until Python is retired.

The migration MUST NOT use the Rust rewrite as an excuse to redesign the dashboard or replace SSR with a new frontend framework.

## 4. Routing and provider semantics

Rust MUST preserve the effective behavior of:

- provider and account eligibility;
- routing priority tiers;
- quota/load scoring;
- fairness rotation and tie handling;
- account/provider/model suppression;
- durable backoff semantics;
- capability and wire-surface eligibility;
- model-router behavior and sticky affinity semantics;
- retry and failover decisions;
- bounded alternate-wire negotiation;
- account claim/reservation ownership;
- provider health effects.

Cost MUST NOT silently become a routing score input if Python does not use it for routing.

Concurrency implementation may differ, but externally meaningful selection and ownership invariants MUST remain stable under equivalent controlled state.

## 5. Request, retry, and finalization invariants

The Rust implementation MUST preserve the high-value correctness properties of the current coordinator, including:

- durable request/attempt state is established before upstream submission where required by the existing contract;
- local preparation faults are not misclassified as provider health evidence;
- only authorized pre-handoff transport/provider failures may consume retry/failover behavior;
- account retries and alternate-wire submissions share the configured submission budget;
- failed-attempt cleanup converges before reselection;
- no provider retry occurs after downstream response handoff;
- streaming terminal success requires protocol-specific terminal evidence rather than transport EOF alone where the current protocol requires it;
- cancellation closes upstream resources and converges owned reservations/finalization state;
- terminal cleanup is idempotent and cannot double-release reservations or counters;
- crash recovery remains durable rather than relying on in-memory-only repair.

Rust RAII MAY simplify synchronous ownership cleanup but MUST NOT substitute `Drop` for async/durable terminal work that requires SQLite or awaited network/resource shutdown.

## 6. Runtime architecture

The end-state Rust server SHOULD be a single OS process for normal foreground or systemd-managed operation.

The Granian supervisor/worker topology is not an end-state requirement. Rust MAY replace it with a direct Tokio/Hyper/Axum runtime provided CLI and operational behavior remain compatible.

Runtime configuration generations SHOULD use immutable generation-owned state and reference-counted leases. Live rehash MUST NOT invalidate in-flight requests or retained terminal work. Process-owned learned state may survive generation changes only under the same semantic/fingerprint rules as the Python implementation.

## 7. HTTP and proxy architecture

The preferred Rust HTTP stack is Tokio + Hyper/Hyper-util + Rustls, with Axum/Tower for inbound routing and middleware.

Per-account outbound proxying SHOULD use Eggress as an in-process connector rather than starting a local proxy listener.

The compatibility target is the subset of the current EggPool pproxy URI contract that is actually documented and supported. Unsupported Eggress feature combinations MUST fail closed and be identified during differential qualification rather than silently falling back to direct networking.

## 8. Security

The migration MUST preserve or improve:

- constant-time API-key comparison;
- secret redaction in logs and diagnostics;
- no prompt/tool/body leakage into dashboard or stats surfaces;
- proxy-credential redaction;
- safe provider-header construction;
- request-body limits before unbounded allocation;
- provider media/document limits;
- filesystem permissions and secret handling;
- root/daemon safety behavior;
- fail-closed behavior for unsupported proxy or wire configurations.

The project remains a LAN/local deployment tool, not an Internet-edge security product. The migration SHOULD avoid production-cloud complexity that does not serve the supported deployment model.

## 9. Resource and dependency goals

Rust SHOULD materially reduce process count, steady-state runtime overhead, Python interpreter dependency, and optional native-wheel complexity.

The Rust implementation SHOULD remain one principal crate until a real ownership/reuse boundary justifies additional crates. It SHOULD avoid an ORM, actor framework, general dependency-injection framework, or duplicate HTTP client stacks unless evidence requires them.

Eggress dependencies MUST use the narrowest feature set needed by EggPool rather than Eggress's default full feature set.

Performance work MUST distinguish local EggPool overhead from upstream model latency. Numerical performance claims require same-host measurements; workstation results do not substitute for ARM64 SBC characterization.

## 10. Verification model

Python-vs-Rust black-box differential testing is a first-class migration artifact.

The migration test system SHOULD compare normalized observations for:

- config validation;
- CLI behavior;
- HTTP API responses;
- SSR route output/DOM invariants;
- SQLite durable effects;
- deterministic routing decisions;
- failure classification;
- streaming event order/termination;
- cancellation and cleanup;
- proxy URI behavior.

The comparator MUST normalize only explicitly non-contractual implementation details. Normalization MUST NOT hide a meaningful semantic mismatch.

Live-provider tests remain bounded manual/opt-in evidence and MUST NOT duplicate every production inference call across both implementations.

## 11. Cutover and removal

Rust becomes canonical only after the long-term closure gates are met. Cutover MUST include:

- supported config corpus parity;
- CLI surface parity;
- database compatibility and rollback evidence;
- public API and stream parity;
- dashboard/SSR parity;
- provider/proxy qualification;
- routing/retry/finalization correctness;
- lifecycle/rehash/restart/backup/recovery qualification;
- representative SBC runtime characterization;
- documentation and installation migration.

Python removal is a separate final milestone after Rust canonicalization. The Python implementation SHOULD remain available in repository history or a final reference tag for oracle/debugging after source removal.

## 12. Non-goals

The migration does not inherently authorize:

- a dashboard redesign;
- a new database schema architecture;
- a new routing algorithm;
- full OpenAI API parity beyond EggPool's existing claim;
- distributed/multi-node EggPool;
- Kubernetes/cloud control-plane machinery;
- generalized plugin frameworks;
- replacing Eggress with a new proxy implementation;
- increasing CI into a broad platform matrix before the migration needs it.
