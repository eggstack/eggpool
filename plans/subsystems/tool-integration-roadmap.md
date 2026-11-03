# Optional Tool Integration Roadmap

Status: active

Long-term references:

- plans/000-long-term-specification.md §§1–5
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md §§4–5
- plans/003-planning-process.md

Related ADRs:

- plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (accepted; M002–M004 decision gate)

## 1. Purpose and ownership boundary

Add optional, governed standard tools to EggPool without changing its default inference role. The operator/config lifecycle belongs to rust/src/operations/integrations.rs, rust/src/cli.rs, rust/src/config.rs and rust/src/config_reload_policy.rs. Per-user authorization belongs to the existing identity, user-policy and database authorities. Authenticated transport remains in rust/src/server/ with typed independent tool handlers; existing rust/src/coordinator/ remains the only path for model inference. The tool client owns bounded calls to an operator-configured Eggsearch process, not arbitrary web search engines. Eggsearch remains the authoritative source for tool contracts, safety, result projection, and retrieval behavior.

## 2. Work classification

**Invariants:** default disabled; existing Chat/Responses/Messages semantics unchanged; identity/tool policy never bypassed through aliases or unlisted tool names; no provider-token or prompt/body retention; no new SQLite writer; one generation-owned path for each inference call; no tool health penalty to model account.

**Capabilities:** discover configured Eggsearch backend; authenticated per-user MCP tools/list and tools/call; optional finite model→tool→model execution endpoint.

**Infrastructure:** backend configuration/verification, typed tool catalog, per-user effective grants and scoped credentials, isolated transport client, bounded execution controller.

**Polish:** small schema disclosure, compact output, operator diagnostics, no-backend solo/SBC footprint regression evidence.

## 3. Non-goals

No mandatory Eggsearch install; no blanket MCP tunneling, hosted Skills/ACP, Python/shell execution, browser automation, stored conversations, background agents, vector DB, hosted SaaS/IAM, or forced migration of existing MCP-capable clients. Do not rewrite Eggsearch's deterministic search logic or enable unrestricted arbitrary MCP server URLs.

## 4. Current state

EggPool README and docs/api-reference.md describe /v1/chat/completions, /v1/responses, /v1/responses/compact and /v1/messages; docs/stateless-responses.md explicitly leaves tool execution to clients. rust/src/wire/tool_calls.rs re-exports the neutral eggpool-wire completed tool-call accumulator. rust/src/server/mod.rs is a thin Axum adapter around coordinator execution. Identity management with personal tokens, policies, budgets, browser sessions and audit lives in rust/src/identity.rs, rust/src/server/auth.rs, rust/src/server/dashboard/, rust/src/db and existing policies. CLI/client integration is in rust/src/operations/integrations.rs and rust/src/cli.rs. No Eggsearch-specific backend or MCP gateway is present in the documented API or Cargo dependency graph.

Eggsearch independently exposes ten MCP operations over stdio or loopback-only Streamable HTTP, with a stable tool contract, structured results, external-untrusted markers, progressive disclosure and compact response_detail. Its installation/integration workflow remains valid independently of EggPool; avoid reinventing it.

## 5. Target architecture

Opt-in operator TOML backend → validated Eggsearch tool-catalog snapshot (bounded cache/fingerprint) → effective per-user tool grant/scope filter → authenticated MCP-compatible gateway → local Eggsearch calls. The gateway may be used without inference. Independently, a separately versioned, finite hosted execution API can orchestrate a series of already-authorized inference turns and tools, each with explicit budgets. Tool grants are database/user-owned; upstream/backend credentials remain TOML/environment-owned. Model policy, user budget, and provider selection are reused, not forked. The ordinary inference API is unchanged.

## 6. Typed dependency graph

- M001: no hard dependency; operator-only backend configuration and diagnostics, no new public tool invocation.
- M002: **hard** M001 closure and accepted ADR-0007; **interface** stable existing managed-credential/profile revisions; can be designed before M001 but not shipped as grants without backend identity.
- M003: **hard** M002 closure; **interface** protocol-version/transport contract with supported Eggsearch binary; **operational** compatible Eggsearch loopback running for live qualification.
- M004: **hard** M003 closure and accepted ADR-0007; **interface** existing coordinator admission/finalization and eggpool-wire tool-call representations; **operational** deterministic local model/tool fixtures and at least one live run.
- Retrieval interoperability R001: **hard** M003 closure (for gateway); embedding E001 is **soft/interface**, not a forced dependency when the retrieval service owns its embedding backend.
- No predecessor in the existing request-admission-wire, on-prem-user-access or operations-cli roadmaps is reopened.

