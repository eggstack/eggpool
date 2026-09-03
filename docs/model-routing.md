# Semantic Model Routing

EggPool has two independent routing layers. Provider/account routing chooses a
healthy account for one concrete model using priority, quota, load, and health.
Semantic model routing chooses which configured concrete model should handle a
virtual alias; after that choice, the ordinary provider/account router owns the
request.

The feature is optional and disabled by default. With no
`[model_routers.*]` tables, requests, catalog output, database work, background
tasks, and concrete-model behavior are unchanged.

## Configuration

The smallest useful router has a virtual client-facing alias, a concrete
selector model, a concrete default, and at least one labelled route. This
example is copyable, but the model names are illustrative rather than bundled
availability guarantees:

```toml
[model_routers.implementer]
selector_model = "qwen3-0.6b/llamacpp-local"
default_model = "muse-spark-1.3"
sticky = true
affinity_ttl_s = 43200
selector_timeout_s = 2.0
max_input_bytes = 2048
repair_attempts = 1

[model_routers.implementer.routes.default]
model = "muse-spark-1.3"
description = "Use for general implementation requests and as the fallback."

[model_routers.implementer.routes.hard]
model = "gpt-5.6-luna"
description = "Use for the most difficult implementation and reasoning tasks."

[model_routers.implementer.routes.research]
model = "research-model"
description = "Use for research-heavy synthesis and long technical reading."
```

`selector_model` and each route `model` use ordinary EggPool concrete model
references. A provider-qualified reference such as
`qwen3-0.6b/llamacpp-local` is passed through the existing provider-scoped
parser. There is no second provider field inside a route.

Virtual IDs are exact aliases and cannot contain `/`. Router definitions are
structurally validated during config parsing and compiled before a generation
is published. Current catalog availability is intentionally not required for
config parsing; normal request-time catalog, capability, and health checks
remain authoritative.

The fields and bounds are:

| Field | Meaning | Bounds/default |
|---|---|---|
| `selector_model` | Concrete model used for bounded classification | required |
| `default_model` | Concrete fallback; must equal a route model | required |
| `routes.<label>.model` | Concrete target | required |
| `routes.<label>.description` | Selector policy text | required, max 512 UTF-8 bytes |
| `sticky` | Enable process-local model affinity | `true` |
| `affinity_ttl_s` | Affinity lifetime | 1–604800 seconds; `43200` |
| `selector_timeout_s` | Total selector plus repair deadline | 0.05–30 seconds; `2.0` |
| `max_input_bytes` | Bounded semantic input | 128–16384 bytes; `2048` |
| `repair_attempts` | One fixed repair attempt after invalid output | `0` or `1`; `1` |

Virtual IDs and route labels are limited to 128 UTF-8 bytes; model references
to 128 bytes; the compiled static policy to 64 KiB. Empty values, control
characters, nested virtual targets, and empty route maps are rejected.

## Selector behavior

Choose a small, local, low-latency selector that follows exact-output
instructions and understands the configured distinctions; it only classifies,
so domain breadth matters less than instruction-following and speed.

The selector receives a deterministic minified prompt containing the route IDs
and descriptions plus a bounded semantic view of the request. It receives
system/developer text, the relevant user text, and coarse feature flags such as
tools, image, PDF, audio, or reasoning. Full conversation history, tool
schemas, tool results, binary content, and raw session headers are excluded to
bound cost, latency, and privacy exposure.

The selector must answer with exactly one compiled route ID. A malformed,
unknown, oversized, unavailable, or timed-out response never becomes a model
name. With `repair_attempts = 1`, EggPool makes one fixed repair request only
after a successful 2xx response has invalid route text. The repair reuses the
same already-bounded semantic request view and adds only a fixed instruction;
it does not include the invalid answer, tools, tool results, or binary content.
If the first or repair response is non-2xx, selection is immediately classified
as `unavailable` and the default is used. If a successful repair response is
still invalid, the result is `repair_failed`. A selector failure with a healthy
default is therefore transparent to the client; if the default itself is
unavailable, the client receives the normal concrete-model availability error.

Selector calls are ordinary non-streaming concrete EggPool requests. Their
usage, quota, latency, and cost appear in the existing request/accounting
surfaces. The eventual target request is separate. Once target submission
begins, ordinary account/provider failover may occur for that concrete model,
but EggPool does not semantically reselect another route.

## Sticky affinity and sessions

With `sticky = true`, EggPool keeps a bounded process-local TTL/LRU decision
from a session identity to a concrete model. It never pins a provider or
account, bypasses health/quota routing, or persists a conversation. The cache is
keyed by the virtual alias, router semantic fingerprint, and a hash of the
identity.

Clients with a stable conversation identifier should send:

```http
X-EggPool-Route-Session: project-42-conversation-7
```

The header is EggPool-local. Its value is hashed immediately, never logged,
persisted, included in metrics, or forwarded to an upstream provider. It has
no effect on concrete model requests. For stateless OpenAI Responses calls,
where server-side conversation state is intentionally unsupported, this
explicit header is the recommended way to carry affinity across independent
requests. Chat Completions and Messages may derive a conservative identity
from a bounded system/developer prefix and first user turn when the header is
absent. Field framing is included in the existing 4096-byte identity budget,
and a reserved portion of that budget always includes bytes from the first user
turn when it exists, even if the shared system/developer prefix is very large.

`sticky = false` invokes the selector for every request. Affinity is lost on
process restart. A safe rehash preserves entries only when the router's
semantic fingerprint is unchanged; changing the selector, default, route
target/description, or affinity policy causes the next request to reclassify.

## Operations and troubleshooting

The complete `[model_routers.*]` mapping is live-reloadable as one atomic field.
An invalid candidate is rejected before publication, leaving the old
generation and its affinity behavior active. Removing a router makes its old
cache entries unreachable. See [Live Configuration Rehash](live-config-rehash.md).

Bounded semantic counters and latency are visible under the authenticated
`model_router` object in `/api/stats/runtime`. They include virtual request
counts, selector/default decisions, affinity hits/misses, fixed fallback
reasons, repair attempts/successes, and bounded virtual-to-concrete selection
pairs. They do not contain prompts, selector output, route descriptions, or
session IDs.

If requests consistently use the default, check the runtime snapshot for
`timeout`, `unavailable`, `invalid_output`, or `repair_failed`, then verify
that the selector model is a normal concrete model available to EggPool and
that descriptions are short, mutually distinguishing, and focused on
operator intent. Descriptions should say when a route is appropriate—not
repeat a model name or expose a private prompt.

Limitations: routers cannot nest; semantic routing is not an authorization
boundary; affinity is not restart-persistent; route descriptions do not grant
model availability; and there is no semantic failover after a target has been
submitted.
