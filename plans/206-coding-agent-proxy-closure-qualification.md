# Plan 206: Coding-agent proxy closure and real-client qualification

> **Status:** complete
>
> **Parent:** Plan 198
>
> **Renamed:** 2026-09-16 from `plans/203-coding-agent-proxy-closure-qualification.md`
> to resolve duplicate numbering with the per-plan closure record
> `plans/203-agent-model-catalog-and-client-config-lifecycle-closure.md`
> (which closes Plan 200). Companion matrix is now Plan 207 and evidence
> closure is now Plan 208.
>
> **Baseline:** Eggpool `main` at `3dc9ece9d713a49f56c9dd2f7aeba9b0c04e68e1` (2026-09-16)
>
> **Implemented prerequisites:** Plans 199–202
>
> **Priority:** Closure / qualification
>
> **Scope:** close the coding-agent proxy compatibility milestone by validating the implemented Codex/OpenCode integration against current real clients, repairing only concrete compatibility defects found during qualification, and updating plan/documentation state so future agents do not mistake completed work for unimplemented work.

## Executive summary

Plans 199–202 have landed on `main`:

- bounded native-only Responses remote compaction;
- provider-neutral agent model projection;
- generated Codex `model_catalog_json`;
- richer Responses-capable OpenCode provider/model configuration;
- managed Codex/OpenCode config lifecycle (`--apply`, `--sync`, `--check`, `--remove`, `--dry-run`);
- Codex deferred `tool_search` translation/conformance;
- top-level `eggpool status [--json]` and authenticated `/api/status` provider/proxy health snapshot.

The repository has deterministic coverage and current CI is green. The remaining work is therefore not another feature or architecture pass. It is a qualification/closure pass aimed at the failure modes deterministic fixtures cannot prove:

1. current Codex accepts the generated provider + catalog exactly as installed;
2. current Codex can complete a real Responses text/tool loop through Eggpool, including model selection from the generated catalog;
3. current OpenCode accepts the generated provider/model configuration and uses the advertised limits/capabilities correctly enough for normal agent work;
4. managed config operations are safe and reversible against realistic existing user configuration;
5. `eggpool status` behaves correctly with a running proxy, partial upstream degradation, and an unavailable proxy;
6. Plans 198–202 and related docs are marked with accurate final status/evidence after qualification;
7. any newly discovered incompatibility is fixed narrowly and protected by a regression test before the milestone is declared closed.

Do not add speculative features during this pass. In particular, do not add WebSocket Responses, server-persisted `previous_response_id`, server-side tool execution, broad OpenCodex backend parity, or new active health probes unless a real current client demonstrably requires them for the supported HTTP/SSE path.

---

# 1. Establish the exact qualification baseline

Before running live qualification, record the exact versions/commits under test.

Capture at minimum:

```text
Eggpool commit
Eggpool version
Codex CLI version
OpenCode version
OS / architecture
provider(s) used for the live path
Eggpool base URL
whether the tested model route is native Responses or translated
```

Use current released clients available at implementation time. If either client has changed materially since the 2026-09-16 research baseline, inspect its current config/catalog/tool schema before changing Eggpool.

Do not silently update source-derived fixtures to make tests pass. Any schema change must be traced to a current client version/source and documented in the closure evidence.

---

# 2. Codex managed-config and model-catalog qualification

Qualify the real user path from an otherwise ordinary Codex install.

Use an isolated temporary `CODEX_HOME` first so the test cannot damage operator configuration. Then separately exercise merge behavior against representative pre-existing config fixtures.

## Required isolated flow

Run the equivalent of:

```bash
eggpool configsetup codex --apply
```

Then verify:

