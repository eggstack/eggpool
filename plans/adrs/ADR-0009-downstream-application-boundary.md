# ADR-0009: Downstream application boundary

Status: accepted

Date: 2026-11-10

Decision owners: project maintainers (explicit product direction, 2026-11-10)

Related specification sections:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#6-non-goals`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`
- `plans/003-planning-process.md` §2.2

Affected subsystem roadmaps:

- `plans/subsystems/application-boundary-roadmap.md`
- `plans/subsystems/tool-integration-roadmap.md`
- `plans/subsystems/retrieval-interoperability-roadmap.md`
- `plans/subsystems/embedding-routing-roadmap.md`

## Context

EggPool is the LAN inference gateway in a local stack whose application
owners are EggUI (single-owner chat workspace) and CodeGG (coding harness).
Both already execute tools. EggUI talks to Eggsearch directly and hosts
loopback MCP Apps plugins. CodeGG is an MCP client for arbitrary local and
remote servers and runs its own Eggsearch sidecar. Ordinary EggPool
Chat Completions, Responses, and Messages already return tool calls to the
client.

ADR-0007 added an opt-in Eggsearch MCP gateway and a finite hosted
model-tool-model route so EggPool could approach a PrivateGPT-style
application API. That gateway is disabled unless `[tools]` is enabled, is
pinned to the Eggsearch 0.4.3 catalog, and is not on the path EggUI or
CodeGG use. Retrieval interoperability R001 then proposed a document
search/fetch adapter on the same gateway. Those surfaces duplicate the
downstream tool loops and would make EggPool a second agent.

The inference routes, managed identity, model policy, budgets, embeddings
route, and client-executed tool-call transcoding remain the flexible
contract for EggUI, CodeGG, and any other OpenAI-compatible or
Anthropic-compatible caller.

## Decision drivers

- Keep one product: a single on-prem inference gateway, not an application
  runtime and not a second agent loop.
- Leave conversation storage, document grounding, citations, web search,
  skills, shell, and MCP execution with the application that owns them.
- Preserve client-executed tool calls on the existing inference APIs so
  downstream and third-party clients keep a complete tool loop.
- Keep `POST /v1/embeddings` as routed inference. Do not add a vector
  store, ingest API, or citation store.
- Keep prompts, tool arguments, tool results, and raw bodies out of
  EggPool persistence and diagnostics.
- Discharge the open tool-gateway and hosted-execution evidence
  obligations by withdrawing the surface, rather than by collecting
  further Eggsearch or SBC proof for a surface the stack does not call.

## Considered options

### Option A — Keep the gateway dormant

Leave `POST /api/tools/v1/mcp` and `POST /api/agent/v1/run` in the tree,
disabled by default, and stop extending them. Cost: a third tool policy,
an Eggsearch 0.4.3 catalog pin, and a standing invitation to add
retrieval, skills, and remote MCP inside the proxy. EggUI and CodeGG
still would not call it.

### Option B — Withdraw gateway execution and keep the inference contract

Remove the MCP gateway, the hosted runner, and their operator surface.
Keep Chat Completions, Responses, Messages, compact, and embeddings.
Keep returning tool calls to the caller. Record external document
retrieval as out of product scope. Add only gateway facts the
applications do not already store: content-free cross-app audit, an
offline metadata profile, and a no-runtime-change measurement record.

### Option C — Move the tool loop into EggPool

Expand hosted execution into the shared agent for EggUI and CodeGG.
Rejected. Both applications already own their loops, permissions, and
MCP clients, and EggUI's specification forbids using EggPool as its
authorization surrogate or conversation store.

## Decision

Option B.

1. **Inference contract.** EggPool serves Chat Completions, stateless
   Responses, remote compact, Anthropic Messages, and explicitly
   qualified embeddings. A tool definition on those routes is data for
   the caller. EggPool transcodes completed tool calls and does not
   execute them.
2. **Withdrawn execution.** The authenticated MCP gateway, the finite
   hosted route, Eggsearch backend configuration, tool-grant
   administration, and discovery/invocation as a reason to call EggPool
   are withdrawn. Eggsearch remains an independent process that EggUI
   and CodeGG connect to themselves.
