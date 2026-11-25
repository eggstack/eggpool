# Provider Agents Milestone 003 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/provider-agents/003-agent-model-origin-and-health.md`

Source subsystem roadmap:

- `plans/subsystems/provider-agents-roadmap.md#7-milestones`

Repository baseline reviewed: `d6a06dd9` (Conditionally close the provider agents
enrollment milestone)

Implementation commits or pull requests:

- `1321e7c9` — Add agent model origin labels, tunnel availability projection,
  and offline routing exclusion

## 1. Executive finding

The M003 capability boundary is complete and verified. An agent-served model is
now discoverable through the tunnel, labelled by its origin, and deterministically
disambiguated when two agents expose the same model name; and an agent that is
not online is visible with a bounded reason and excluded from routing for its
own account alone. The upstream model ID remains the dispatch and policy
identity: the origin label and its numeric suffix are display-only and never
reach a provider, the model-policy grammar, or a routing key.

The milestone is **conditionally closed**, not closed, for named reasons
recorded in §10: the repository-wide dependency-audit gate is red on two
pre-existing `russh` advisories that M003 neither introduced nor can resolve;
the live ticket producer in `eggstack/eggpass#10` does not exist, so no user
can yet enroll an agent whose models this milestone would label; and the pinned
Eggress `1.0.12` reverse server does not observe an idle control-channel
disconnect, which bounds how promptly an offline agent is detected.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Origin identity is projection-only | `rust/src/catalog/cache.rs` `ModelOrigin` | pass | `(provider_id, account_name, label)`; the catalog stays keyed by the upstream `model_id` |
| The label is the agent's enrolled display name, else the account name | `rust/src/catalog/cache.rs` label resolution | pass | A configured label wins; the account name is the fallback |
| The collision suffix is deterministic | `rust/src/catalog/cache.rs` `project_model_origins` | pass | Sorted by the total, stable lexicographic key `(provider_id, account_name)`, documented in a comment |
| The suffix does not depend on insertion, discovery, or iteration order | `rust/src/catalog/cache.rs` unit test `agent_origin_labels_are_sorted_stable_and_display_only` | pass | The same configuration yields the same labels under a reversed insertion order |
| A single origin renders without a suffix | same unit test | pass | |
| No second model identity for the same upstream model | the unit test asserts the catalog still holds only the bare model ID | pass | |
| Agent discovery resolves the account key with no OAuth manager call | `rust/src/catalog/refresh.rs` Agent branch | pass | Uses the account's own credential and endpoint |
| Agent discovery requests the agent's models path | `rust/src/catalog/refresh.rs`; `rust/src/providers/transport.rs` join | pass | The target is relative to the account base path, so an enrolled account's effective request is `GET /v1/models` |
| A failed discovery keeps the previous entry | `rust/tests/catalog_refresh.rs` | pass | The failure records a bounded outcome and withdraws nothing |
| A failed discovery does not touch another account's support | same test | pass | Sibling-account isolation is asserted |
| The tunnel state projects `Online \| Starting \| Offline \| Failed` | `rust/src/agents/tunnel.rs` `AgentTunnelState` | pass | With a bounded, secret-free reason vocabulary |
| The state comes from the tunnel's own control state, with no probing | `rust/src/agents/tunnel.rs` | pass | The reverse server's own counters are read; nothing probes a sleeping machine |
| "Has connected at least once" is owned by the connection state | `rust/src/agents/tunnel.rs` authenticated-accept counter | pass | The counter rises only after a control connection authenticates, so EggPool's own readiness probe never counts |
| Routing excludes non-online agent accounts | `rust/src/routing/eligibility.rs` | pass | With `agent_starting`/`agent_offline`/`agent_failed` reasons |
| The exclusion does not change the owner-binding rules | `rust/src/routing/eligibility.rs` | pass | The owner gate is applied first; the availability signal only narrows that account |
| A static-key or OAuth account is unaffected | the same gate | pass | The signal is read only for `AccountAuthMode::Agent` |
| No per-request topology lock was introduced | `rust/src/routing/router.rs` | pass | The availability snapshot is published through an `ArcSwap` and read lock-free |
| The member model list groups by model, one row per origin | `rust/src/server/dashboard/admin.rs`, `render/admin.rs` | pass | The handler gathers the projection; the renderer formats it |
| The rendering stays pure | `rust/src/server/dashboard/render/admin.rs` | pass | No runtime state or I/O inside the renderer |
| Status and readiness expose the state and a bounded reason | `rust/src/operations/status.rs`, `rust/src/server/health.rs` | pass | `readyz` and `/api/status` still share one evaluator |
| No secret, credential, ticket, or request body in a label, status value, or diagnostic | `rust/src/catalog/cache.rs`, `rust/src/operations/status.rs` | pass | Bounded reason codes and fixed state values only |
| A ticket's enrollment credential cannot become the display label | `rust/src/agents/ticket.rs` `has_safe_display_label`, `rust/src/server/agents.rs` | pass | Refused at the ticket and rechecked at the publication boundary |
| Dispatch identity stays the bare upstream `model_id` | the origin unit test and the catalog assertions | pass | No label or suffix reaches a provider, the policy grammar, or a routing key |
| The model-policy grammar is unchanged | `rust/src/routing/eligibility.rs` | pass | Still allows or denies by model and provider; no account-level grammar was added |
| The OAuth and static-key paths are behaviourally unchanged | no OAuth branch was modified | pass | Direct OAuth custody stays in force until M004 |
| M002's ticket, enrollment, revocation, and publication behaviour is unchanged | `rust/src/agents/ticket.rs`, `rust/src/server/agents.rs` | pass | The credential-as-label refusal is a required tightening, not a behaviour change |
| No new dependency, no dependency version change, no waiver | `rust/Cargo.toml`, `Cargo.lock` | pass | The Eggfetch `0.4.0`, Eggress `1.0.12`, and EggServe `0.4.0` pins are unchanged |
| `--no-default-features` parity | the full suite in both feature configurations | pass | |

