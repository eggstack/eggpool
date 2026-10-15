# On-Prem User Access M015 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/015-authz-threat-review-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m015--independent-authentication-and-authorization-threat-review`

Repository baseline reviewed: PR #7 candidate `3c7def1e35871209e68563b2ee88f36033aff76e`; final reviewed implementation SHA `d8fd9c2a5cbcd4cda317a215e17e9a188699cb71`.

## Implementation commits

- `a526980c` — harden on-prem authentication and authorization boundaries.
- `deb1622f` — address independent review findings on hard-budget reservation and browser budget authorization.
- `d8fd9c2a` — require conservative token proof for hard-cost reservations and allow browser-session authentication for `/api/user-budget` while preserving role and target checks.
- `80852e95` — record M015 active status before implementation.

## 1. Executive finding

M015 is closed on the exact PR #7 candidate SHA above. The independent
adversarial review found two substantive defects: hard USD reservations used
estimated token counts instead of the conservative proof bound, and browser
sessions could not reach `/api/user-budget` because personal-credential
middleware rejected the request before session authentication. A subsequent
review also found that the configured shared-key early rejection had the same
effect for cookie-only sessions. All three paths were corrected with
regression coverage. The independent reviewer confirmed the final patch
closed its findings, and the completed scoped Codex Security diff scan found
no additional reportable issues.

The full local default/no-default Rust suites and exact-SHA hosted CI,
installer and dependency-audit jobs pass. M016 is now dependency-ready; it
owns final PR #7 integration, metadata and merge recommendation. PR #7 remains
open and has not been merged. The separate Pi MMC latency plan was excluded
and untouched.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Review identity, browser-session, personal-token and shared-key boundaries | Independent review of PR #7 auth middleware and session paths; `rust/tests/on_prem_sessions.rs`; scoped security scan `52bd79cc-9e8b-4aef-a405-4ec2486f8e65` | pass | Cookie sessions reach `/api/user-budget`; managed shared-key presence does not block session auth; legacy shared-key behavior remains enforced. |
| Preserve user and role isolation for budget reads | `browser_sessions_can_read_scoped_user_budget_api` in `rust/tests/on_prem_sessions.rs` | pass | Member sees self only; other-member targeting is forbidden; admin can select a member; instance rules stay admin-only; response uses `no-store`. |
| Prove hard-cost monetary holds from a conservative token bound | `cost_hard_holds_require_and_use_conservative_proof_tokens` in `rust/src/user_budget.rs` | pass | Admission and retry use `single_attempt_worst_tokens(proof)`; missing output cap fails closed; an underestimated estimate cannot reduce the hard-cost hold. |
| Review broader route, CSRF, proxy, model-policy, recovery, secret and redaction invariants | M015 route/principal and authz review; existing focused on-prem/session/model/budget/transport/recovery tests; no schema or auth contract change | pass | No new privilege, model-policy, CSRF, trusted-proxy, migration, or secret-redaction violation remains open. |
| Correct findings with bounded changes and regression tests | `deb1622f`, `d8fd9c2a`; focused integration/unit tests | pass | The fixes remain within existing route and budget ownership. |
| Complete final local verification at the reviewed source | Commands and results below | pass | Default and no-default serial suites both pass. |
| Confirm hosted checks at the exact reviewed SHA | CI `38720507683`, installer `38720509034`, dependency audit `38720507720` | pass | All three completed successfully with head SHA `d8fd9c2a5cbcd4cda317a215e17e9a188699cb71`. |

## 3. Production implementation evidence

`rust/src/user_budget.rs` computes hard-cost admission and retry amounts from
the single-attempt worst-case token proof, multiplied by the existing bounded
attempt-equivalent factors. It fails closed when a usable proof is absent.
This prevents a low or resource-capped estimate from justifying an
under-reserved hard USD ceiling.

`rust/src/server/middleware.rs` exempts `/api/user-budget` from the two
personal-credential-only rejection branches so the existing browser-session
authentication and handler authorization can run. This does not grant access
to the shared server key: managed users still resolve through their session,
members remain self-scoped, and legacy mode continues to require its shared
key. The integration test covers configured shared-key behavior, member and
admin scope, cross-member denial, instance-rule visibility, and cache policy.

No database migration, public API shape, provider routing behavior, or
authentication contract was added or changed. No Pi performance plan or
physical-Pi artifact was changed.

## 4. Verification executed

### Commands run

```bash
cargo test --manifest-path rust/Cargo.toml --lib cost_hard_holds_require_and_use_conservative_proof_tokens -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions browser_sessions_can_read_scoped_user_budget_api -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets -- --test-threads=1
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
git diff --check
```

### Results

- New conservative hard-cost unit test passed. The browser budget integration
  test passed, including configured server-key managed mode, member
  self-scope, cross-member 403, admin target access, instance-rule visibility,
  and `no-store`.
- Focused suites passed: `on_prem_sessions` 10, `on_prem_budgets` 27,
  `on_prem_global_budgets` 16.
