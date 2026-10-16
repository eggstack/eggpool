# On-Prem User Access M022 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/022-hard-budget-agent-compatibility.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m022--hard-budget-agent-interoperability`

Repository baseline reviewed: post-merge `88f58bbf`; implementation commit
`dee65ae`; status transition `45f43e9` (active → closing).

## 1. Executive finding

Hard-budget interoperability evidence is recorded for the installed Codex CLI
and OpenCode versions and for each supported request surface's explicit output
bound fields. Codex CLI 0.162.1 omitted an output bound on its captured
Responses request; OpenCode 1.18.35 sent the configured `limit.output` as
`max_output_tokens`. Hard budget proof remains caller-bound and fail-closed.
No implicit output cap or estimated fallback was added. Budget errors now
include stable codes and actionable, sanitized guidance. M022 is closed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Freeze a sanitized client and surface matrix | `rust/tests/fixtures/on_prem_budget_agent_compatibility.json`; `on_prem_budgets::sanitized_agent_budget_capture_corpus_records_supported_surfaces` | pass | The client capture used synthetic requests and a local mock that returned HTTP 400. Only version, endpoint, stream and output-cap facts are retained; no body, prompt, key or provider call. |
| Measure actual coding-agent cap behavior | Same fixture and `docs/model-limits.md#coding-agent-compatibility` | pass | Codex CLI 0.162.1 omitted the cap; OpenCode 1.18.35 sent `max_output_tokens: 1024` from its configured `limit.output`. OpenCode retried the mock response; the evidence fixture records the observed field shape. |
| Preserve supported native output-cap extraction | `request/limits.rs::requested_output_tokens`; `on_prem_budgets::admission_extracts_per_surface_output_bounds` | pass | Chat accepts `max_completion_tokens`/`max_tokens`; Responses and compact Responses accept `max_output_tokens`/`max_completion_tokens`/`max_tokens`; Messages accepts `max_tokens`. |
| Reject omitted or invalid caps under hard rules before provider dispatch | `on_prem_budgets::hard_token_rule_denies_unbounded_request_without_upstream_io`, `hard_token_rule_denies_missing_proof_without_upstream_io`, `invalid_agent_output_caps_are_rejected_across_surfaces`; `on_prem_global_budgets::global_hard_token_and_usd_deny_unprovable_without_upstream_io` | pass | Existing admission proof and atomic publication gate remain authoritative. No request-body mutation or output-cap inference was added. |
| Return stable, sanitized budget failure codes and corrective guidance | `EndpointError`; `endpoint_error_body_for`; `on_prem_budgets::endpoint_error_maps_not_enforceable_to_stable_429`; `docs/api-reference.md` | pass | `hard_budget_not_enforceable` is used for missing proof, unknown tariff or overflow; `budget_exceeded` means a rule is exhausted. The proof failure message recommends a supported explicit cap and an operator tariff when hard USD applies. |
| Preserve retry settlement, private-model budgets and provider-health boundaries | Existing `on_prem_budgets`, `on_prem_global_budgets`, `on_prem_attribution`, coordinator and provider suites; request lifecycle documentation | pass | No `Retry-After` is emitted for a policy/proof decision. No retry, routing, provider health, persistence or price-accounting behavior was changed. |

## 3. Production implementation evidence

`request/limits.rs` continues to extract only positive, surface-compatible
caller output caps. `BudgetProof` and the publication transaction remain
unchanged: hard token rules require the explicit cap, hard USD rules also
require the operator tariff, and no proof means pre-dispatch denial.

Inference errors now use `hard_budget_not_enforceable` and `budget_exceeded`
machine codes in OpenAI-shaped and Messages-shaped envelopes. The hard-proof
message names both safe corrections (a caller cap and, for hard USD, an
operator tariff) because the durable transaction result deliberately does not
expose whether the underlying missing proof was output, price or overflow.

An operator-injected cap was evaluated and rejected: available protocol and
client evidence does not prove one inferred limit would be honored across
native, translated, streaming, compact, retry and fallback-provider paths.
Codex must provide a request-level cap for hard token rules. OpenCode
operators can configure `limit.output` so the client sends one.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets --no-default-features -- --test-threads=1
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk git diff --check
```

### Results

- Final implementation workspace runs: default features 1,039 passed and 1
  ignored across 80 suites; no-default features 1,040 passed and 1 ignored
  across 80 suites.
- Final focused budget target: 29 passed with default features and 29 passed
  with `--no-default-features`.
- Formatting, default/no-default strict Clippy and no-default workspace check
  passed.
- Tooling pytest: 192 passed, 1 skipped. `git diff --check` passed.
- Local verification only; hosted CI and hardware performance qualification
  were not run for this milestone.

## 5. Invariant review

- No hard request/token/USD rule is weakened; no unbounded request reaches a
  provider because of this work.
- Requests are not mutated to synthesize output bounds. Zero, malformed or
  absent caps cannot become hard proof.
- USD proof still requires the selected provider's conservative operator
  tariff; estimator fallbacks do not become proof.
- Client-facing errors disclose no user budgets, provider IDs, accounts,
  prompts, tokens or request bodies.
- Policy intersection, retry holds, selector overhead, cancellation and
  stream finalization retain their previous owners and behavior.

## 6. Failure and recovery review

This change adds no durable state, queue, background task, retry or network
call. The admission proof is immutable request data and is checked in the
existing atomic publication path before provider submission. Retry settlement
and stream/cancellation recovery are covered by the unchanged serial
coordinator and budget suites. Invalid limits fail during request admission;
missing hard proof and unknown tariff fail closed at publication.

## 7. Migration and compatibility review

No schema or configuration migration was needed. Existing estimated rules
retain their semantics. Existing explicit cap aliases remain supported per
surface. The new error codes are additive JSON fields; existing status remains
HTTP 429. No `Retry-After` is emitted for a budget policy decision.

## 8. Security review

Only synthetic requests reached a local loopback mock during client capture.
The checked-in fixture contains only sanitized version and field facts. No
real prompt, provider key, request body, or provider traffic was used. Error
messages are static and do not reveal rule scope or tariff values. No
privilege or routing boundary changed.

## 9. Documentation and operations

Updated `docs/model-limits.md`, `docs/agent-configuration.md`,
`docs/api-reference.md`, and `architecture/deep-dive-request-lifecycle.md`.
The client guidance tells OpenCode operators to set `limit.output` and
explains that Codex CLI requests without a cap cannot use hard token budgets.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M022 correctness, security or compatibility defect remains. | Hard proof behavior and client guidance are verified locally. | None. |

## 11. Roadmap disposition

M022 is closed. M023's hard M022 dependency is satisfied. Its remaining
ordinary-server Linux/SSD-or-NVMe measurement is an operational evidence gate
for implementation acceptance and team-capacity claims. The available runner
is a Darwin MacBook Pro, so it cannot supply that evidence. The independent
concurrency M002 tooling/setup slice is conditionally closed, but its attested
Ubuntu host exceeded the predeclared I/O-pressure stop gate during build
verification; no qualification cell or capacity measurement ran. M023 remains
blocked pending an isolated ordinary-server result. No other on-prem
user-access plan after M023 is registered as eligible.

## 12. Registry updates

The closure commit marks M022 `implemented` and the roadmap milestone `closed`,
adds this record to recent closures, and records the M023 unblock audit.
M023 remains `blocked` with M022 removed from its blocker; its remaining
operational gate is the unmeasured ordinary-server M002 qualification. No other
dependency-ready or active plan is promoted.