## 3. Production implementation evidence

Landed ownership:

- `rust/src/catalog/cache.rs` — `ModelOrigin`, `model_origins()`,
  `model_origins_for_accounts()`, and `project_model_origins()`, which
  assigns the collision suffixes by the documented total, stable lexicographic
  key. The origin is recorded alongside the existing per-account support data;
  the catalog's own keying is unchanged.
- `rust/src/catalog/refresh.rs` — the `Agent` discovery branch, which resolves
  the account credential and the account's own endpoint and fetches the models
  path relative to that base path.
- `rust/src/agents/tunnel.rs` — `AgentTunnelState` with its bounded reason
  vocabulary, `AgentTunnelAvailability`, `AgentTunnelSnapshot`,
  `AgentTunnelConnectionFacts`, and the per-account `state_for`/`status_for`
  projections. The reverse server's own metrics are installed on each listener,
  so the authenticated-accept counter is the connection-state owner for "this
  agent has connected at least once".
- `rust/src/routing/eligibility.rs` — the non-online agent exclusion and its
  reasons.
- `rust/src/routing/router.rs` — the `ArcSwap`-held availability snapshot, read
  without a lock on the selection path.
- `rust/src/operations/status.rs`, `rust/src/server/health.rs` — the bounded
  tunnel rows and the shared readiness evaluator.
- `rust/src/server/dashboard/admin.rs`, `render/admin.rs` — the grouped member
  model list, one row per origin.
- `rust/src/agents/ticket.rs`, `rust/src/server/agents.rs` — the refusal of a
  credential-bearing display label, at the ticket and at the publication
  boundary.
- `rust/src/catalog/mod.rs`, `rust/src/agents/mod.rs`, `rust/src/runtime_lifecycle/generation.rs` —
  the exports and the generation wiring.

Dependency change: none. The Eggfetch `0.4.0`, Eggress `1.0.12`, and EggServe
`0.4.0` pins are unchanged and no advisory waiver was added.

## 4. Verification executed

### Commands run

