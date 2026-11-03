# Tool Integration M003 — Authenticated, scoped MCP gateway

Status: active

Repository baseline: `0e021c1ffe55803aff5f325033e65313dca9f1dc` (planning, 2026-11-03)

Source roadmap: plans/subsystems/tool-integration-roadmap.md (M003)

Long-term requirements: plans/000-long-term-specification.md §§1–5; plans/001-terminology-and-domain-model.md; plans/002-long-term-roadmap.md `4; plans/003-planning-process.md

Applicable ADR: plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (accepted)

Primary class: capability

## 1. Objective

Serve a disabled-by-default, authenticated tools-only MCP endpoint through EggPool for an operator-configured local Eggsearch backend. Valid managed users can discover and invoke exactly their allowed tools; ordinary inference endpoints stay transparent and independent.

## 2. Why blocked

**Hard:** M002 is closed with ADR-0007 accepted. M005 corrected the grant list to the exact verified Eggsearch catalog. **Interface and operational gates:** Eggsearch 0.4.3 Streamable HTTP initialize, discovery/list, and `tools/call` were qualified against the pinned local service. The effective grant interface is stable and may now be consumed.

## 3. Current evidence

rust/src/server/mod.rs hosts thin Axum routes and auth middleware; rust/src/server/middleware.rs validates managed identities. rust/src/identity.rs distinguishes personal tokens from browser sessions and legacy shared credentials. rust/src/server/inference.rs delegates to coordinator, which owns provider retries/finalization. Eggsearch docs/deployment.md documents loopback-only /mcp, Host/Origin checks, 1 MiB POST and 120s transport ceiling; docs/tool-matrix.md covers structuredContent, outputSchema, trust markers, tool fingerprint, compact response detail, error taxonomy and protocol versions. EggPool has no MCP endpoint today.

## 4. Invariants

No client-selected backend URLs, arbitrary MCP transports/methods, session tunneling, or live discovery of unauthorized tools. Gateway authentication is always required even when dashboard.public is true; inference-only token denied. Tool responses retain trust labels; no result content stored/logged; no new downstream listener. Tool failure must not affect model provider health or account backoff. Disabled/no-backend path should not create a transport client.

## 5. Scope

**In:** a separately gated route such as `POST /api/tools/v1/mcp` (freeze after ADR acceptance); negotiated MCP initialisation/discovery and tools/list/tools/call; exact scoped authorization and filtered catalog, valid JSON-RPC/MCP error envelopes; bounded backend transport; compact response defaults with faithful source identifiers and provenance; operator diagnostics. Consistent concurrent stale-catalog and cancellation behavior.

**Out:** general resources/prompts or filesystem, blind HTTP response forwarding, hosted model→tool loop (M004), unsupported SSE/resume/batch semantics, web search engines in EggPool, remote MCP server registration, arbitrary client-selected tool execution. Do not reimplement Eggsearch `web_search`/`web_fetch`.

## 6. Required production changes

Build a narrow MCP boundary in rust/src/server/ with its own typed application service rather than coordinator or arbitrary HTTP proxy. Route only supported JSON-RPC operations; validate MCP protocol-version negotiation and request IDs, content types, origin/host where applicable, batch/notification/session behavior (explicit rejects for unsupported variants). Use existing personal bearer authentication and the tool scopes/grants from M002. Publish a namespaced catalog (e.g. backend.tool) that maps exactly to a configured backend, with deterministic snapshot fingerprint, schema cache and size bound. Authorize again at call time even if the client used an earlier tools/list. Reject requests for unlisted tools or unknown params before backend invocation. Only operator-approved local loopback Eggsearch URLs reach the transport and require correct Host/Origin semantics; never forward downstream Authorization headers upstream.

Handle MCP structuredContent without coercing it to untrusted free text. Preserve `external_untrusted`/`local_trusted` markers, source-card IDs, injection markers, isError and sanitized repair hints. Apply content size, max depth, concurrency and execution deadline limits after upstream parsing as well as before. Shorten the model-visible projection using Eggsearch response_detail compact without hiding failures/absence. Logging persists names, status classes and numeric counts, never arguments, URLs, source text or full result.

Decide whether tools-only stateless MCP semantics are sufficient for target clients; if clients require session semantics, implement a bounded legitimate subset or document unsupported clients. Do not claim generic Streamable HTTP parity from a single JSON-RPC POST smoke. Use feature gate/config to keep default initialization lean; no new runtime thread/pool when disabled.

## 7. Ordered work packages

A. **Protocol contract fixture.** Pin supported Eggsearch MCP version and capture sanctioned initialize, tools/list and tools/call request/response shapes, including errors and structuredContent. Choose supported session flow and negotiate protocol without fabricated responses.

B. **Catalog/identity.** Map backend contract to namespaced tool IDs; implement grants filtering and version/fingerprint invalidation. Deny stale/unknown tool calls even on direct invocation.

C. **Bounded invocation adapter.** Dispatch using a narrow MCP client, enforce deadlines/bytes/depth, map safe typed errors; ensure backend-origin validation and no credential forwarding.

D. **HTTP routing and operator docs.** Add authenticated route to existing Axum/EggServe listener with explicit enable flag; document client setup and limits. No changes to /v1 inference handlers.

E. **Negative/operational qualification.** Prove denial matrix with two principals and actual supported Eggsearch, then run default-off SBC and representative team-host contention checks.

## 8. Failure/cancellation/restart/contention

A backend timeout, malformed response, MCP version mismatch, tool error, aborted client, or late result fails that tool call without unhealthy-model side effects. Cancellation must drop backend work and release concurrency permits; downstream retry does not accidentally re-run a potentially side-effectful tool. Backend/catalog changes during rehash use immutable generation snapshots or an equivalently atomic published state; do not serve tool A's schema while invoking backend B's A. Service restarts reconstruct ephemeral MCP sessions; persisted per-user permissions remain authoritative.

## 9. Compatibility/migration

All standard inference requests and existing clients work unchanged. New route is off by default, explicitly authenticated, and not part of the OpenAI/Anthropic tool-call promise. No new migration beyond M002. Never expose the operator's underlying loopback endpoint to unauthenticated LAN clients via a generic tunnel.

## 10. Required tests

Add rust/tests/mcp_gateway.rs. Include version/handshake conformance, tools/list filter, direct-call bypass, tool name collision, schema tampering, malformed JSON-RPC, batch/notification/unsupported method, origin/content-type, stale catalog revision, credential revoke, disabled/missing backend, oversize response, wrong MIME, source metadata retention, slow backend abort, parallel users, rehash and shutdown. Re-run rust/tests/server_auth.rs, server_transport.rs, on_prem_identity.rs, on_prem_model_policy.rs, coordinator_finalization.rs, codex_responses_compat.rs and wire_stream.rs.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Run `cargo test --manifest-path rust/Cargo.toml --test mcp_gateway -- --test-threads=1` and exact Eggsearch service smoke; preserve outputs specifying its version and advertised MCP protocol. Record baseline memory of gateway disabled and enabled.

## 12. Documentation

docs/api-reference.md, docs/firewall.md, docs/deployment.md, docs/cli-reference.md, docs/agent-configuration.md, architecture/overview.md and dedicated tool-gateway threat model/guide. State exactly which MCP methods/transports are supported and which fail.

## 13. Acceptance criteria

With two managed users on the same EggPool instance, one may list/call allowlisted Eggsearch tools, the other sees no discovery or execution for those names; malformed/direct calls fail closed. Existing inference traffic is unchanged. Tool content and credentials are not persisted. External service failure is isolated from model readiness. Genuine MCP client interop is proven rather than merely an HTTP mock.

## 14. Stop conditions

Pause if supported Eggsearch version requires unsupported resumable sessions/transport or cannot deliver security-preserving result metadata; scope any protocol expansion through ADR review. Stop if a correct implementation needs changing auth default, the single inference admission path, or creating a second HTTP listener. Never mark a generic MCP compatibility claim from only a tools/list response.

## 15. Closure evidence

End-to-end live MCP client to EggPool to Eggsearch, protocol transcript sanitized of arguments/results, per-user auth matrix, stress/cancel/restart, negative tests, default/no-default suite results, binary/RSS impact and explicit supported-version table. Promote M004 only upon closure.

## 16. Handoff notes

Do not duplicate the Eggsearch tool catalog mechanically in EggPool. Catalog schemas should be discovered and fingerprinted, with a compact initial palette but no authority widening through deferred discovery. Keep policy and network I/O out of rust/src/server/ handler bodies.
