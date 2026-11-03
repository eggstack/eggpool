# ADR-0007: Optional tool authority, MCP gateway, and hosted execution boundary

Status: proposed

Date: 2026-11-03

Decision owners: EggPool maintainers

Related specification sections:

- plans/000-long-term-specification.md §§1–4 (on-prem identity, single admission, canonical wire, stateless compatibility)
- plans/001-terminology-and-domain-model.md (principal, account, generation, authorization)
- plans/002-long-term-roadmap.md §§4–5 (integrations and local users)
- plans/003-planning-process.md `2.2 (public-contract ADR gate)

Affected subsystem roadmaps:

- plans/subsystems/tool-integration-roadmap.md
- plans/subsystems/retrieval-interoperability-roadmap.md

## Context

EggPool is a transparent stateless LLM provider router with Chat Completions, Responses, Responses compact, and Anthropic Messages ingress. It transcodes function calls but currently does not execute tools: docs/stateless-responses.md explicitly assigns execution to the client. Eggsearch independently exposes ten deterministic search/fetch/research/evidence tools through MCP (stdio or loopback Streamable HTTP) and a Rust SDK. The gap relative to PrivateGPT-style application APIs is not another search backend but opt-in, governed access to tool discovery and execution. This is an intentional extension of an on-prem router, not a conversion into a general agent framework.

## Decision drivers

- Preserve byte-level and semantic compatibility of all current inference routes by default; never assume a client-supplied tool definition grants EggPool permission to execute it.
- Preserve the solo/SBC path: no mandatory Eggsearch binary, background service, MCP parser, retrieval database, or new default listener.
- Support a controlled local team gateway using existing managed identities, token revocation, effective-policy semantics, and request attribution.
- Prevent privilege escalation via model aliases, client-provided tool names, synthetic tool responses, prompt injection, SSRF, or MCP protocol tunneling.
- Avoid duplicating Eggsearch engines, and avoid making Eggsearch's dependency graph a mandatory EggPool runtime dependency.
- Respect the single coordinator inference owner, generation leases, cancellation, storage gate, and secret-free observability.

## Considered options

### A — Ship Eggsearch engines or SDK inside the core router

Reduced IPC and a single-process experience, but large dependency/footprint expansion, shared failure domain, and inconsistent service lifecycle. Rejected as the default; optional in-process SDK optimization requires separate measured evidence and a new decision.

### B — Blind HTTP reverse proxy of all MCP traffic

Easy transport forwarding but no reliable per-tool authorization, protocol downgrade safety, output bounds, or namespace isolation. Rejected.

### C — Narrow, opt-in MCP adapter with scoped dispatch and separate hosted orchestration

Chosen. Operator-configured tool backends remain distinct services. EggPool authenticates the downstream principal, authorizes specific operations, and speaks the supported MCP subset on the authenticated gateway path. Any model-to-tool loop is a distinct opt-in operation.

## Decision

1. **Tooling stays optional and out of ordinary inference.** The default EggPool binary/runtime remains a model gateway. Existing Chat/Responses/Messages/compact requests remain transparent. Client-owned tool calls, native upstream-hosted tools, and unsupported native semantics retain their existing pass-through/rejection rules. No implicit web search and no interception of arbitrary function calls.
2. **Operator-owned backend registration.** Backends are configured in operator-controlled TOML/environment, not user-editable SQLite. The initial supported backend is one explicitly configured local Eggsearch Streamable HTTP endpoint, bound on loopback. Validate service identity, protocol, origin, address, timeouts, and tool contract. Do not accept arbitrary client-chosen upstream URLs. A later remote backend requires explicit TLS/auth and SSRF policy review.
3. **Authenticated, tool-scoped MCP gateway.** A separate, disabled-by-default route family exposes only a negotiated MCP tools discovery/invocation subset, not a generic JSON-RPC tunnel. Implement correct initialization/discovery/response/error/transport semantics for the MCP version actually used by Eggsearch. Unsupported MCP methods, notifications, transports, session resumability, arbitrary resources/prompts, and event streams fail explicitly rather than being silently proxied. Tool definitions are sourced from a validated operator backend; client tool arguments never select a different backend.
4. **Separate tool authority from model authority.** A managed personal credential must carry explicit tool scopes and be subject to the authenticated user's effective per-tool grants. Profiles and overrides are DB-owned. Admin permissions alone do not authorize a general remote tool-execution bypass; default effective tool grants are empty. Disabled users and revoked credentials are denied. Discovery is filtered to the caller's tools; invocation repeats the check after resolving canonical backend/tool identity. No tool access from legacy shared-key or unauthenticated dashboard shortcuts.
5. **Explicitly selected hosted execution.** Any EggPool-owned model→tool→model loop must be exposed through a separately versioned EggPool-specific API, initially finite and stateless; the eventual route and DTO are frozen in the M004 handoff after interface review. It may call only explicitly operator-allowlisted tools and only when the caller opts in. No claim of OpenAI's native hosted-tools compatibility, autonomous browsing, persistent threads, background runs, code execution, or generic skills. Each internal model turn uses the existing coordinator admission, policy, provider selection, quotas, retry/finalization, generation lease, and user-budget authorities; the loop itself owns neither independent retries nor detached storage.
6. **Bounded execution and trust.** Tool count per turn, total iterations, deadline, concurrency, tool argument bytes, result bytes, and model-visible projection are finite and fail closed. Preserve Eggsearch's `external_untrusted`/`local_trusted` classification (neither is instruction-trusted), source IDs, and provenance; never upgrade tool results to system/developer authority. Use compact schema discovery and result projection by default. Tool failure is not model-provider health failure.
7. **Diagnostics.** Only content-free tool name, backend ID, authorization decision, safe error class, timings, counts, and coarse usage may enter logs/analytics. Never retain tool arguments, fetched documents, prompts, result bodies, arbitrary URLs, or credentials. Durable tool audit storage, if introduced, must have bounded retention and preserve the current single SQLite gate.

## Consequences

### Positive

- A native way to use Eggsearch with managed team credentials, while ordinary clients still use Eggsearch directly.
- Optional application-level features without making the single-user router an always-on agent service.
- Reuse of Eggsearch's output provenance, sanitization, and progressive-disclosure work.

### Negative

- An MCP adapter and policy schema add protocol, cancellation, auth, and test burden.
- Hosted orchestration adds multiple accounted inference turns per user request and cannot masquerade as a single ordinary provider request.
- Explicit tool-enabled mode is less convenient than silently changing default completions, by design.

### Neutral or deferred

- A Rust SDK-backed transport, external RAG services, SQL tools, skills, local sandbox executors, and streaming orchestration are separate decisions and milestones.
- No hosted tenant, SSO, cloud search default, new vector store, or enterprise connector requirement.

## Compatibility and migration

Default config omits tooling, and all existing clients, request schemas, API keys, budgets, diagnostics, and deployment workflows remain valid. Managed policy migration creates default-deny grants; it does not broaden an existing inference credential. New tool scopes must be explicitly granted through an operator-controlled lifecycle. Any new versioned API responds with an explicit feature-unavailable or permission denial, never silently falls back to general inference. New config fields follow config_reload_policy.rs; do not independently add reload/restart decisions.

## Security and reliability implications

Assume malicious MCP arguments and outputs and hostile remote content. Validate every requested name and JSON body against the cached backend contract and per-user policy; independently enforce limits after the backend responds. Protect against request smuggling, session mixup, stale cached grants, DNS rebinding, MCP origin confusion, prompt injection, SSRF, and cyclic tool/model loops. Explicitly test revocation/disable during a session, shutdown, client disconnect, backend failure, and late tool response. Transport to Eggsearch loopback is not a security boundary against other local processes; only the EggPool ingress can claim user-specific authorization.

## Verification

Require protocol conformance against the installed supported Eggsearch version, strict deny cases including malformed JSON-RPC and unauthorized direct invocation, two-user isolation, lifecycle/restart/reload tests, a missing-backend solo smoke, baseline binary/RSS measurement, and default/no-default workspace parity. Ensure normal Responses native streaming bytes and existing client tool calls remain unchanged. An ADR acceptance review is a hard gate for M002–M004; M001 operator-only backend discovery may proceed without enabling a public tool route.

## Supersession

None.