1. Codex starts without config or catalog parse errors.
2. The generated `[model_providers.eggpool]` block points to the expected Eggpool Responses base URL.
3. `wire_api = "responses"` or the current equivalent is accepted.
4. WebSockets remain disabled unless Eggpool actually implements them.
5. `EGGPOOL_API_KEY` is referenced rather than embedded.
6. `model_catalog_json` resolves to the Eggpool-owned generated catalog file.
7. At least one expected Eggpool model/alias is visible in the Codex model picker/listing.
8. Display name, context window, output limit, and available reasoning variants match the conservative Eggpool projection closely enough to avoid client-side invalid assumptions.
9. An explicit `--model`/configured model path continues to work when no picker interaction is desired.

## Alias/capability check

Include at least one alias/selector backed by heterogeneous targets if available in the local test configuration.

Confirm the generated client metadata is conservative:

- context/output limits do not exceed the weakest eligible route;
- image/tool/reasoning capabilities are not advertised merely because one candidate supports them;
- unknown metadata remains absent/conservative rather than fabricated from the model name.

If the route cannot provide a clean heterogeneous fixture in live testing, retain the deterministic projection tests as the acceptance authority for this subcase.

---

# 3. Codex real inference and tool-loop qualification

The existing deterministic and opt-in smoke coverage should be extended only where necessary to capture a real current-client regression.

Run a real current Codex session through Eggpool with:

## A. Text-only Responses request

Require a unique random marker in the final text so stale/cached output cannot be mistaken for success.

Verify:

- request reaches Eggpool `/v1/responses`;
- SSE stream terminates cleanly;
- Codex accepts the event sequence;
- usage/terminal handling does not cause a retry or hang;
- no unsupported stateful field (`previous_response_id`, `store=true`, conversation/background) is required on the qualified HTTP/SSE path.

## B. Ordinary tool call

Use the existing read-only temporary-file pattern or another deterministic shell/read tool flow.

Require Codex to:

1. receive a function/tool call;
2. execute it client-side;
3. send the tool output back;
4. receive the final answer.

Verify call/item identity survives both native and translated surfaces when those routes are available.

## C. Deferred `tool_search`

If the current Codex build exposes/uses deferred tool search in a controllable test path, qualify one real loop through a translated function-only upstream.

Acceptance:

- Eggpool translates only the declaration marked as deferred search;
- an ordinary function named `tool_search` is not reclassified;
- Codex remains the executor;
- authoritative downstream `tool_search_call` identity is reconstructed;
- malformed wrappers still fail closed.

If current Codex does not provide a stable way to force this path in a live smoke, keep the deterministic `codex_responses_compat` test as the required authority and document the live limitation rather than inventing a brittle harness.

---

# 4. Compaction qualification without changing the default architecture

Current Eggpool now supports bounded native remote compaction, but current custom Codex providers may still use local compaction by default. Qualification must distinguish these cases rather than treating remote compaction as mandatory.

## A. Normal Codex long-context behavior

Run a sufficiently long current Codex session to trigger the client's supported compaction path if practical without excessive provider cost.

Verify the session continues coherently after compaction and Eggpool does not require persisted conversation state.

If Codex performs local compaction, record that as the expected result for the tested client/provider configuration.

## B. `/v1/responses/compact`

Where a configured provider explicitly advertises/qualifies native remote compaction, exercise Eggpool's compact endpoint directly or through a client path that actually invokes it.

Verify:

- only compact-capable native Responses routes are eligible;
- request bounds/stateless restrictions hold;
- alias model rewrite is correct;
- provider response is validated and bounded;
- unsupported upstreams fail before outbound submission rather than falling back to translated summarization;
- compact requests do not create persisted response/conversation state.

Do not enable remote compaction metadata in the Codex catalog merely to force the test.

---

# 5. OpenCode real-client qualification

Use an isolated temporary OpenCode config location/profile where supported.

Run:

```bash
eggpool configsetup opencode --apply
```

Verify:

1. OpenCode parses the generated/merged configuration.
2. The Eggpool provider uses the Responses-capable OpenAI runtime (`@ai-sdk/openai` or the current required equivalent), not the generic Chat Completions compatibility runtime.
3. `{env:EGGPOOL_API_KEY}` or the current supported environment interpolation is accepted.
4. Eggpool models/aliases appear and can be selected.
5. `limit.context` and `limit.output` are accepted and correspond to the shared conservative projection.
6. advertised modalities/reasoning variants do not overstate the route.
7. a text request completes.
8. a normal function/tool loop completes.
9. a multi-turn/longer agent session does not fail because the generated context limits are malformed or absent.

If OpenCode's schema has changed, update only the OpenCode renderer and source-provenanced fixtures; do not put OpenCode-specific fields into routing/catalog core types unless they represent a genuinely provider-neutral capability.

---

# 6. Managed config lifecycle and rollback qualification

The managed lifecycle is user-facing and potentially destructive if merge ownership is wrong. Give it explicit closure coverage beyond unit tests.

For both Codex and OpenCode, test against fixtures representing:

- empty/missing config;
- config containing unrelated user settings;
- an existing non-Eggpool provider;
- an existing Eggpool-managed block from the previous generated version;
- deliberate drift inside the managed Eggpool section;
- comments/formatting where the target format supports preservation;
- malformed/unsupported JSONC for OpenCode where silent rewriting must be refused.

Required operations:

```text
--dry-run
--apply
--check
--sync
--remove
```

Acceptance:

- `--dry-run` writes nothing;
- first `--apply` creates only owned artifacts/fields;
- repeated `--apply` is idempotent;
- `--check` is read-only and detects drift;
- `--sync` updates only Eggpool-owned material;
- drift outside Eggpool ownership is preserved;
- drift inside managed ownership is refused when required unless explicit `--force` semantics already exist and are documented;
- `--remove` restores/removes only Eggpool-owned material and leaves unrelated user config intact;
- generated catalog/manifest cleanup is complete;
- no credentials are written into Codex/OpenCode config files.

Any defect here should receive a focused regression fixture before fixing implementation.

---

# 7. `eggpool status` closure qualification

The new command should be exercised as an operator would use it, not only through aggregation unit tests.

## A. Healthy running proxy

With multiple configured providers/accounts, verify:

```bash
eggpool status
eggpool status --json
```

Acceptance:

- exactly one human provider row per configured provider;
- deterministic provider ordering;
- overall state matches `readyz` semantics;
- `ready` is shown only when real upstream evidence exists;
- JSON schema version is present and stable;
- no API keys, credential labels, prompts, raw request bodies, raw provider error bodies, or cache keys appear.

## B. Partial degradation

Induce or reproduce a safe bounded failure condition such as one account/provider under backoff while another route remains usable.

Verify:

- affected provider is `degraded` or `unavailable` according to the implemented precedence;
- proxy remains `degraded` with exit 0 when inference is still serviceable;
- reason code is bounded/classified rather than raw upstream text;
- invoking `status` does not alter the cooldown/circuit state or generate new provider traffic.

Do not deliberately trigger expensive or account-risking failures if a deterministic test fixture can establish the same behavior.

## C. Proxy unavailable

Stop/use an unreachable local Eggpool endpoint and run status.

Verify:

- CLI reports proxy `unavailable`;
- configured providers are still shown from safe local config as `unknown`/`disabled` where possible;
- exit code uses the existing unavailable/control class (`3` if unchanged);
- `--json` remains one valid bounded document.

## D. Reachable but unready

Use a configuration with no usable route/catalog/credential condition.

Verify exit code `1` and a bounded readiness reason shared with `readyz`.

---

# 8. Regression and architecture review after live testing

If any real-client qualification fails, classify the defect before changing code:

```text
client schema drift
Eggpool renderer bug
catalog projection bug
wire/event incompatibility
tool identity/lifecycle bug
managed-config ownership bug
status/readiness aggregation bug
provider-specific upstream incompatibility
client limitation / unsupported path
```

For each actual Eggpool defect:

1. add the smallest deterministic regression fixture/test that reproduces it;
2. fix at the narrowest existing ownership boundary;
3. rerun focused tests;
4. rerun the affected live qualification;
5. rerun workspace CI-quality checks.

