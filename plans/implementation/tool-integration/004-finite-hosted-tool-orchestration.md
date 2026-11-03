# Tool Integration M004 — Explicit finite hosted tool orchestration

Status: active

Repository baseline: `0e021c1ffe55803aff5f325033e65313dca9f1dc` (planning, 2026-11-03)

Source roadmap: plans/subsystems/tool-integration-roadmap.md (M004)

Long-term requirements: plans/000-long-term-specification.md §§1–5; plans/001-terminology-and-domain-model.md; plans/002-long-term-roadmap.md §§1, 4–5; plans/003-planning-process.md

Applicable ADR: plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (accepted)

Primary class: capability

## 1. Objective

Add an explicitly opted-in, finite, stateless model→approved tool→model operation, available to non-MCP clients, without interpreting ordinary OpenAI/Anthropic tool_calls as authorization to execute them. All internal inference turns still pass through the existing coordinator and user/model budget authorities.

## 2. Readiness and dependencies

**Hard:** M003 authenticated MCP gateway closed and ADR-0007 accepted. **Interface:** stable completed tool-call IR and tool definitions, GenerationLease / execute_endpoint_attributed path, principal/request attribution, hard cost/token admission, and M003 bounded tool-invocation API. A deterministic test model and local Eggsearch fixture are operational inputs. Do not build a second retry owner or start on an unclosed gateway.

Readiness audit (2026-11-03): M003 is conditionally closed with its software
capability, live Eggsearch route, and per-user authorization qualified; its
remaining condition is physical SBC footprint/contention only. ADR-0007 is
accepted. The current stable interfaces are
`eggpool_wire::CanonicalToolCallAccumulator` / `CompletedToolCall`,
`GenerationLease`, `execute_endpoint_attributed`, and
`AuthenticatedPrincipal::request_attribution`. The M003 gateway will first
extract a crate-private bounded invocation service from the HTTP adapter so
orchestration will not call the public MCP route. That is an M004 work-package
change within the accepted ownership boundary, not a blocker to the milestone
handoff. Local Eggsearch 0.4.3 and Ollama models are available for live
qualification; deterministic model behavior remains an implementation test
fixture requirement.

## 3. Current evidence

docs/stateless-responses.md states EggPool's Responses route is stateless and client-executed tools remain in the client; native-only hosted search is passed only to eligible upstreams, otherwise rejected. rust/src/wire/tool_calls.rs re-exports eggpool-wire's completed-call accumulator. rust/src/server/inference.rs delegates to rust/src/coordinator/execute_endpoint_attributed, and the coordinator owns request admission, accounting, cancellation and finalization. rust/src/server/mod.rs and rust/src/runtime_lifecycle/ own handler/generation lifecycle. The on-prem identity/budget code already has hard-versus-estimated admission rules, including failure-before-upstream behavior.

## 4. Invariants

No changes to /v1/chat/completions, /v1/responses, /v1/responses/compact, /v1/messages default semantics, including raw native Responses SSE bytes and native provider hosted tools. No stored conversations, background jobs or autonomous crawling. Every model turn is authenticated with the initiating user's policy and separately budget-admitted/finalized; tool service failure cannot be counted as model-provider failure. Every tool invocation reauthorizes the principal. No queries, prompts, raw tool results, reasoning content or credentials in logs/SQLite.

## 5. Scope

**In:** separately versioned EggPool-specific endpoint (candidate `POST /api/agent/v1/run`; freeze public route/DTO against accepted ADR before code), finite nonstreaming request/response, explicit tool profile and allowed tool selection, prompt/iteration/tool result budgets, typed error and evidence disposition, bounded sequencing. Provide original source and response tool call ID correlation and final answer.

**Out:** silently executing client tool_calls, full Responses server-side threads/previous_response_id, hosted native browser/file-search parity, arbitrary tools specified in request, code/shell execution, Python kernels, image/audio execution, multi-agent orchestration, persistent memory, streaming/resume, generic workflow editor or background execution.

## 6. Required production changes

Introduce a small application-layer orchestration service (not in rust/src/server/ handler logic) that accepts only a caller-selected mode and a validated model plus a bounded input. The public DTO must explicitly say it is EggPool-specific, finite, and stateless; require an authenticated managed token with both inference and hosted execution/tool permissions. Reject stream=true and unsupported native/server tool grammar before any dispatch. Obtain an immutable user policy snapshot and optionally re-check effective grants at each new tool invocation to honor revocation. Resolve model aliases through current routing, never bypass concrete model policy.

For each model step, call the existing coordinator once with a provider-compatible function-tool surface generated from the **allowlisted backend catalog**; preserve tool choice semantics and reject models lacking qualified compatible tool-call output. Parse completed call IDs/arguments with eggpool-wire; validate exact canonical tool name, schema and max argument bytes. Before invoking a tool, authorize again, then call M003's internal execution interface, not the external HTTP handler. Append the result as a model-visible tool output preserving `external_untrusted` markers, source IDs, failure/absence/truncation semantics, and no higher instruction privilege. Use response_detail=compact for default Eggsearch projections. Model-visible tool errors should be typed and constrained: do not manufacture success from tool timeouts. Terminate with a final answer only on authoritative finite model completion without further tool calls.