## 7. Milestones

### M001 — Backend configuration and local readiness
Class: capability. Deliver one operator-owned, off-by-default Eggsearch HTTP backend definition, a safe local health/capability probe and operator commands/config diagnostics. Value: reliable setup and troubleshooting, without promising tool calls to clients. Exit: clear disabled/missing/mismatched/ready cases, no mandatory dependency or extra listener, compatible solo config. Deferred: tool grants and gateway.

### M002 — Effective tool grants and scoped credentials
Class: infrastructure. Deliver DB-backed profile/user effective tool authorization and explicit credential tool scopes, CLI administration, default-deny migration, bounded internal policy APIs, token-revocation semantics. Exit: cross-user grants, disabled/revoked tokens, stale version and unknown-tool cases fail closed, no public gateway yet. Deferred: MCP wire.

### M003 — Authenticated MCP tool gateway
Class: capability. Deliver negotiated tools-only MCP discovery/invocation through EggPool with authorization, backend validation, bounded output, structured error/trust semantics. Exit: real Eggsearch tools/list and tools/call work for approved users; forbidden/unavailable tools never execute; no unhandled MCP method tunnels. Deferred: model-orchestrated tool loops and streaming agents.

### M004 — Explicit finite hosted tool orchestration
Class: capability. Deliver a separately versioned, opt-in finite operation for bounded model/tool iterations, using the existing coordinator on each model turn. Exit: multi-turn model/tool/model works with exact user attribution and budget enforcement, deterministic stop/error/cancellation; ordinary inference unchanged. Deferred: streaming, background threads, code execution.

## 8. Cross-cutting requirements

Storage: M002 adds only additive local policy tables or columns with backup/restore checks, one database gate; no tool payload storage. Protocol: standard MCP subset with version negotiation, no arbitrary pass-through; distinct hosted endpoint is EggPool-specific. Security: no tool access via inference-only credential or dashboard-public legacy path; per-tool policy at both discovery and invocation, trust labels survive result projection. Runtime: immutable catalog revision, bounded concurrency/deadlines, session/restart/cancellation ownership, process generation reload classification through single authority. Observability: content-free tool/backend/decision metrics only, no query or URL. Performance: disabled path adds no service connection, recurring task or large dependency; validate physical SBC solo footprint and representative team host separately.

## 9. Verification strategy

Use existing serial cargo suites and no-default feature checks; add tool-config, policy/DB, HTTP MCP conformance, malicious input, disabled/no-backend, revocation races, stale catalog, truncated results, restart and cancellation tests. Live Eggsearch local HTTP test is operational evidence, not a substitute for deterministic protocol fixtures. Compare binary/RSS/startup behavior of default disabled before/after. Update architecture/deep-dive references, docs/api-reference.md, docs/cli-reference.md, docs/configuration.md or the actual canonical config references, and AGENTS.md if coverage changes.

## 10. Risks and decision points

ADR-0007 is accepted; avoid treating a tool definition as execution authorization. MCP legacy sessions versus stateless discovery, request transport envelopes, sessions and pagination need explicit version-appropriate conformance. A feature that needs generic process execution, remote MCP trust, new stateful Responses, or secondary inference/retry owner requires a separate ADR, not scope creep. Client-owned Eggsearch MCP remains the recommended simple workstation route.

## 11. Completion definition

Four distinct milestones receive closure evidence under plans/closure/tool-integration/, and each dependency-ready successor is promoted through the registry only after predecessor closure. Tests establish default transparent inference behavior and an opt-in tool-enabled workflow end-to-end. No upgrade automatically enables web access.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| M001 backend config/readiness | closed | plans/implementation/tool-integration/001-eggsearch-backend-config-and-readiness.md | plans/closure/tool-integration/001-status.md | none |
| M002 tool policy/scopes | closed | plans/implementation/tool-integration/002-tool-grants-and-credential-scopes.md | plans/closure/tool-integration/002-status.md | none |
| M003 MCP gateway | conditionally closed | plans/implementation/tool-integration/003-authenticated-mcp-gateway.md | plans/closure/tool-integration/003-status.md | Software capability and live Eggsearch qualification pass; physical SBC footprint/contention evidence remains |
| M004 hosted execution | active | plans/implementation/tool-integration/004-finite-hosted-tool-orchestration.md | pending | Implement finite route and extract bounded internal tool-invocation service |
| M005 canonical tool identity correction | closed | plans/implementation/tool-integration/005-canonical-tool-identity-correction.md | plans/closure/tool-integration/005-status.md | Exact shared identity list matches Eggsearch 0.4.3; M003 promoted to ready |
