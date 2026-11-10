# Application Boundary Milestone 001 — Withdraw MCP gateway and hosted execution

Status: implemented

Repository baseline: `e049025274a397d7799c3ae21ed106947c2a1572`

Source roadmap:

- `plans/subsystems/application-boundary-roadmap.md` M001

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#4-protocol-and-compatibility`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`

Applicable ADRs:

- `plans/adrs/ADR-0009-downstream-application-boundary.md` (accepted)
- `plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md` (superseded)

Primary class: capability

## 1. Objective

Remove EggPool's MCP gateway and finite hosted tool runner. An enabled
`[tools]` execution switch fails `check-config` and process startup
with a typed error. Chat Completions, Responses, Messages, compact, and
embeddings keep their present behavior, including returning tool calls
to the client.

## 2. Why this milestone is ready

ADR-0009 is accepted. No other hard dependency. Retrieval R002 owns the
PrivateGPT document narrative and is not a blocker.

## 3. Current implementation evidence

- `rust/src/server/tools.rs` serves `POST /api/tools/v1/mcp`.
- `rust/src/server/hosted.rs` and `rust/src/agent_orchestration.rs`
  serve `POST /api/agent/v1/run`.
- `rust/src/tool_policy.rs` and `rust/src/operations/tools.rs` own
  grants and `eggpool tools doctor`.
- `rust/src/identity.rs` can attach discovery and invocation scopes and
  has `RequestAttribution::hosted_tool_child`.
- Migration `rust/assets/db/migrations/0065_tool_grants.sql` created
  `profile_tool_rules`, `user_tool_overrides`, and
  `instance_tool_denials`, with revision triggers.
- Tests: `rust/tests/mcp_gateway.rs`, `rust/tests/tool_grants.rs`,
  `rust/tests/tool_backend_config.rs`.
- Docs: `docs/tool-gateway.md`, `docs/hosted-tool-orchestration.md`,
  and the tool sections of `docs/api-reference.md`,
  `docs/cli-reference.md`, `docs/configuration.md`, and
  `docs/agent-configuration.md`.
- Client-executed tool calls live in `rust/src/wire/` and stay.

## 4. Invariants that must not regress

- One inference admission path in `rust/src/coordinator/endpoints.rs`.
- Tool-call transcoding still returns calls to the client and does not
  execute them.
- `POST /v1/embeddings` admission is unchanged.
- Historical migrations v1–v65 are not edited.
- `classify_transition` is the only reload authority.
- Credentials, prompts, raw bodies, and tool payloads stay out of logs
  and new persistence.
- Default solo startup still succeeds when `[tools]` is absent.

## 5. Scope

### In scope

- Remove the two routes, the hosted controller, the Eggsearch backend
  client, grant administration, and the tools doctor.
- Fail closed when `[tools].enabled`, `[tools].gateway_enabled`, or
  `[tools].hosted_execution_enabled` is true.
- Accept a config that omits `[tools]` or sets those switches false.
- Stop issuing discovery and invocation scopes.
- Update operator docs and architecture notes that describe the routes
  as current behavior.
- Retarget or remove tests that exist only to prove the gateway.

### Explicitly out of scope

- Dropping the v65 tables or editing `0065_tool_grants.sql`.
- Changing EggUI, CodeGG, or Eggsearch.
- Retrieval documentation owned by retrieval R002.
- Audit export (M002), the local metadata profile (M003), and the
  workload record (M004).
- A replacement in-proxy tool runner.

## 6. Required production changes

Remove route registration from `rust/src/server/mod.rs` and delete the
gateway and hosted handlers once nothing references them. Remove
`eggpool tools` and `eggpool users tool-policy` command paths from
`rust/src/cli.rs` and `rust/src/runtime.rs`. Stop writers in
`rust/src/tool_policy.rs` from being reachable; leave the module only
if a migration or read-only restore path still needs the type, and
delete it when no reader remains. `hosted_tool_child` goes away with
its callers.

Config parsing keeps enough of `[tools]` to detect the three switches.
True is `BootstrapError` or the existing config-validation error with a
stable message that the execution surface is withdrawn and that tool
execution belongs to the client. False or absent is success, with no
listener, task, or backend connection. Reload classification for any
remaining `[tools]` keys stays in `rust/src/config_reload_policy.rs`.
An attempt to turn a switch on through rehash fails closed and does
not publish a generation that serves the routes.

Personal-token issue no longer offers discovery or invocation scopes.
A stored token that has only those scopes can no longer call a removed
route; a token that also has inference still calls inference. Do not
rewrite historical credential rows.

## 7. Ordered work packages

### Work package A — Config rejection

Intent: make an enabled execution switch a check-time and startup-time
error before any route work is relied on.

Required changes: validation in the config loader used by `check-config`
and `serve`. Cover absent, all-false, and each switch true.

