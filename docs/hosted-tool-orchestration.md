# Hosted tool orchestration

EggPool does not run a hosted tool loop. `POST /api/agent/v1/run` is
not routed. Ordinary Chat Completions, Responses, Messages, and compact
still return tool calls to the caller, and the caller executes them.

See [Tool execution](tool-gateway.md).