All commands below were run LOCALLY on 2026-11-24 in the authorized worktree.
No commit was pushed and no hosted CI run was executed for this change, so no
CI evidence is claimed.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
```

### Results

| Check | Result |
|---|---|
| `cargo fmt --check` | pass |
| strict Clippy, default features | pass, zero warnings |
| `cargo check --no-default-features` | pass |
| strict Clippy, `--no-default-features` | pass, zero warnings |
| full serial workspace suite, default features | 1395 passed, 0 failed, 2 ignored (98 result blocks) |
| full serial workspace suite, `--no-default-features` | 1396 passed, 0 failed, 2 ignored (98 result blocks) |
| `cargo build --locked --release` | pass |
| `cargo deny check` | **advisories FAILED**; bans ok, licenses ok, sources ok — see §10 |
| tooling suite | 267 passed, 1 skipped |
| `scripts/validate_release_docs.py` | pass |
| `git diff --check` | clean |

The `cargo deny` result is a genuine failure of the advisory gate and is not
reported as a pass. Its scope is established in §10.

### Independent review and the corrective pass

An independent verifier audited the uncommitted implementation before closure.
It returned one HIGH, one MEDIUM, and two LOW findings plus three test-coverage
gaps. All were fixed and re-verified by the matrix above:

- HIGH — the Agent discovery path was double-prefixed. The enrolled account's
  base URL ends in `/v1`, the Agent branch asked for `/v1/models`, and the
  transport joins base path with target, so the effective request was
  `/v1/v1/models`. The Agent target is now relative to the configured base
  path, so the effective request is the agent's `GET /v1/models`, and the
  discovery fixture now uses the enrollment-shaped base URL and rejects any
  other path.
- MEDIUM — "this agent has connected at least once" was latched at the sampling
  site, so a connection that was established and then consumed before the first
  read could be reported as still awaiting its first connection. The latch now
  comes from the reverse server's own authenticated-accept counter, which is
  incremented by the server itself and never by EggPool's readiness probe.
- LOW — a ticket's enrollment credential could pass the label syntax check and
  be published as the display label. The credential is now refused, and the
  publication boundary re-checks it.
- LOW — the suffix sort key was not documented. It is now named, with its
  totality and stability, in the comment that assigns the suffix numbers.
- TEST GAPS — the discovery fixture now uses the enrollment-shaped base URL
  and asserts the path strictly; the routing suite now proves an online agent
  IS selected and is excluded once its tunnel state leaves `Online` while the
  shared static-key account stays selectable; and the status and dashboard
  suites now assert the live projection and a credential-bearing label.

A parent-side review during the corrective pass established one further fact
about the pinned dependency, recorded in §10: the Eggress `1.0.12` reverse
server's active-control counter counts POOLED control connections and is
decremented only when one is handed to an external request, so the server does
not observe an idle control-channel disconnect. The regression test for the
MEDIUM finding was therefore written against the scenario that actually
reproduces it — a control that is established and then consumed before the
first projection read — and the documentation states the detection limit
plainly.

## 5. Invariant review

- **Dispatch identity unchanged.** The upstream `model_id` is what reaches the
  provider, the model-policy grammar, and the routing key. Origin labels and
  suffixes exist only in the projection, the member model page, and the
  operator surface.
- **Deterministic labelling.** The suffix is assigned by a total, stable
  lexicographic key, so the same configuration produces the same labels across
  reloads and restarts, independent of insertion, discovery, or iteration
  order.
- **No secret in the presentation layer.** No label, status value, reason code,
  or rendered cell carries a credential, ticket content, CA material, or
  request body; a credential-bearing label is refused outright.
- **Owner-only selection, no pooling.** The owner gate is evaluated before the
  availability signal, so the signal can only narrow an account the owner may
  already use. No other owner's account is ever selected or disabled.
- **Account-scoped exclusion.** Only an `AccountAuthMode::Agent` account is
  excluded by the availability signal; a static-key or OAuth account is
  entirely unaffected.
- **No probing.** The state is read from the tunnel's own control state; nothing
  probes a sleeping machine.
- **Reload authority unchanged.** `classify_transition` remains the only
  reload-versus-restart authority.
- **Performance boundaries unchanged.** The single SQLite gate, the streaming
  mpsc bridge, the `current_thread` Tokio runtime, and the routing selection
  lock are untouched. The availability snapshot is published through an
  `ArcSwap` and read without a lock.
- **Closed milestones not regressed.** M001's tunnel ownership, shutdown model,
  owner gate, and terminal agent 401 are unchanged, and M002's ticket decoder,
  enrollment registry, enrollment and revocation routes, and atomic publication
  path keep their behaviour.

## 6. Failure and recovery review

- **Failed discovery.** The previous catalog entry stays in place, a bounded
  failure outcome is recorded, and no sibling account's support is withdrawn.
- **A tunnel drop during a request.** The in-flight request fails at the
  transport and the account is not silently substituted.
- **A listener that stops.** The state becomes `failed` and the account is
  excluded from routing for itself alone.
- **A generation replacement.** The tunnel is released only when the last lease
  owner goes away, under the existing bounded shutdown.
- **Status and dashboard reads.** Both are lock-free projections of a snapshot
  and never block request admission.

## 7. Migration and compatibility review

- No storage migration and no schema change. The persisted model, quota, health,
  and binding tables are untouched.
- Origin labels are additive to the catalog projection and the rendered
  surfaces; a consumer that reads `model_id` or `display_name` is unaffected.
- The label and the suffix are not part of the request grammar, so no client
  change is required.
- `[oauth]`, `AccountAuthMode::OAuth`, the OAuth credential store, the OAuth
  routes, the `oauth-login` CLI command, and the ADR-0015 refresh path all
  remain fully in force. Direct OAuth custody is retired only in Provider
  Agents M004.
- Existing configurations load unchanged; the new configuration keys are
  additive.

## 8. Security review

- **Transport.** Unchanged: the control channel is TLS with operator-supplied
  material, and the external listener stays loopback-only.
- **Authentication.** Unchanged: the reverse server authenticates the agent
  with the per-agent transport credential.
- **Secret hygiene.** A ticket's enrollment credential can no longer be
  published as a display label, so the credential cannot reach the model
  surface, a status value, or a diagnostic.
- **Privilege bounds.** The availability signal is applied after the
  owner-binding check, so it never widens what an owner may use.
- **Bounded output.** The status and dashboard projections carry fixed state
  and reason vocabulary only; no endpoint, credential, or free-form text is
  serialized.

## 9. Documentation and operations

- `docs/providers.md` — how agent models appear, what the label and the
  suffix mean, that the suffix is display-only and deterministic, the
  credential-as-label refusal, the availability states, and the pinned
  server's detection limit.
- `architecture/deep-dive-catalog.md` — the Agent discovery branch, the
  account-scoped credential, and the failure-retention behaviour.
- `architecture/deep-dive-providers.md` — the availability projection, the
  routing exclusion, the counters it reads, and the detection limit.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| high | The live ticket producer in `eggstack/eggpass#10` does not exist. | An agent cannot yet be enrolled by a user, so the models this milestone labels and the offline states it projects have no user-facing route yet. The EggPool-side capability is complete and proven against a fixture producer. | Land the `eggpass eggpool` ticket producer with the agreed `epagent1` format, then re-run the enrollment journey against a live producer. |
