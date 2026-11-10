# Tool execution

EggPool does not host an MCP gateway and does not execute tools. Chat
Completions, Responses, Messages, and compact return tool calls to the
client. EggUI and CodeGG execute those calls in their own processes.

`POST /api/tools/v1/mcp` and `POST /api/agent/v1/run` are not routed.
Setting `[tools].enabled`, `[tools].gateway_enabled`, or
`[tools].hosted_execution_enabled` to true fails `eggpool check-config`
and process startup. Omit `[tools]`, or leave those switches false.

Point a client that needs search or other tools at that client's own
tool loop, or at Eggsearch directly. Use a personal EggPool token with
the inference scope for model calls.
