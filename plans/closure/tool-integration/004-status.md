# Tool Integration Milestone M004 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/tool-integration/004-finite-hosted-tool-orchestration.md`

Source subsystem roadmap:

- `plans/subsystems/tool-integration-roadmap.md#7-milestones`

Repository baseline reviewed: `088a1379` (embedding implementation commit; M004 implementation commit `5451be62`)

Implementation commits or pull requests:

- `5451be62` — bounded hosted model/tool/model orchestration and deterministic end-to-end coverage.
- `088a1379` — additional embedding integration and a smaller live M004 qualification request using bounded `web_fetch` output.
- `73eac4c6` — a complete live Ollama model→tool→model trace through a local MCP fixture.

## 1. Executive finding

The finite, opt-in hosted operation is implemented and its deterministic
model→tool→model integration, authorization failures, cancellation, disabled
route, and backend isolation tests pass. The live Eggsearch 0.4.3 + Ollama
`qwen3.5:4b` run did not complete: the first `web_search` attempt returned
`tool_backend_invalid_response` and a later reduced `provider_status` result
returned `tool_result_limit_reached`; a third attempt using `web_fetch` with a
small public page timed out at the configured 120-second operation deadline.
The typed timeout was returned as designed. Live hosted orchestration is
therefore not claimed as qualified. Conditional closure preserves the
implemented finite capability while requiring a successful live trace before
promoting this milestone to unconditionally closed.

A separate live Ollama `qwen3.5:4b` run against a local MCP fixture completed
two model turns and one tool invocation. The initial run took 40.95 seconds;
the final no-default-features rerun took 65.54 seconds. Both returned a
non-empty answer and recorded exactly one backend invocation. The current
256-token per-turn output cap and 120-second operation deadline were adequate
for this trace; no limit adjustment is indicated. This qualifies the live
model/tool/model cycle and its latency, while the real Eggsearch backend trace
remains a distinct, unresolved integration requirement.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Explicit, finite, disabled-by-default hosted operation | `rust/src/agent_orchestration.rs`, `rust/src/server/hosted.rs`, `rust/tests/mcp_gateway.rs::disabled_gateway_route_is_not_registered` | pass | Existing inference APIs do not acquire tool execution semantics. |
| Model/tool/model flow preserves canonical tool identity and authorization | `rust/tests/mcp_gateway.rs::hosted_endpoint_completes_finite_model_tool_model_flow`; `hosted_endpoint_rejects_unsupported_grammar_and_unauthorized_tools` | pass | Uses deterministic local model and MCP fixtures. |
| Deadline, cancellation, result bounds, and backend health isolation | `rust/tests/mcp_gateway.rs::hosted_client_disconnect_cancels_in_flight_tool_call`; `unavailable_backend_is_isolated_from_model_health` | pass | The live timeout also exercised the public 120-second timeout response. |
| Live Eggsearch and compatible model complete a full cycle | Ignored live test run against Eggsearch 0.4.3 and Ollama `qwen3.5:4b` | partial | Two earlier live attempts failed typed validation limits; bounded `web_fetch` run ended with HTTP 504 `hosted_execution_timeout` after 120 seconds. |
| Live model/tool/model cycle and current output/latency limits | `rust/tests/mcp_gateway.rs::live_ollama_hosted_model_tool_model_with_mcp_fixture` | pass | Live Ollama completed two model turns around one local MCP fixture call in 40.95 seconds and again in 65.54 seconds; 256 output tokens/turn and 120-second operation deadline were sufficient. |
| Default/no-default compatibility and Rust quality gates | Full suites recorded below; default/no-default Clippy and no-default check passed | pass | No-default parity retained. |
| Operator and API documentation | `docs/api-reference.md`, `docs/agent-configuration.md`, `docs/deployment.md`, `docs/stateless-responses.md`, `architecture/overview.md` | pass | Ordinary inference semantics and limits are stated separately. |

## 3. Production implementation evidence

