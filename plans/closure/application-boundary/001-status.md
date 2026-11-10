# Application Boundary Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/application-boundary/001-withdraw-mcp-gateway-and-hosted-execution.md`

Source subsystem roadmap:

- `plans/subsystems/application-boundary-roadmap.md#m001--withdraw-mcp-gateway-and-hosted-execution`

Repository baseline reviewed: `e049025274a397d7799c3ae21ed106947c2a1572`

Implementation commits:

- Uncommitted working tree on that baseline. No commit has been created.

## 1. Executive finding

M001 is complete. EggPool no longer mounts `POST /api/tools/v1/mcp` or
`POST /api/agent/v1/run`, no longer issues Eggsearch discovery or invocation
scopes, and no longer exposes `eggpool tools` or `eggpool users tool-policy`.
A true `[tools].enabled`, `[tools].gateway_enabled`, or
`[tools].hosted_execution_enabled` fails config validation before listen.
Absent or all-false `[tools]` still validates. Chat Completions, Responses,
Messages, compact, and embeddings stay. Client-executed tool calls stay in
`rust/src/wire/`.

Tool-integration M003 and M004 evidence conditions are vacated rather than
collected. No migration was added. v65 grant tables stay.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Absent and all-false `[tools]` validate | `false_tool_switches_load_and_each_true_switch_is_rejected` | pass | Inside both serial suites. |
| Each true switch fails and names the key | Same test, plus `enabled_tool_switch_names_the_key_and_hides_the_url` | pass | Message contains `tools.<key>` and `tool execution belongs to the client`. |
| Error text hides the backend URL and secret | Those two tests plant `secret-value` / `super-secret-token` and `10.1.2.3` | pass | Neither secret nor URL appears. |
| Both routes are unmounted | `withdrawn_tool_routes_are_not_mounted` | pass | Bearer `withdrawn-route-key` on a freshly migrated database gets 404 for both paths. |
| CLI tool commands are gone | `withdrawn_tool_commands_are_absent_and_audit_export_parses`; `cli_contract` matrix is 74 commands | pass | `tools` and `users tool-policy` rows removed. `check-config` gained `--local` for M003. |
| Issued scopes omit tool execution | `issued_token_omits_tool_execution_scopes`; `issue_api_token` writes `inference,discovery,integrations` | pass | Historical credential rows are not rewritten. |
| Embeddings still admit | `embeddings_api`: 3 passed, 1 ignored (live Ollama) | pass | Inside both serial suites. The ignored live test was already ignored. |
| Client-executed tool calls remain | `eggpool-wire` `tool_calls` tests in both serial suites | pass | No new Chat Completions round trip was added. Nothing in this tree executes a tool call. |
| No new migration | `git status --short -- rust/assets/db/migrations` empty | pass | `0065_tool_grants.sql` was not edited. |
| Docs do not instruct enabling the gateway | `docs/tool-gateway.md`, `docs/hosted-tool-orchestration.md`, API, CLI, configuration, deployment, firewall, agent-configuration, releasing | pass | Remaining route mentions say the paths are not routed. |
| Tool-integration SBC conditions | This record | pass | Vacated, not collected. |

## 3. Production implementation evidence

Deleted: `rust/src/server/tools.rs`, `rust/src/server/hosted.rs`,
`rust/src/agent_orchestration.rs`, `rust/src/operations/tools.rs`,
`rust/src/tool_policy.rs`. Route registration for the two paths is gone.
`ServerState` no longer carries a tool gateway. Personal-credential and
inference path lists no longer include those routes.

`validate_tool_backend` rejects only the three true switches. A false switch
with a bad URL or unused timeout still loads, because those fields are unused.
`("tools", ReloadDisposition::RestartRequired)` is unchanged.
`tool_gateway_config_requires_restart` passed inside the default serial suite.
`classify_transition` does not call `validate`; `serve` loads through
`from_toml`, so startup fails closed.

`on_prem_model_policy` no longer calls the removed tool-policy denial writer.
v65 tables `profile_tool_rules`, `user_tool_overrides`, and
`instance_tool_denials` remain, including their revision triggers, and have
no production reader.

## 4. Verification executed

### Commands run

Local, offline, not CI. Focused targets below were not invoked as separate
`cargo test --test` commands. Each passed inside both full serial suites.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --offline -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --offline -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline -- --test-threads=1
```

### Results

- `cargo fmt --check`: exit 0.
- `cargo clippy` with `-D warnings`: exit 0. Finished the `dev` profile in 3.28s on the tree that includes the CLI contract count change.
- Default serial suite: exit 0, elapsed 346s. Includes `mcp_gateway` 2, `tool_grants` 2, `tool_backend_config` 2, `embeddings_api` 3 passed / 1 ignored, `cli_contract` 7, `gateway_audit` 2.
- No-default serial suite: exit 0, elapsed 415s. Same targets passed.

## 5. Invariant review

Inference admission stays in `rust/src/coordinator/endpoints.rs`. This change
does not add a second admission path. Embeddings admission is unchanged.
Historical migrations v1–v65 are unchanged. `classify_transition` remains the
only reload authority. The validation error does not echo the backend URL or
a secret. Default `Config::default().validate()` succeeds.

## 6. Failure and recovery review

An enabled switch fails in config validation, before listen. Rehash cannot
publish a generation that serves the routes, because validation rejects the
switch and the routes are not registered. In-flight inference follows the
existing cancellation and generation rules. There is no tool backend to
cancel. Token issue stays on the existing identity transaction. No new lock.

## 7. Migration and compatibility review

No new migration. A restored database that already contains v65 grant rows
still migrates forward and those rows stay unread. Chat, Responses, Messages,
compact, and embeddings routes are unchanged. Clients of the MCP and hosted
routes receive 404. A stored token that only had tool scopes can no longer
call a removed route. A token that also has inference still calls inference.
Historical credential rows are not rewritten.

The operational replacement is the client's own tool loop, or Eggsearch
directly. Model calls use a personal EggPool token with the inference scope.

## 8. Security review

The withdrawn routes are not in `personal_credential_allowed` or
`is_inference_path`. The validation message does not include the configured
URL or credential. No prompt, tool argument, or secret was added to logs or
to a new table.

## 9. Documentation and operations

Updated: `docs/api-reference.md`, `docs/cli-reference.md`,
`docs/configuration.md`, `docs/agent-configuration.md`, `docs/deployment.md`,
`docs/firewall.md`, `docs/stateless-responses.md`, `docs/tool-gateway.md`,
`docs/hosted-tool-orchestration.md`, `docs/releasing.md`,
`architecture/overview.md`, `architecture/deep-dive-on-prem-user-access.md`,
`AGENTS.md`. `architecture/README.md` did not list the deleted modules.
`docs/retrieval-interoperability.md` is owned by R002.

The CLI contract fixture dropped `tools`, `tools doctor`, and the five
`users tool-policy` paths, and dropped `--tool-discovery` and
`--tool-invocation` from `users token`. The frozen count is 74.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | No new Chat Completions function-tool HTTP round trip was added | The wire codecs still reconstruct tool calls, and no executor remains | None. Existing `tool_calls` tests passed in both suites. |
| low | Working tree is uncommitted | Closure SHAs are not yet a commit | Commit when requested. |

## 11. Roadmap disposition

Milestone closed. M002, M003, and R002 proceed in the same working tree.
M004 does not depend on this milestone.

## 12. Registry updates

Application-boundary M001 leaves the dependency-ready table. The roadmap
milestone row points at this record. Tool-integration M003 and M004 stay
conditionally closed as history, and their open evidence conditions stay
vacated. Nothing else was waiting on this withdrawal.