3. **Identity stays on inference.** Local users, personal tokens, model
   profiles, and per-user and instance budgets continue to govern who
   may call which model. They do not become an application login, a
   document ACL, or a directory service.
4. **Retrieval stays downstream.** EggPool does not ingest documents,
   store chunks or vectors, serve private-document search, or delegate
   document ACLs. `POST /v1/embeddings` remains available for a caller
   that embeds through the gateway.
5. **Audit is gateway facts only.** An operator export may expose
   content-free administration and inference attribution that already
   belongs to this process. It does not store prompts, bodies, tool
   arguments, or tool results.
6. **Offline metadata is configuration.** A local profile and doctor
   must be able to refuse outbound model-info, pricing, and update
   sources. That is not a new inference server and not a Kubernetes
   packaging change.
7. **Measurement does not authorize a runtime change.** A shared-process
   record may describe how EggUI-like and CodeGG-like call mixes use the
   existing process. It does not change the single SQLite gate, the
   current-thread runtime, or the routing lock.

## Consequences

### Positive

- EggUI and CodeGG remain the tool and MCP owners.
- Third-party clients keep a stable inference API and client-side tools.
- Open tool-integration evidence conditions and the blocked retrieval
  adapter stop being active work.

### Negative

- A deployment that enabled `[tools]` loses that route and must point
  its client at Eggsearch, EggUI, or CodeGG.
- Historical v65 tool-grant tables remain in migrated databases until a
  later milestone proves a drop is safe. This decision does not drop
  them.

### Neutral or deferred

- LDAP, a workspace UI, content connectors, batch inference, and bundled
  workflow engines stay out of this decision.
- Syslog or HTTP delivery of the audit export waits until a consumer
  asks for it. The first consumer is a local export.
- Physical shared-host timings stay `not measured` when that host is
  unavailable. Absence of a timing is not permission to retune SQLite.

## Compatibility and migration

Chat, Responses, compact, Messages, embeddings, model list, health,
identity, and budget routes stay. `POST /api/tools/v1/mcp` and
`POST /api/agent/v1/run` go away. A config with a `[tools]` block that
turns the gateway or hosted execution on fails closed at check and
startup with a typed error. A config that omits `[tools]`, or that
leaves those switches off, still starts. Historical migrations through
v65 stay immutable. Discovery and invocation scopes already stored on
tokens no longer authorize a route. Issuing those scopes stops.
`classify_transition` remains the only reload authority for whatever
`[tools]` keys remain readable.

## Security and reliability implications

Removing the gateway removes a second authenticated entry to Eggsearch
and the hosted model-tool loop. Inference authentication, model policy,
and budgets are unchanged. Audit export is administrator-only, bounded,
and content-free. The offline doctor must fail closed while an outbound
metadata source is enabled, and it must not print secrets or fetched
catalog bodies.

## Verification

Withdrawal is proven by the absence of the tool routes, a startup error
for an enabled `[tools]` block, and an unchanged client-executed tool
call on Chat Completions, Responses, and Messages. Embeddings admission
stays covered by the existing embeddings tests. Audit export fixtures
contain no prompt, body, or tool payload. The offline doctor fails on
an enabled OpenRouter model-info source and passes on the local
profile. The measurement plan records aggregate scalars or `not
measured` and changes no runtime default.

## Supersession

This ADR supersedes ADR-0007's decisions to expose an authenticated MCP
gateway and a finite hosted tool runner. ADR-0007's rule that ordinary
inference returns tool calls to the client and does not execute them
stands, and it is restated here as decision 1. ADR-0008's embedding
inference boundary stands. The retrieval roadmap's pattern B adapter is
not authorized.

## Acceptance record

Accepted 2026-11-10 from explicit product direction: EggPool is used
primarily in front of EggUI and CodeGG, tool execution stays in those
applications, and the gateway keeps enough of an inference contract for
other OpenAI-compatible and Anthropic-compatible callers.