Bound maximum model iterations, individual and total tool calls, concurrent tools, result/output byte caps, total wall clock, request memory and per-user concurrent orchestrations. Enforce local bounds even if client asks for higher. No automatic retry of non-idempotent tool invocations. Account each internal model turn under the initiating user in the existing per-user and instance budgets; also provide a content-free orchestration ID linking child inference events without double-counting request metrics. If hard output/cost bounds cannot be established per child, fail closed before submit. The outer endpoint must not introduce an independent cost estimate mislabeled as hard.

## 7. Ordered work packages

A. **Public operation contract.** Freeze explicit route/DTO/version and stop conditions, supported tool-call grammar, finite result and error schema, max iteration defaults, and model capability qualification. Provide negative unsupported-mode tests first.

B. **Canonical tool call matching.** Build allowlisted tool definitions and idempotent call-ID tracking; reject malformed wrappers, duplicate IDs, unknown tool names, invisible schema changes and client-supplied backend URLs.

C. **Scoped finite execution.** Compose existing coordinator calls with authorized M003 internal tool calls, immutable policy/generation context, cancellation and timeout. No blocking database work in handler; no nested dispatcher logic duplication.

D. **Attribution, termination and diagnostics.** Ensure all nested requests charge users correctly, no double count, bounded controller state, safe terminal results and no raw content metrics.

E. **End-to-end tests and documentation.** Test two-step research example with deterministic model; run live local Eggsearch and a compliant upstream model separately. Document hosted tool scope and non-goals.

## 8. Failure, cancellation, restart and contention semantics

Abort on total deadline or client disconnect; release generation leases, tool permits and budget holds according to existing durable settlement. Late tool result after cancellation is ignored; never schedule a background continuation. A tool timeout does not trigger LLM-account quarantine. If model returns repeated tool-only outputs, step counter terminates deterministically; don't fabricate a final answer. Upstream model failure follows coordinator's established retry ownership; controller does not retry model calls independently. Revoked user denies subsequent steps and tools; clarify in-flight accepted operation semantics. Restart loses the stateless loop without resumability; no recovery of agent progress is claimed.

## 9. Compatibility and migration

Existing inference routes and clients stay unchanged. New operation is off by default and uses existing personal tokens plus explicit hosted/tool scopes; no stored state migration beyond M002 policy schema. Nested usage stored through existing request attribution, and any linking ID must be bounded/content-free. No provider API surface claims beyond supported function-tool execution.

## 10. Required tests

Create rust/tests/hosted_tool_orchestration.rs with valid two-tool and zero-tool paths; client denial, disabled/revoked user, model alias restrictions, tool catalog drift, unsupported native hosted tool request, repeated/duplicate call IDs, malformed arguments, nondeterministic tool order policy, prompt-injection framing, bounded loops, budget exhaust midloop, hard-price unknown, cancellation during each phase, slow and absent MCP, concurrent users, daemon shutdown/restart. Re-run rust/tests/coordinator_finalization.rs, coordinator_failure.rs, on_prem_budgets.rs, on_prem_global_budgets.rs, codex_responses_compat.rs, wire_stream.rs, and server_auth.rs.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Run `cargo test --manifest-path rust/Cargo.toml --test hosted_tool_orchestration -- --test-threads=1` and local live model + Eggsearch evidence. Record per-step usage and cancellation/failure outcomes; verify standalone default routing has zero additional overhead.

## 12. Documentation

docs/api-reference.md (explicit nonstandard route, protocol and limits), docs/agent-configuration.md, docs/deployment.md, docs/firewall.md, docs/stateless-responses.md (unchanged ordinary tool execution), architecture/overview.md and relevant coordinator/identity deep dives; add supported model/tool matrix and a sanitized curl example.

## 13. Acceptance criteria

One opt-in authenticated request completes a bounded model/tool/model operation with correct tool evidence. A regular Chat/Responses/Messages request with identical tool definitions does not cause EggPool to invoke Eggsearch. The initiating user's model/tool grants and budgets apply to every nested step; known/unknown usage, denials, errors and cancellation are accurately represented. No persistent prompts/results and no hung background tasks.

## 14. Stop conditions

Stop if supported providers lack a trustworthy completed function-call grammar; if step admission would bypass existing hard budgets; if an implementation needs stored Responses or secondary retry/finalization owner; or if server handler code begins owning orchestration. Require a separate ADR for streaming/background/sandbox execution.

## 15. Closure evidence

Public endpoint contract, implementation SHA, exact resource/step caps, deterministic and live traces with content removed, user budget attribution comparison, old endpoint byte/protocol regression, error/cancellation matrix, security review, default/no-default test results and footprint. Do not close based on a single happy-path chat.

## 16. Handoff notes

M004 is a new explicit application-mode surface, not a change in ordinary proxy API behavior. Keep it behind a disabled-by-default feature/config setting and use the M003 internal tool invocation API rather than proxying through the new public MCP endpoint.