`rust/src/agent_orchestration.rs` owns the finite sequencing and bounds.
`rust/src/server/hosted.rs` and the server route are thin ingress adapters.
Every model turn uses the existing coordinator, and tool calls go through the
crate-private bounded Eggsearch invocation interface rather than the public
MCP HTTP route. No persistent conversation, background continuation, or
arbitrary backend URL was added.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test mcp_gateway -- --test-threads=1
EGGPOOL_TEST_EGGSEARCH_URL=http://127.0.0.1:11320/mcp cargo test --manifest-path rust/Cargo.toml --test mcp_gateway live_ollama_eggsearch_hosted_orchestration -- --ignored --exact --nocapture --test-threads=1
```

### Results

- Formatting and strict Clippy passed (default and no-default feature sets).
- No-default workspace check passed.
- Full default suite: 94 targets, 1,241 passed, 4 ignored, 0 failed.
- Full no-default suite: 94 targets, 1,242 passed, 4 ignored, 0 failed.
- Focused MCP gateway suite: 6 passed, 2 ignored, 0 failed.
- Live qualification command failed its assertion after receiving the expected typed 504 timeout at 120 seconds; earlier live attempts produced the typed result-size/invalid-response failures above.
- Live local-fixture model/tool/model qualification passed twice: 40.95 seconds
  in the initial run and 65.54 seconds in the final no-default-features rerun.
  Both used a real loopback Ollama model and an in-process MCP fixture. Neither
  is represented as a live Eggsearch qualification.

These are local results, not CI claims.

## 5. Invariant review

- Default inference endpoints remain transparent and do not invoke Eggsearch.
- Each nested model call uses the existing principal, coordinator, and budget
  authorities; each tool call reauthorizes against current grants.
- Tool results remain bounded and external-untrusted; they do not gain
  instruction priority.
- No prompts, tool results, credentials, or persistent orchestration state
  were added to logs or storage.

Evidence: deterministic orchestration, authorization, cancellation, route
registration, and health-isolation tests listed above; full default and
no-default workspace suites.

## 6. Failure and recovery review

The operation is finite and stateless. Client disconnect cancels an active
tool call; backend failures do not poison provider health; the global deadline
returns a typed timeout. A process restart loses in-flight progress and has no
resume behavior. The live local-model runs completed within the existing
deadline; Eggsearch-backed result shape and end-to-end latency remain an
operational qualification risk, not an unhandled background task.

## 7. Migration and compatibility review

No M004-specific migration or durable state was added. Existing inference and
MCP contracts remain separate. The endpoint is opt-in and unsupported
streaming/tool grammar fails closed.

## 8. Security review

The path requires managed-token authentication and applicable inference/tool
permissions. Backend tool identity is drawn from the validated catalog;
request-supplied URLs and unlisted tool names are rejected. Request/result
bytes, iteration count, tool calls, output tokens, and wall time are bounded.
Content-free diagnostics are retained.

## 9. Documentation and operations

The API reference documents the nonstandard finite operation, limits, and
scope. Deployment and agent configuration docs describe disabled-by-default
setup. `docs/stateless-responses.md` retains client-owned behavior for ordinary
inference APIs.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Live Eggsearch 0.4.3 + Ollama hosted execution did not complete within the 120-second operation deadline; two other live attempts failed typed output validation bounds. | The live model/tool/model cycle is qualified with a local MCP fixture, but the Eggsearch backend integration has no successful hosted trace. | Repeat with a pinned compatible model and bounded Eggsearch tool result; capture one successful Eggsearch-backed live model→tool→model response and verify nested usage attribution. |
| low | Physical SBC disabled/enabled footprint and contention comparison was not run on this x86 host. | The embedded/solo overhead target has no device-specific measurement. | Capture the planned paired physical-SBC measurements before claiming SBC footprint qualification. |

## 11. Roadmap disposition

Conditionally closed. The production implementation and deterministic
acceptance suite are complete. Keep the live qualification and physical SBC
measurement conditions visible; do not describe M004 as live-qualified until
the required traces pass.

## 12. Registry updates

The registry and Tool Integration roadmap now link this record and mark M004
conditionally closed. M003 remains conditionally closed on its own physical
SBC evidence. No newly-ready successor was identified; Retrieval
Interoperability R001 remains blocked on a backend-enforced delegation/ACL
contract, independently of hosted orchestration.