| high | The repository-wide dependency-audit gate is red: `cargo deny check` reports advisories FAILED for RUSTSEC-2026-0492 and RUSTSEC-2026-0495 against `russh 0.62.7`, reached through `eggress-transport-ssh 1.0.12` → `eggress-outbound 1.0.12`, which the default `ssh` feature enables. | The `dependency-audit` workflow gates on `rust/Cargo.toml`/`rust/Cargo.lock`/`deny.toml`, so it will fail for this branch. The condition is a pre-existing SSH-transport advisory, not an Agent-path defect. | Owned by Security hardening M008. Resolution requires an Eggress release permitting `russh >= 0.65`, or an accepted ADR for a vendored patched `0.62.x`, or an accepted ADR removing the default `ssh` feature. No advisory waiver is acceptable and none was added. |
| medium | The pinned Eggress `1.0.12` reverse server does not observe an idle control-channel disconnect. Its active-control counter counts POOLED control connections and is decremented only when one is handed to an external request. | An agent whose control channel drops while idle is reported as online until the next request consumes the pooled control, so the routing exclusion follows the first failed request rather than the disconnect itself. EggPool reports the state the pinned server actually exposes, and does not probe. | A future Eggress release that observes control-channel closure would allow a prompt offline state. Until then the behaviour is documented in `docs/providers.md` and `architecture/deep-dive-providers.md`. |
| low | The mTLS branch of the reverse server's client-certificate handling is still not proven end-to-end, carried forward from M001's and M002's closure records. | The branch is configured and validated but not exercised by an automated test. | Prove the mTLS branch when a per-agent client certificate is issued. |

Scope evidence for the high dependency-audit finding: the identical
`cargo deny check` run against the pre-M001 commit `3b90434b` in a detached
worktree fails with the same two advisories, and M003 adds no dependency at all,
so the finding is pre-existing, repo-wide, and neither introduced nor worsened
by M003.

## 11. Roadmap disposition

Conditionally closed with named outstanding conditions: the live ticket producer
in `eggstack/eggpass#10` must exist, and the dependency-audit gate must report
`advisories ok` again, before this branch lands on `main`. Neither is an M003
interface defect, so M004 is promoted to `ready` with the promotion recorded.

M003's own capability boundary is complete: no partially implemented origin or
availability path, no unrun required test, no unimplemented security or
migration requirement, and intact `--no-default-features` parity.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md` — the Provider agents roadmap row records M003 as
  conditionally closed and M004 as ready; the M003 row moves from the
  dependency-ready table to the recently-closed table; a dated audit note
  records the closure, the conditions, and the promotion reasoning.
- `plans/subsystems/provider-agents-roadmap.md` — the milestone status table
  records M003 as conditionally closed and M004 as ready, and the dependency
  edges record the satisfied M003 prerequisites.
- `plans/implementation/provider-agents/003-agent-model-origin-and-health.md` —
  header status becomes `implemented`.
- `plans/implementation/provider-agents/004-retire-direct-oauth-custody.md` —
  header status becomes `ready`.
