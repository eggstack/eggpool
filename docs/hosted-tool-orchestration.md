# Finite hosted Eggsearch orchestration

`POST /api/agent/v1/run` is a separately versioned EggPool operation for one
finite, stateless model → Eggsearch tool → model sequence. It does not change
ordinary Chat Completions, Responses, or Messages behavior: those APIs continue
to return tool calls to the client and EggPool does not execute them.

The route is absent unless `[tools].enabled` and
`[tools].hosted_execution_enabled` are both true. `gateway_enabled` is only
needed for the public MCP route. A hosted request requires a managed agent
token with the `inference` scope and current Eggsearch discovery/invocation
scopes and grants. Every model turn uses the same generation lease and normal
coordinator admission, model policy, provider routing, budgets, cancellation,
and finalization. Every tool call rechecks its current grant and the backend
catalog fingerprint.

## Model qualification and request

Operators must explicitly list exact model IDs they have qualified for finite
OpenAI Chat Completions function-call output:

```toml
[tools]
enabled = true
hosted_execution_enabled = true
hosted_models = ["local-tool-model"]
base_url = "http://127.0.0.1:11320/mcp"
```

The operator must separately configure grants for each user and tool. A
request selects only names from EggPool's fixed Eggsearch catalog; clients
cannot submit schemas, backend URLs, or arbitrary MCP methods.

```sh
curl http://127.0.0.1:11300/api/agent/v1/run \
  -H "Authorization: Bearer $EGGPOOL_AGENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -d '{"mode":"eggsearch","model":"local-tool-model","input":"Find current release notes and summarize the changes.","tools":["web_search"],"max_iterations":3,"max_tool_calls":4}'
```

The required fields are `mode` (`eggsearch`), `model`, `input`, and `tools`.
The request is finite and stateless; `stream`, unknown fields, and native
hosted-tool grammar are rejected. `tools` may be empty for a model-only call.
Successful responses contain an EggPool orchestration ID, the final assistant
answer, and bounded iteration/tool-call counts. Errors use a sanitized
`error.code` and do not include prompts, provider bodies, or tool results.

## Enforced limits

- Request body: 64 KiB; user input: 48 KiB.
- At most 5 model iterations and 8 total tool calls; defaults are 3 and 4.
- At most 256 output tokens per model turn.
- Each serialized tool result: 64 KiB; total results: 256 KiB.
- Total operation deadline: 120 seconds.
- At most 8 active hosted operations globally and 2 per user; Eggsearch also
  retains its independent 16-operation limit.
- Tool calls execute sequentially and are never automatically retried.

Only a finite assistant completion with `finish_reason=stop` terminates
successfully. Tool output is framed as `external_untrusted` model input. A
model/backend failure ends the request; EggPool does not synthesize a success
or continue work in the background. Each internal model turn is accounted
under the initiating user and carries the content-free orchestration ID in
request attribution. Prompts and results are not persisted.

See the [MCP gateway guide](tool-gateway.md), [API reference](api-reference.md),
and [stateless Responses contract](stateless-responses.md).
