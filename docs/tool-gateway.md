# EggPool MCP tool gateway

EggPool can expose one operator-configured Eggsearch service through a narrow,
authenticated MCP route. The gateway is disabled unless both
`[tools].enabled` and `[tools].gateway_enabled` are true. It uses the existing
EggPool listener and does not change Chat Completions, Responses, Messages, or
their client-executed tool behavior.

## Supported contract

The route is `POST /api/tools/v1/mcp`. It supports MCP protocol version
`2025-03-26` JSON POST requests for `initialize`,
`notifications/initialized`, `tools/list`, and `tools/call`. It does not
provide general Streamable HTTP compatibility: JSON-RPC batches, resources,
prompts, GET/SSE streams, pagination, and session resumption are unsupported.
The Eggsearch backend must be version 0.4.3 and must advertise the exact
canonical tool catalog. See the [API reference](api-reference.md#optional-tool-gateway)
for request and resource limits.

When a tool schema explicitly offers `response_detail: "compact"`, EggPool
selects that projection when the caller does not specify a detail level. An
explicit caller value is validated against the current schema and preserved.

## Authorization and trust

Every request requires a managed user's personal token. Discovery and
invocation use separate token scopes, and each tool must also be granted by the
user's effective tool policy. The gateway rechecks authorization when a tool is
called, so an existing MCP session does not preserve access after credential
revocation or a policy change. Browser sessions, shared server credentials,
and dashboard-public access do not authorize this route.

The backend address is operator-owned and restricted to a literal loopback
HTTP address; clients cannot select a host or path. Downstream credentials,
request arguments, and tool results are not forwarded to logs or persisted by
EggPool. Tool output preserves Eggsearch's structured result, error, and trust
metadata. Content marked `external_untrusted` remains untrusted data and must
not be treated as system or developer instructions by a consuming agent.

MCP sessions are random, in-memory, bound to the user, credential, and current
auth epoch, and expire after 30 minutes of inactivity. A process restart
invalidates them. Each list or invocation uses a fresh short-lived Eggsearch
session. Catalog changes invalidate calls made against an older discovery
fingerprint. Backend failure affects only the tool request and does not alter
model provider health.

## Setup

1. Install and supervise Eggsearch independently, bound to loopback.
2. Set `[tools].enabled = true`, `[tools].gateway_enabled = true`, and the
   loopback `base_url` in EggPool configuration; restart EggPool.
3. Run `eggpool tools doctor --json` to verify the backend.
4. Issue a personal token with only the required discovery/invocation scopes.
   Grant exact tools with `eggpool users tool-policy` and verify them with
   `eggpool users tool-policy show`.
5. Configure the MCP client for the EggPool route and provide the token through
   the client's secret store.

The gateway intentionally supports a small method subset. Clients that
require GET/SSE transport or resumable sessions should connect to Eggsearch
directly where their deployment permits it. See [agent configuration](agent-configuration.md#optional-eggpool-mcp-tools),
[deployment](deployment.md#optional-eggsearch-service), and
[firewall guidance](firewall.md#optional-eggsearch-tools).
