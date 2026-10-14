# On-Prem User Access M012 — Physical SBC and small-team qualification closure

Status: blocked

Repository baseline: `c18dcc2051ae4987a2cb95a7cd046b8aff2095ff` on `plans/on-prem-small-team-user-access` (PR #7), schema 60, M001–M007 closure records as of 2026-10-13.

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m012`

Long-term references:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: polish

## 1. Objective

Physical SBC and small-team qualification closure. This is a **new corrective** to the conditionally closed M005/M006/M007 records, not a rewrite of historical closure.

## 2. Readiness and dependencies

**Hard:** accepted M008, M009, M010 and M011 closures, with
stable budget/storage/UI contracts. **Operational:** provisioned
physical Le Potato/Pi-class machine and ordinary Linux reference
host; macOS x86_64 synthetic fixtures are insufficient. This
qualification plan is registered but **not ready** until the
dependency and evidence gates are met. It is not a promise to
perform hardware work without those targets.

## 3. Evidence and why prior tests missed it

`plans/closure/on-prem-user-access/001-status.md` deferred Argon2id
19 MiB / two-worker memory and latency measurement.
`003-status.md` deferred principal/accounting overhead.
`007-status.md` records 7/7 local synthetic loopback
requests at schema 60 on macOS x86_64, no physical SBC
or 1/5/10/25/49-user/concurrency benchmarks, no paired
legacy single-owner control, no WAL/SQLite gate percentiles.
The existing `scripts/qualification_sbc.py` is the one
physical-target runner; `scripts/qualification_stability.py`
and `tests/tooling/test_qualification_sbc.py` provide
tooling fixtures. Previously measured Pi WAL checkpoint stalls
mean simply observing green average latency is not sufficient.

## 4. Non-regressing invariants

- Canonical use is **one administrator on one local/SBC instance**, optional <50 named users. No SaaS, organization tenancy, billing platform, centralized IAM, new database writer/service, built-in WAN/VPN/reverse proxy, or mandatory cloud calls.
- Upstream provider topology, API keys, routing and self-hosted model endpoints remain in TOML/environment; SQLite owns users, personal credentials, profiles, policy, downstream budgets, accounting and audit. Do not place upstream keys in user tables or web administration.
- Maintain generation leases, existing coordinator publication/finalization, a **single SQLite connection/gate/worker with WAL/NORMAL**, canonical wire and transparent streaming, bounded hot-path memory and provider fairness. Do not turn downstream user budget refusal into upstream provider unhealthiness.
- Preserve published OpenAI Chat/Responses/compact and Anthropic Messages behavior, permitted client auth headers, existing client profiles, dashboard themes and local legacy upgrade behavior.
- Keep identity and cost metadata redacted, prohibit secret/password/bearer leakage in logs, HTML snapshots or qualification artifacts, and fail closed rather than silently weaken configured security limits.

## 5. Required qualification scope

### A — Freeze actual target and work corpus

Provision and record CPU type, core count, OS image, RAM, disk/MMC/
filesystem, board model, power/network, Rust and binary SHA-256,
DB schema, persistent WAL flags, native feature set, config
and allowed provider stub. Verify representative lightweight
Le Potato and Raspberry Pi 5 where available; otherwise record
missing target separately, without silently substituting a laptop.
Run isolated synthetic users and fake upstreams; **never**
point test harness at production DB or real credentials.
Maintain both installed single-owner upgrade and newly provisioned
small-team startup flows. Identify and lock baselines before
candidate measurement.

### B — Named users distinct from concurrency

Measure **1/5/10/25/49 named users** (49 is not 49 simultaneous
LLM generations) under explicitly chosen bounded in-flight
concurrency levels 1/2/4/8 and optionally 16 when hardware and
memory headroom allow. For each compare same input corpora,
Chat/Responses/Messages, finite+SSE, allowed/denied model
selection, qualified alias/semantic routing, realistic tiny and
moderate payloads, user/global budget admission and last-slot
refusals. Include an operator-local self-hosted model fixture
(no cloud dependency); use fake provider responses and optional
local model only if isolated. Record actual concurrency and
completion totals, no hidden extra selector work.

### C — Physical Argon2 and runtime resource posture

Run 1 and 2 simultaneous Argon2id password verifications under
idle and representative inference load, measuring p50/p95
wall-time, RSS/peak heap, swap/OOM events and CPU.
Under above matrix capture binary size, idle and peak RSS,
CPU, credential cache/DB auth read, request p50/p95/p99,
stream TTFT, SQLite gate wait/tenure, WAL growth/checkpoint
progress, open-file/thread counts and backup/restart behavior.
Compare legacy unmodified main release, current managed
single-admin without budgets, and candidate with budgets.
The existing single-user SBC path must remain practical and
cannot require extra services or a multi-step SaaS onboarding.

### D — Qualification gates and release disposition

Before testing, choose explicit pass/fail gates using
existing `scripts/qualification_sbc.py` baseline evidence and
user-facing solo path (e.g. allowable TTFT p95/SQLite gate and
RSS deltas, bounded tail latencies, no WAL divergence,
no OOM/swap, no sustained request drops). Don't fabricate
thresholds or measurements after results. Failing a gate
requires small measured optimization or new corrective plan;
never introduce a second DB writer/connection or retune
checkpoint policy without a separate architecture acceptance.
If 49 named users are inexpensive while 8 concurrent inference
requests exceed hardware, document actual supported load
rather than marketing "50 users" as a throughput guarantee.

### E — Cross-cutting final release audit

Cross-reference M008–M011 closures, original conditions in
M001/M003/M005/M006/M007, independent schema-54 restore and
whole-workspace hosted CI. Run older unrelated
`operations::update` tests on the target or confirm M010's
documented environment-specific disposition. Reconcile
`plans/registry.md` and roadmap to `closed` only **if**
all normative requirements and physical gates have closure
evidence; otherwise keep `active` with precise blocked/conditional
items. Do not back-edit historical closure records.
Before merging PR #7, verify docs/PR summary accurately reflect
actual runtime work and security caveats.

## 6. Non-goals

No remote SaaS load generator, cloud IAM/service, internet-facing
deployer, exact promise of 50 simultaneous inferences, complex
multi-node scaling or unsupported SBC target claim.

## 7. Ordered work packages

1. Reuse `qualification_sbc.py` / tooling harness with bounded,
   repeatable user-identity/client/profile/quotas matrix; add
   `tests/tooling/` fixture validations and sanitization.
2. Freeze controls, build artifacts, scripts, hardware and
   acceptance thresholds; run paired target corpus and Argon2.
3. Record raw evidence/derived aggregates per hardware class;
   investigate failures and repeat controlled measurements.
4. Publish honest capacity/budget caveats, final release matrix
   and roadmap/registry closure or remaining blocker.

## 8. Failure, cancellation, restart and contention

Long-lived streams and canceled users must release correct
per-user/global holds; revocation rejects new inference.
Forced shutdown/restart, WAL growth and restoration remain
bounded. A fixture inducing physical memory pressure must
abort cleanly before kernel OOM rather than render the box
unresponsive. If hardware is unavailable, mark as
**blocked** and publish no target claims; successful macOS
CI never substitutes for physical results.

## 9. Compatibility and migration

Protect the original single-user TOML/provider/agent setup,
schema-54-into-current restored deployment, backup permissions,
API model discovery, anonymous readiness probes, self-hosted
provider config and `EGGPOOL_API_KEY` compatibility with
personal tokens. No rollback to older binary without
pre-upgrade DB backup.

## 10. Tests and target commands

New `tests/tooling/test_on_prem_sbc_qualification.py` and
updates to `tests/tooling/test_qualification_sbc.py`
should validate manifest, redaction, exact target binding,
named-users vs actual concurrency and baseline/candidate
pairing. Use actual existing
`scripts/qualification_sbc.py --help` to select arguments,
not hardcoded/assumed flags. Focused:
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
uv run pytest tests/tooling/test_qualification_sbc.py -q
```
## 11. Required verification commands

Run applicable focused targets in §10, then the full default/no-default gates:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
Run `cargo deny --manifest-path rust/Cargo.toml check`, the feature tree,
and `cargo build --manifest-path rust/Cargo.toml --locked --release` when
Cargo dependencies/features change. Use the repository's tooling/test commands
when scripts are changed. Closure records must distinguish local vs hosted
vs physical hardware results; never claim unrun tests as passed.
## 12. Documentation

Update `docs/raspberry-pi.md`, `docs/deployment.md`,
`docs/agent-configuration.md`, `docs/backup-restore.md`,
`architecture/deep-dive-database.md` and `README.md`
with only qualified capacity/requirements and recommended
operator TLS reverse-proxy/VPN for LAN browser passwords.
Run release docs validator for relevant files.

## 13. Acceptance criteria

All qualifying physical targets and reference-host results are
archived with reproducible manifests, p50/p95/p99 and named
user/concurrency coverage, and resource usage is within
**predeclared** accepted gates. No unqualified assertion about
Le Potato/Pi performance or 49 simultaneous completions.
M001/M003/M005/M006/M007 conditional findings have explicit
evidence-based resolution, with approved exception only when
non-normative and transparently documented. One-admin fresh
install remains low-friction, no provider secret moved to DB,
and managed policy/budget cannot regress isolation.

## 14. Stop conditions

Block closure on missing physical hardware, target-class OOM,
WAL nonconvergence, unaddressed hard-limit bypass, bad backup
identity, leaked tokens, failed visual security checks or
false capacity claim. Open narrowly scoped corrective for
new defects rather than marking a condition as implicitly
satisfied.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/012-status.md` must
include environment/bin/config fingerprints, direct target
and control artifacts, Argon2, named-user and concurrency
metrics, single-admin deltas, SQLite WAL and recovery,
long-running failures, eligibility checklist,
CI/migration/dashboard verification, known risks and
registry unblock audit. If hardware evidence is missing,
this milestone cannot close and on-prem roadmap stays
`active` with the blocker explicit.

## 16. Handoff

This is physical evidence / polish, **not** an authorization
to redesign persistence for an unsupported benchmark.