Acceptance evidence: unit tests for the three true switches and for
absent/`false`. The error text does not echo secrets or backend URLs.

### Work package B — Remove routes and runners

Intent: the process has no MCP gateway and no hosted runner.

Required changes: delete route registration and the now-unused server,
orchestration, and backend client code. Keep `rust/src/wire/` tool-call
codecs.

Acceptance evidence: router tests show the two paths are unmounted.
A Chat Completions fixture with a function tool still returns the tool
call to the client. Embeddings tests still pass.

### Work package C — Operator surface

Intent: CLI and token issue no longer administer Eggsearch grants.

Required changes: remove tools and tool-policy commands. Stop issuing
the two tool scopes. Update CLI help.

Acceptance evidence: `cli_contract` or the focused CLI test shows the
commands are gone. Issuing a token yields the inference scope set
without discovery or invocation.

### Work package D — Docs and dead tests

Intent: docs describe the withdrawn surface as gone, and tests do not
boot a gateway.

Required changes: API, CLI, configuration, agent-configuration,
deployment, and architecture pages that currently document the routes.
Delete or rewrite `mcp_gateway`, `tool_grants`, and
`tool_backend_config` so they guard absence and config rejection.
Leave retrieval-interoperability.md to R002 except to delete claims
that this milestone's docs still tell operators to register an adapter
on the gateway.

Acceptance evidence: `rg` over `docs/` and `architecture/` finds no
current-tense instruction to call the removed routes. Focused tests
pass.

## 8. Failure, cancellation, restart, contention semantics

Startup with an enabled switch exits before listen. A running process
from an older binary is unchanged until restart; rehash must not turn
the routes on. In-flight requests on inference routes follow the
existing cancellation and generation rules. There is no tool-call
backend to cancel. Concurrent token issue uses the existing identity
transaction. No new lock.

## 9. Compatibility and migration

No new migration. v65 tables may remain populated and unread. Backup
restore of a database that contains them still migrates forward.
Clients of Chat, Responses, Messages, compact, and embeddings see the
same routes. Clients of the MCP and hosted routes get connection
failure or 404 from the unmounted path. Document the operational
replacement: point the client at Eggsearch or at the application's own
tool loop, and use a personal token with the inference scope against
EggPool for model calls.

## 10. Required tests

- Config matrix: absent, all false, each switch true.
- Unmounted routes.
- One Chat Completions function-tool round trip that EggPool does not
  execute, using the existing wire fixtures.
- Token issue omits discovery and invocation scopes.
- Embeddings regression via `rust/tests/embeddings_api.rs`.
- No test may require a live Eggsearch process.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test mcp_gateway -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test tool_grants -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test tool_backend_config -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test embeddings_api -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

If a focused target is deleted, the closure record says so and names
the replacement test. Do not claim a command that was not run.

## 12. Documentation updates

- `docs/api-reference.md`
- `docs/cli-reference.md`
- `docs/configuration.md`
- `docs/agent-configuration.md`
- `docs/deployment.md`
- `docs/tool-gateway.md` and `docs/hosted-tool-orchestration.md`
  (remove or replace with a short withdrawal note and a pointer to
  client-executed tools)
- `architecture/README.md` and
  `architecture/deep-dive-on-prem-user-access.md` where they list
  `server/tools.rs`, `server/hosted.rs`, and `tool_policy.rs` as live
  owners
- `AGENTS.md` only if it still names the gateway as a live module

Do not rewrite `docs/retrieval-interoperability.md` beyond removing a
now-false claim that the gateway can host a retrieval adapter. R002
owns that page.

## 13. Acceptance criteria

- `POST /api/tools/v1/mcp` and `POST /api/agent/v1/run` are not routed.
- `eggpool check-config` and startup fail on any true execution switch
  and succeed when the section is absent or all-false.
- A function tool on Chat Completions is returned to the caller.
- Embeddings still admit an authorized request.
- No new migration file.
- Docs no longer tell an operator to enable the gateway.

## 14. Stop conditions

Stop if withdrawal requires editing v1–v65, dropping grant tables, or
changing inference admission. Stop if a caller inside this repository
other than the tool surface executes tools. Stop if config rejection
cannot be expressed through the existing error type without a new
public status mapping whose owner is unclear. Do not start M002's
export in this patch.

## 15. Closure evidence required

Commits, the config matrix results, proof the routes are unmounted,
the client-executed tool-call test, embeddings result, confirmation
that no migration was added, doc diff summary, and the serial default
and no-default commands with outcomes. State that M003/M004 tool
SBC conditions are vacated rather than collected.

## 16. Handoff notes

Serial tests only. Do not add an Eggsearch dev-dependency. Preserve
unrelated working-tree changes. The v65 tables and their triggers stay
even if they look unused.