- Full default workspace suite: 1,024 passed, 1 ignored across 79 suites.
- Full no-default workspace suite: 1,025 passed, 1 ignored across 79 suites.
- Formatting, strict default Clippy, no-default check, and strict no-default
  Clippy passed. `git diff --check` passed before the implementation commit.
- Hosted CI [38720507683](https://github.com/eggstack/eggpool/actions/runs/38720507683),
  installer qualification [38720509034](https://github.com/eggstack/eggpool/actions/runs/38720509034),
  and dependency audit [38720507720](https://github.com/eggstack/eggpool/actions/runs/38720507720)
  all passed on exact head `d8fd9c2a5cbcd4cda317a215e17e9a188699cb71`.
- The prior M015 local verification also passed `cargo deny`, locked release
  build, Python tooling, release-doc validation, and browser parity/Q007
  qualification. Those results were run on the immediately preceding M015
  implementation before the final narrow Rust correction; the exact final
  hosted CI/installer/audit jobs above cover `d8fd9c2a`.

## 5. Invariant review

Authentication remains fail-closed. A missing/invalid managed credential
does not fall back to anonymous or legacy access; the budget route reaches
the existing session resolver and role-scoped handler. Cross-user target
selection remains denied to members. Hard USD holds now require a conservative
token proof for both admission and retry. No provider health, quota, request
publication, retry ownership, streaming finalization, secret persistence, or
redaction boundary changed.

The first scoped security scan (`776c75d7-a6f4-4210-8ba1-d3828989d889`)
reported no findings for its corrective diff, but its bounded review did not
identify the two pre-existing gaps later found by the independent reviewer.
The reviewer’s findings were therefore fixed and separately re-reviewed; the
follow-up scan `52bd79cc-9e8b-4aef-a405-4ec2486f8e65` reviewed the exact
`deb1622f..d8fd9c2a` corrective diff and completed with no reportable
findings. Neither bounded diff scan is represented as an exhaustive
repository-wide security certification.

## 6. Failure and recovery review

Missing output proof fails closed for hard-cost admission and retry. Missing
or invalid browser credentials proceed only to the existing session check and
are rejected when no valid session exists. Member target escalation remains
403. The changes add no persistent state and therefore require no recovery or
restart procedure.

## 7. Migration and compatibility review

No schema or migration changed. Existing personal agent credentials, browser
session cookies, CSRF validation, legacy shared-key mode, and managed-mode
roles retain their contracts. Hard-cost reservations become more
conservative and may reject when no output proof exists; that is the
fail-closed behavior required by the existing hard-budget contract.

## 8. Security review

The initial independent review identified and ranked:

| Severity | Finding | Disposition |
|---|---|---|
| High | Hard USD reservations derived from estimates rather than conservative token proof, allowing under-reservation | Fixed in `deb1622f` and strengthened in `d8fd9c2a`; unit regression test proves estimate cannot lower hold and missing proof fails closed. |
| Medium | Browser sessions could not access `/api/user-budget` through personal-credential middleware | Fixed in `deb1622f`; middleware now permits session authentication, with a role and target-isolation integration test. |
| Medium | Configured shared-key early rejection still blocked cookie-only session access in managed mode | Fixed in `d8fd9c2a`; same integration test uses a configured server key and confirms managed cookie-session authorization. |

The independent reviewer confirmed the final corrections close all reported
findings. The exact corrective-diff security scan found no additional
reportable issue. There are no unresolved high or medium authorization or
privacy findings.

## 9. Documentation and operations

No product documentation change was required; the fixes restore existing
authorization and hard-budget contracts rather than introducing new behavior.
The closure, roadmap, plan status and registry now record current state.
M016 must reconcile stale PR #7 description and perform the final merge
readiness review. PR #7 has not been merged. The physical Pi 5 restricted
operating envelope in M012 remains the only qualified performance statement;
the separate Pi MMC attribution plan is not a dependency and was not touched.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| None | No unresolved M015 security-correctness finding | No known M015 release blocker remains. | M016 performs final candidate and PR reconciliation; do not merge automatically. |

## 11. Roadmap disposition

M015 is closed; M016's hard dependencies M014 and M015 are accepted. The
registry audit found M016 is the only registered successor blocked on this
closure, so it is promoted to `ready`. It can now begin final-SHA integration
and PR #7 reconciliation. M016 remains an explicit no-merge plan. The
separate on-prem concurrency-performance M001 Pi 5/MMC plan is neither a
dependency nor modified by this closure.

## 12. Registry updates

In the same closure commit, `plans/implementation/on-prem-user-access/015-authz-threat-review-corrective.md`
is marked `implemented`; the roadmap marks M015 `closed` and M016 `ready`;
`plans/registry.md` removes M015 from active work, records M015 in recent
closures, and promotes M016 from blocked to ready. The stale registry audit
claim that M015 “remains ready” is replaced with the accepted closure and
successor-promotion evidence. No Pi plan, closure, or artifact was changed.
