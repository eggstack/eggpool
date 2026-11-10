# Application Boundary Roadmap

Status: closed

Long-term references:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#6-non-goals`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Related ADRs:

- `plans/adrs/ADR-0009-downstream-application-boundary.md` (accepted)
- `plans/adrs/ADR-0008-embedding-inference-surface-boundary.md` (accepted; embeddings stay)
- `plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md` (superseded)
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted; identity stays inference-scoped)

## 1. Purpose and ownership boundary

Keep EggPool the inference gateway that EggUI, CodeGG, and other
compatible callers share, and take back the tool-execution surface that
overlaps those applications.

EggPool owns admission, wire adaptation, provider routing, identity,
model policy, budgets, embeddings inference, content-free operator
records, and local process configuration. EggUI owns chat, attachments,
citations, memory, web search, and its MCP plugins. CodeGG owns the
coding loop, shell, skills, permissions, and MCP client connections.
Eggsearch remains an independent process those applications connect to
themselves.

Runtime owners stay `rust/src/server/`, `rust/src/coordinator/`,
`rust/src/wire/`, `rust/src/identity.rs`, `rust/src/config.rs`, and
`rust/src/config_reload_policy.rs`. This roadmap does not move
conversation storage, document indexes, or tool execution into those
modules.

## 2. Work classification

### Invariants

- One admission path, one SQLite gate, client-executed tool calls on
  inference routes, and content-free persistence.
- `[tools]` cannot silently turn execution back on.
- A measurement record is not a runtime-policy change.

### Capabilities

- The MCP gateway and hosted runner are gone, and an enabled `[tools]`
  block fails closed with a typed error.
- An administrator can export content-free gateway audit facts.
- An operator can select a local profile that the doctor rejects while
  any outbound metadata or update source is enabled.

### Infrastructure

- None as a user-visible claim. Schema v65 stays in history. This
  roadmap does not drop those tables.

### Polish

- A shared-process workload record, or an explicit `not measured`, with
  no change to SQLite, the current-thread runtime, or the routing lock.

## 3. Non-goals

No replacement agent loop, no arbitrary MCP client or server, no
document ingest, vector index, citation store, or document ACL. No
LDAP, workspace UI, content connector, batch API, or bundled workflow
engine. No prompt or tool-payload retention. No change to EggUI or
CodeGG repositories. No second reload-key list. No edit to historical
migrations v1–v65.

## 4. Current state

`POST /api/tools/v1/mcp` and `POST /api/agent/v1/run` are not routed.
A true `[tools]` execution switch fails `check-config` and startup.
Ordinary inference still returns tool calls to the caller through
`rust/src/wire/`. `POST /v1/embeddings` is live. v65 grant tables remain
and are unread. `eggpool audit export` and `GET /api/admin/audit/export`
project administration and logical inference facts without prompts.
OpenRouter model-info still defaults on. `check-config --local` and
`rust/config.local.example.toml` are the opt-in local profile.
`scripts/qualification_ordinary_server.py` is a frozen concurrency
corpus. M004 is `not measured` and authorizes no runtime change.

Retrieval R001 is superseded for execution by R002. Tool-integration
M003 and M004 stay conditionally closed, and their open evidence
conditions are vacated.

## 5. Target architecture

Callers use the inference routes and execute their own tools. EggPool
config either omits `[tools]` or carries it only with the execution
switches off. An enabled execution switch is a startup and
`check-config` error. Audit export reads existing administration and
request-attribution rows and returns scalars and identifiers. The local
profile disables outbound model-info, pricing catalogs, and the update
checker, and `check-config` fails while any of those sources are on
when the profile is selected. Shared-process evidence is an artifact
outside the runtime, or the words `not measured`.

## 6. Dependency graph

- M001: **hard** accepted ADR-0009. No dependency on retrieval R002.
- M002: **soft** M001, so the export does not grow a tool-payload field
  while the gateway still exists. M002 may proceed against the ADR
  contract before M001 merges if it never reads tool arguments.
- M003: no hard dependency. **Interface** the existing
  `classify_transition` authority.
- M004: **operational** availability of the shared host. No hard
  dependency on M001–M003. Explicitly not a dependency of any runtime
  change.
- Retrieval R002: **soft** this roadmap. It records the documentation
  consequence of ADR-0009 and does not block M001.

## 7. Milestones

### M001 — Withdraw MCP gateway and hosted execution

Class: capability

Objective: remove EggPool-executed tools and reject an enabled
`[tools]` block, while Chat, Responses, Messages, and embeddings keep
their current client-executed behavior.

Dependencies: accepted ADR-0009.

Deliverable boundary: routes, hosted controller, tool CLI, and grant
writers. v65 tables stay. Wire tool-call transcoding stays.

