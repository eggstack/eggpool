# EggPool Rust Migration — Terminology and Domain Model

Status: normative migration terminology

This document defines terms used throughout `migration-rs/` so implementation agents do not conflate product compatibility with byte-for-byte framework emulation.

## 1. Implementations

**Python implementation** — the existing EggPool implementation under `src/eggpool/` and its existing packaging/runtime machinery.

**Rust implementation** — the side-by-side pure Rust implementation developed under `rust/`.

**Oracle** — the implementation or independently specified artifact treated as authoritative for one observable behavior during migration. Python is the default oracle where the contract is not independently specified.

**Candidate** — the Rust implementation at a particular migration milestone.

**Cutover** — the release transition where Rust becomes the canonical shipped EggPool implementation.

## 2. Compatibility vocabulary

**Contractual behavior** — behavior that existing users, clients, config files, databases, scripts, frontend resources, or documented operator workflows may rely on and therefore must be preserved unless explicitly changed.

**Incidental behavior** — implementation artifacts such as ASGI internals, Granian-generated headers, Python exception class names not exposed publicly, exact TCP fragmentation, or object identity. Incidental behavior is not copied unless evidence shows a consumer dependency.

**Exact parity** — observations must be identical after documented normalization. Use for field names, route/method sets, config defaults, CLI command names, schema/checksums, and other closed contracts.

**Semantic parity** — implementation may differ internally but yields the same meaningful result. Use for scheduling, internal error types, concurrency primitives, and rendering mechanics.

**Supported difference** — an explicitly accepted, documented difference approved by ADR or cutover policy. Migration agents may not invent supported differences locally.

**Normalization** — a comparator transformation that removes known non-contractual variation. Every normalization rule must name what it removes and why it is safe.

## 3. Runtime ownership

**Process runtime** — state that survives safe config generation swaps, such as process-level database handles and bounded learned state that Python already treats as process-owned.

**Generation** — an immutable coherent runtime snapshot built from one validated configuration and published atomically for new requests.

**Generation lease** — ownership held by an in-flight request or retained terminal work that prevents its generation dependencies from being destroyed.

**Retained terminal work** — finalization/cleanup work whose lifetime may outlast the initiating request task and which must converge or remain durably recoverable.

**Claim** — the in-memory/durable ownership representing selection of account capacity for an attempt.

**Reservation** — durable quota/capacity accounting associated with an attempt and requiring exactly-once terminal release semantics.

## 4. Request boundaries

**Client surface** — the incoming EggPool protocol surface: Chat Completions, Responses, or Messages.

**Wire surface** — the concrete provider endpoint grammar/auth/path selected for an upstream attempt.

**Canonical request** — provider-independent request intent retained so retries can be rebuilt from source rather than from a prior translated payload.

**Downstream handoff** — the explicit boundary after which EggPool has committed an upstream response status/headers to the client and may no longer perform transparent provider failover.

**Terminal evidence** — protocol-defined evidence that a successful streaming response completed. Transport EOF alone is not necessarily terminal evidence.

## 5. Differential testing

**Fixture** — deterministic input plus controlled environment used against both implementations.

**Observation** — normalized externally meaningful result captured from one implementation: exit code, stdout/stderr, response status/headers/body/events, rendered DOM facts, database effects, or routing trace.

**Golden observation** — a recorded expected observation derived from the independent contract or Python oracle and reviewed as intentional.

**Differential case** — one fixture run against Python and Rust and compared through one explicitly declared normalization policy.

**Parity gate** — a required set of differential cases that must pass before a roadmap milestone can close.

## 6. Frontend terminology

**SSR renderer** — server-side code that creates the dashboard HTML response. Python and Rust renderers may be implemented differently but must preserve contractual DOM/content/escape behavior.

**Static asset baseline** — the frozen set of CSS, JavaScript, themes, images, and other static files copied into Rust packaging and guarded for drift.

**Visual parity** — no material visual regression for the supported dashboard pages at the agreed browser/viewport fixtures. It is supported by DOM/static-asset checks and targeted screenshots/manual inspection, not vague aesthetic judgment.

## 7. Migration work classes

**Invariant** — property that must remain true across the entire migration.

**Capability** — user/operator/client-visible behavior established in Rust.

**Infrastructure** — internal machinery needed for later capabilities but not itself proof of user-visible parity.

**Polish** — ergonomics, diagnostics, performance, cleanup, or documentation after a correctness boundary exists.

Implementation plans must use these labels consistently.