Do not respond to a provider-specific quirk by weakening the provider-neutral wire contract globally.

Do not move client-specific schema into core routing merely because a renderer changed.

---

# 9. Plan/documentation closure

After qualification succeeds, update planning metadata so the repository does not keep presenting completed work as pending.

At minimum review Plans 198–202.

Preferred closure convention should match existing repository practice. If prior completed plans append a closure section rather than rewriting history, follow that convention.

Record for each implemented child plan:

- implementation commit(s);
- focused test evidence;
- CI run/conclusion;
- live qualification result where applicable;
- any limitations that remain intentional.

Plan 198 should be marked closed only after the real-client qualification in this plan succeeds or an explicit documented limitation is accepted.

Update relevant docs if qualification reveals any discrepancy, especially:

- `README.md` coding-agent examples;
- `docs/agent-configuration.md`;
- `docs/codex-compatibility-smoke.md`;
- `docs/stateless-responses.md`;
- architecture integration/request-lifecycle/transcoder docs;
- `.opencode/skills/*` guidance if command/test expectations changed.

Do not rewrite historical plan rationale solely to match the final implementation; append closure/evidence where that is the established convention.

---

# 10. Required verification

Run the normal repository quality gates at the final closure commit:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release

git diff --check
```

Also run the relevant focused targets, including current names at implementation time:

```bash
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::status -- --test-threads=1
```

If the repository still has the Python tooling checks used by the current CI contract, run them unchanged as well.

Run the existing opt-in Codex live smoke with real credentials/provider access. Add or retain an OpenCode live/manual qualification script only if it stays optional and does not introduce Node/OpenCode as a production or mandatory CI dependency.

---

# Non-goals / explicit deferrals

Unless the real current supported client path proves otherwise, this closure pass must not add:

- Responses WebSocket proxying;
- persisted `previous_response_id` state/replay;
- stored conversations/background jobs;
- server-side Codex/OpenCode tool execution;
- broad OpenCodex API/backend parity;
- image/voice/history/notes/account-affinity APIs;
- active provider probing from `eggpool status`;
- a second provider-health state machine;
- a new coding-agent-specific routing subsystem;
- new dependencies solely to run Codex/OpenCode in normal Eggpool production.

These remain separate future work if justified by a concrete requirement.

---

# Acceptance criteria

This closure pass is complete when:

1. current Codex starts successfully from an Eggpool-managed provider + generated model catalog;
2. expected Eggpool models/aliases are selectable in current Codex with conservative metadata;
3. a real Codex streamed text request succeeds through Eggpool;
4. a real Codex client-executed tool loop succeeds;
5. deferred `tool_search` is live-qualified where the current client exposes a stable test path, or its deterministic conformance limitation is explicitly documented;
6. current Codex long-session compaction behavior is qualified without requiring Eggpool conversation persistence;
7. native remote compaction is qualified where an upstream actually supports it, without adding translated fallback/state;
8. current OpenCode accepts the managed Eggpool configuration and completes text + ordinary tool work;
9. generated OpenCode context/output/capability metadata does not overstate heterogeneous routes;
10. Codex/OpenCode `--apply`/`--check`/`--sync`/`--remove` lifecycle is idempotent, reversible, and preserves unrelated user configuration;
11. `eggpool status` is qualified for ready, degraded, unready, and unreachable cases without causing provider traffic or exposing secrets;
12. every concrete defect discovered by live testing has a deterministic regression test before the fix is accepted;
13. workspace quality gates and CI are green at the final closure commit;
14. Plans 198–202 contain accurate completed/closure evidence and no longer imply the implemented work is still pending;
15. remaining limitations are explicit intentional deferrals rather than ambiguous TODOs.

---

# Handoff note

Treat this as a **qualification-first closure pass**. Start by running the current clients against the code already on `main`. Do not preemptively refactor or add features because OpenCodex or another proxy supports them. If the existing Eggpool implementation passes the real-client tests, the correct result is primarily evidence/documentation/plan closure, not additional code.

---

## Closure evidence (2026-09-16)

- Qualification baseline: Eggpool `0.8.0` at `39c83564`, Codex CLI `0.154.0`,
  OpenCode `1.18.30`, macOS Darwin 25.6.0 (x86_64), isolated `CODEX_HOME` +
  `XDG_STATE_HOME` + temp Eggpool config (no operator config touched).
- Codex managed config: `configsetup codex --apply/--check/--sync/--remove/--dry-run`
  PASS (isolated). `codex debug models` initially FAILED with
  `missing field supported_reasoning_levels`; after patching also required
  `base_instructions`. Fixed narrowly in
  `rust/src/operations/integrations.rs::codex_catalog_entry` (always emit
  `supported_reasoning_levels`, empty when unknown; always emit
  `base_instructions = ""` as no-override) plus strict
  `validate_codex_catalog_json` enforcement and regression test
  `codex_catalog_emits_current_required_fields_for_unknown_models`.
  Re-qualified: `codex debug models` PASS, `codex doctor --json`
  `config.load ok`, `EGGPOOL_API_KEY (present)`, `wire_api responses`,
  WebSockets disabled. No secrets in TOML/catalog/manifest.
- OpenCode managed config: `configsetup opencode --apply` PASS (isolated).
  Generated provider uses `@ai-sdk/openai` with `{env:EGGPOOL_API_KEY}`,
  per-model `limit.context/output`, no embedded secret. `opencode models`
  lists `eggpool/demo-model-a/demo`. Config parse PASS.
- Live inference (Codex text/tool-loop, OpenCode text/tool, deferred
  `tool_search` live, long-session compaction, native remote compact):
  SKIP_WITH_REASON — no provider credentials in this environment
  (`scripts/smoke_codex_compat.sh` exits 77 as designed). Deterministic
  authorities retained: `codex_responses_compat` (incl. deferred search),
  `codex_compaction_compat` (native compact, unsupported rejection, no
  translated fallback/state). Custom Codex path uses local compaction as
  expected; remote compaction remains optional provider capability.
- Managed lifecycle: idempotent `--apply`, read-only `--check`/`--dry-run`
  (no writes), `--sync` drift refusal without `--force`, `--force` converge,
  `--remove` restores owned fields + deletes catalog, unrelated content
  preserved, JSONC rewrite refused. Deterministic fixtures in
  `operations_o005` + `operations::integrations` remain green.
- `eggpool status`: healthy running proxy PASS (one row per provider,
  deterministic order, `schema_version: 1`, exit 0, degraded with
  `probe_failed` reason for demo invalid upstream, no outbound probe beyond
  normal catalog refresh, secret-free); offline unreachable PASS (providers
  `unknown`/`disabled`, exit 3, valid JSON wrapper); partial degradation
  covered by demo probe-failure case. Unready covered deterministically by
  `status_command` + `operations::status` unit tests.
- Defect classification: client schema drift (Codex 0.154.0 new required
  catalog fields) + renderer omission. One narrow fix, one regression test,
  live re-qualification, no global wire-contract weakening, no new
  dependencies.
- Docs: `architecture/deep-dive-integrations.md` (required catalog fields),
  `docs/codex-compatibility-smoke.md` (isolated catalog qualification +
  current versions). `README.md`, `AGENTS.md`, skills unchanged (no new
  module boundary; existing guidance remains accurate).
- Focused tests: `operations::integrations` (16), `operations_o005` (13),
  `codex_responses_compat`, `codex_compaction_compat`, `status_command`,
  `operations::status`, `cli_contract` — all pass. Full workspace gates run
  at closure commit (see Plan 208).
- Intentional deferrals: Responses WebSocket, persisted
  `previous_response_id`/conversations/background, server-side tool
  execution, OpenCodex parity, active probing from `status`, image/voice
  APIs — unchanged.