User or operator value: one tool owner per application, and a config
error instead of a silent second agent.

Exit conditions: the two routes are absent; enabled `[tools]` fails
check and startup; a Chat Completions tool call is still returned to
the client; default and no-default serial suites pass.

Deferred work: dropping v65 tool tables; syslog audit; retrieval docs
owned by retrieval R002.

### M002 — Content-free gateway audit export

Class: capability

Objective: give an administrator one bounded export of administration
actions and inference attribution that EggUI and CodeGG do not share
with each other.

Dependencies: ADR-0009. Soft M001.

Deliverable boundary: read and project existing rows. Add a scalar
column only when a terminal budget outcome is not already recoverable.
No new body store.

User or operator value: one local record of who called which model,
with what result and token counts, across both applications.

Exit conditions: export fixtures contain identifiers, counts, status,
and timestamps, and contain no prompt, body, tool argument, or secret.
Members cannot read another user's rows.

Deferred work: syslog, HTTP delivery, and prompt retention.

### M003 — Local metadata profile and doctor

Class: capability

Objective: make a fully local EggPool configuration checkable, so a
gateway in front of a local model does not call out for model metadata
or updates.

Dependencies: none. Interface `config_reload_policy.rs`.

Deliverable boundary: one documented profile and a failing doctor.
Inference behavior is unchanged.

User or operator value: an offline stack can prove it is offline.

Exit conditions: the profile disables OpenRouter model-info, both
optional metadata sources, pricing catalogs, and the update checker.
`check-config` fails while any of those are enabled under the local
profile, and passes when they are off. No secret or response body is
printed.

Deferred work: air-gapped artifact mirroring and a new update
transport.

### M004 — Shared-process workload record

Class: polish

Objective: write down how a mixed EggUI-like and CodeGG-like call
pattern should be observed on the existing process, and record either
aggregate scalars from the shared host or `not measured`.

Dependencies: operational host availability.

Deliverable boundary: no Rust runtime change and no edit to the frozen
ordinary-server corpus.

User or operator value: a decision record before anyone proposes a
SQLite or runtime change for this stack.

Exit conditions: the closure says `not measured` or attaches aggregate
scalars only. It authorizes no gate, runtime, or lock change.

Deferred work: any optimization plan, which would be a new roadmap.

## 8. Cross-cutting requirements

Storage: no prompt, body, cache key, or tool payload. v1–v65 immutable.
Protocol: inference and embeddings routes unchanged; tool execution
routes removed. Security: audit export is admin-only and bounded.
Concurrency: M004 must not add a writer or a hot-path metric. Reload:
only `classify_transition`. Docs: API, CLI, configuration, tool
gateway, hosted orchestration, and the architecture ownership notes
that name the withdrawn routes.

## 9. Verification strategy

Serial `cargo test --manifest-path rust/Cargo.toml --workspace
--all-targets -- --test-threads=1`, the same suite with
`--no-default-features`, and strict Clippy. Focused targets include
`rust/tests/mcp_gateway.rs`, `rust/tests/tool_grants.rs`,
`rust/tests/tool_backend_config.rs`, `rust/tests/embeddings_api.rs`,
`rust/tests/wire_codecs.rs`, and `rust/tests/on_prem_attribution.rs`,
replaced or extended as each milestone says. M004 is tooling-only and
is not a CI gate.

## 10. Risks and decision points

Dropping v65 tables is out of this roadmap until a later plan proves
the policy-revision triggers and backup restore. A deployment that
depended on hosted execution has no in-proxy replacement; the error
must name that. M002 must not become a transcript log. M004 must not
be read as capacity evidence from a developer workstation.

## 11. Completion definition

M001, M002, and M003 have accepted closure records. M004 has a closure
record whose disposition is either aggregate scalars from the shared
host or `not measured`. The tree no longer serves EggPool-executed
tools. Embeddings and client-executed tool calls still work. No
runtime concurrency default has changed.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| M001 withdraw gateway and hosted execution | implemented | `plans/implementation/application-boundary/001-withdraw-mcp-gateway-and-hosted-execution.md` | `plans/closure/application-boundary/001-status.md` | none |
| M002 content-free audit export | implemented | `plans/implementation/application-boundary/002-content-free-gateway-audit.md` | `plans/closure/application-boundary/002-status.md` | none |
| M003 local metadata profile | implemented | `plans/implementation/application-boundary/003-local-metadata-profile.md` | `plans/closure/application-boundary/003-status.md` | none |
| M004 shared-process record | implemented | `plans/implementation/application-boundary/004-shared-process-workload-record.md` | `plans/closure/application-boundary/004-status.md` | disposition `not measured`; no optimization authorized |
