# On-Prem User Access M016 — Final on-prem integration and PR #7 merge-readiness reconciliation

Status: blocked

Repository baseline: `3c7def1e35871209e68563b2ee88f36033aff76e` on `plans/on-prem-small-team-user-access` (PR #7, open; 134-file implementation, schema 62).

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m016--final-integration-and-merge-readiness-reconciliation`

Long-term requirements:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted; no new ADR needed unless an auth or lease boundary must change).

Primary class: polish

## 1. Objective

Produce one auditable pre-merge recommendation for PR #7 with its
current implementation, security review, fixed TLS dependency, hosted
CI status, accurate planning registry, release docs and documented
Pi 5 concurrency envelope. Do **not** merge automatically. This
is an integration/closure gate, not a duplicate Rust implementation
milestone or a new performance promise.

## 2. Why this milestone is blocked

**Hard:** M014 (Rustls advisories resolved + hosted audit green)
and M015 (independent authz/threat review accepted) must both
close. All older on-prem M001–M013 work is implemented and
has historical closure records; this plan cannot inherit a
stale green CI badge from a prior SHA. **Operational:** hosted
CI/installer/dependency audit, browser parity fixtures and release
artifact qualification must be available for final closure.
The separate optional Pi MMC tail-latency investigation does
**not** block this plan, provided docs retain its measured
restrictive operating envelope.

## 3. Current implementation evidence and why extra gate is needed

As of `3c7def1e35871209e68563b2ee88f36033aff76e`, PR #7 is open, mergeable at a Git tree level,
but check state is `unstable` because the dependency audit
fails. It has 134 changed files / 58 commits and includes
new identity, policy, quota, dashboard, recovery and planning
code. M012 measured physical Pi 5 under MMC storage: solo
p50 ~4–5 ms, low idle RSS, 49 **named** identities provisioned,
but concurrent p95 up to ~14 s and 8-way saturating; do not
market 49 concurrent inference sessions. M011 restored browser
sign-in, new-page browser reference and unchanged historical
Python parity count; M013 fixed a routing eligibility line
lost in M008 that Rust unit tests missed. These are integration
risks to verify at the *final merged candidate*, not an
excuse to reopen correct historical closure records.

## 4. Invariants that must not regress

- EggPool remains a **single on-prem instance** for an owner or small team,
  with provider topology, upstream credentials, TLS trust, private LLM
  endpoints and routing configuration controlled by operator TOML/environment;
  user identities, profiles, sessions, audit and downstream quotas stay in
  SQLite. No SaaS, org/tenant control plane, embedded reverse proxy, external
  IAM, secret-bearing HTML or browser provider editor.
- Preserve generation leases, provider health/eligibility, request identity,
  retry and streaming finalization, single SQLite gate/worker and WAL/NORMAL,
  live/restart config authority, all supported Chat/Responses/compact/Messages
  client and cross-protocol routes, and the canonical solo SBC installation.
- Authorization and budget failure must not become provider unhealthiness
  or anonymous fallback. No unauthenticated web mutation, shared
  `admin/admin` credential, bypass of profile/global/model denies, or
  moving operator secrets to SQLite.
- All qualification uses fake provider tokens, temporary roots and redacted
  artifacts; never production data or real user passwords.

## 5. Scope

### In scope

- Freeze PR head/base, inspect changed-file manifest and compare
  all M001–M016 plan/closure claims to actual code and CI.
- Confirm M014 rustls security resolution and M015 threat review.
- Re-run the full combined auth/model/budget/transcode/UI/installer
  compatibility matrix on the final locked dependency graph.
- Validate clean merge calculation, accurate release documentation,
  operator migration and rollback, version/release provenance and
  safe one-owner onboarding; update stale PR title/description.
- Final reviewer approval or explicit hold with exact failed gate.

### Explicitly out of scope

No automatic merge, forced history rewrite, packaging release,
SaaS roadmap, fresh whole-codebase performance refactor, auth
redesign, new enterprise or cluster features, or claim of
on-prem 49-way throughput.

## 6. Required production/documentation changes

No production change is expected if M014/M015 close cleanly.
If merge review finds a **new** release-blocking violation,
write an independently bounded corrective plan and keep M016
blocked; do not apply opportunistic major rewrites.

Fix PR #7 metadata: current title/description still describes
"M008–M012 registered" despite M008–M013 implemented and M012
closed on hardware. Explain precisely that implementation is
on the branch and not yet in `main`, that audit/security
correctives supersede the old claim, and that 49 accounts
are distinct from active inference concurrency. Update docs
only when source-tested claims materially changed. Ensure
status markers in `plans/registry.md` and roadmap show
M014/M015 closed **only after** accepted closure evidence;
M016 itself stays active/closing until exact SHA verification.

## 7. Ordered work packages

1. Merge preflight — list final base and head SHA, commit/change
   diff, migration inventory 54→current and additive 61/62,
   release/hardware artifact hashes and public API/CLI changes.
2. Verify closure dependencies — independent M014 hosted
   audit + M015 security matrix, confirm no ignored RustSec
   advisories or privilege/quotas findings.
3. Execute combined full Rust default/no-default, Python
   tooling/Q007, UI/browser parity and installer-shell tests
   on one immutable candidate; check merge conflict and test
   actual merge result if branch base has moved.
4. Reconcile PR description/review checklist, source-backed
   README/deployment/model-limits/Raspberry Pi claims, and
   planning roadmap/registry. Produce an explicit
   `merge recommended` or `merge held` disposition; only
   maintainer decides to merge.

## 8. Failure, cancellation, restart and contention semantics

Prove combined latest code retains M010 startup recovery
release of abandoned **user and global** holds, no double-charge,
unattributed legacy control, disabled-user fencing, TLS negative
certificate failures, no shared-key browser privilege, and
cross-protocol transcode. The dashboard browser must still sign
in with real Origin+Referer pair; privilege/CSRF tests remain
green across different roles. No SQLite gate-held provider I/O;
outstanding physical Pi tail regressions stay clearly documented.
CI of the final head must be genuinely green; rerun flaky
jobs with documented first-failure cause, not a green-only
report.

## 9. Compatibility and migration

Validate old schema-54 restore/upgrade through schema 62, current
personal token/role/bootstrap, `--locked` release, and
agent integrations via `EGGPOOL_API_KEY` against both
loopback and trusted TLS-proxy posture. The shared
`server.api_key` remains migration-only as documented;
preserve provider TOML, private LLM choice and solo SBC
installation. Downgrading requires matching pre-upgrade
DB backup, not down-migration. No new schema in this plan.

## 10. Required tests

Focused combined tests:
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_model_policy -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
uv run pytest tests/tooling/test_qualification_live_provider.py -q
uv run python scripts/validate_release_docs.py
```
Use `scripts/qualification_dashboard_parity.py` with its
actual supported flags and pinned oracle/reference for
historical/new pages; confirm documented M011 reproducibility.
Include tests for installed release binary after dependency bump,
not only `cargo test` debug artifacts.

## 11. Required verification commands

Record commands/results **actually executed** at the implementation SHA:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv sync --frozen
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```
Use `cargo deny check` with the repository's root `deny.toml`
and verify the command/working directory that the GitHub audit workflow
actually invokes. Run `cargo build --manifest-path rust/Cargo.toml --locked
--release` for production-affecting Cargo changes. The hosted CI,
installer-shell and dependency-audit jobs must be green on the final
head SHA; no earlier green check is a substitute.
## 12. Documentation updates

Update `README.md`, `docs/deployment.md`,
`docs/agent-configuration.md`, `docs/model-limits.md`,
`docs/raspberry-pi.md`, `docs/backup-restore.md`,
and `architecture/deep-dive-security.md` **only if necessary**
to reconcile actual implementation. Update PR #7 description
and merge checklist; never misstate 8-way Pi performance.

## 13. Acceptance criteria

At the exact PR SHA intended for merge, default/no-default
serial Rust and local tooling tests pass, hosted CI + Linux/
macOS installer + cargo-deny all pass, security review has no
unresolved high/critical/medium authorization or privacy
defect, Rustls advisories are not ignored, dashboard role and
historical parity are qualified, migration/restore remain
sound, and no in-repo source claim exceeds measured Pi 5
capacity. Planning links/closures align and PR description
matches the diff. **Merge recommendation is explicit, but the
plan never performs the merge**.

## 14. Stop conditions

Any failed dependency audit, missing M015 closure, unreviewed
new attack surface, unqualified schema rollback, red hosted
integration, stale docs claiming a 50-concurrent guarantee,
or unresolved merge conflict => hold PR, open scoped fix.
If the main branch advanced materially, rebase/merge-test
the actual intended integration base under maintainer
policy instead of assuming the old base.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/016-status.md`: final
PR/base/head SHA, diff/changed-file and migration manifest,
evidence links to 014/015 closures, complete local/hosted
job URLs and results at final SHA, mergeability/approvals,
security matrix disposition, legacy/managed recovery,
browser/oracle, hardware envelope caveat, docs/PR metadata,
severity-tagged findings and registry unblock audit.
Only `closed` with evidence may restore the on-prem
subsystem roadmap to `closed`.

## 16. Handoff notes

Run only after M014/M015 closure. Do not merge or release
automatically, and do not mark M016 closed before the
full integration head checks pass. The independent MMC
tail-latency workstream is follow-up research, not a
backdoor release blocker or a license to violate WAL
architecture invariants.
