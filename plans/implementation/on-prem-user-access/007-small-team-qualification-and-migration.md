# On-Prem User Access Milestone 007 — Small-team qualification, migration and release hardening

Status: closing

Repository baseline: `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9` (accepted new architecture on `plans/on-prem-small-team-user-access`).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md`.

Long-term requirements:
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: polish

## 1. Objective

Qualify secure on-prem user operation on real SBC and reasonable local
hardware, including fresh/legacy/headless upgrades, read/write backup
restore, single-user non-regression and small-team controlled load with up
to 49 **named users**. Produce a supportable resource-sizing/deployment
guide rather than an invented 50-concurrent-request SLA.
**Hard dependency:** accepted M006 closure. Real target hardware is an
operational evidence requirement, not a paper substitute.

## 2. Readiness and remaining qualification gate

M006 is conditionally closed with the admin/member route journey and
user-scoped client behavior implemented. M006's dashboard parity and M005's
estimate/recovery findings are carried into this qualification pass. Physical
SBC capacity evidence is still unavailable on the current macOS x86_64 host;
run reproducible synthetic/local checks now, but make no SBC or 49-named-user
capacity claim without explicitly provisioned target hardware. CI and hosted
ARM timings cannot replace physical Pi/Le Potato measurements.

## 3. Baseline evidence

`scripts/qualification_sbc.py` is the physical-target runner,
`scripts/qualification_stability.py` checks long-lived load,
`rust/tests/server_transport.rs` and `runtime_lifecycle_r009.rs`
test serving lifecycle, `tests/tooling/test_installer.py` and release
qualification tools validate install ownership. `docs/raspberry-pi.md`,
`docs/deployment.md`, `docs/backup-restore.md`,
`docs/agent-configuration.md` and
`architecture/deep-dive-database.md` define existing operational
expectations; preserve their current source-backed claims.

## 4. Non-regressing invariants

- This is one LAN/on-prem instance, not hosted SaaS. The first-admin SBC flow remains canonical; no mandatory external service.
- TOML/environment retain provider topology, provider credentials, outbound routing, model overrides and self-hosted LLM connection details. SQLite holds downstream users, policies, budgets, sessions and usage. Never make the web dashboard a general provider configuration editor.
- Existing Chat Completions/Responses/Messages/compact wire, streaming termination, auth-header compatibility, metadata redaction, recovery, provider scoring and health remain intact.
- Single SQLite gate/worker (WAL/NORMAL), `GenerationLease` and immutable provider topology remain authoritative. No secret/body logging, second retry/finalization owner or unbounded in-memory cache.
- A denied user cannot transform a permitted virtual model or provider qualifier into an unauthorized underlying dispatch.

## 5. Required qualification scope

### A — Fresh and migration matrix

Use disposable per-test `HOME`, config, runtime dir and DB. Exercise
binary-first quick-install and `eggpool onboard` in single-owner local
mode; noninteractive stdin-based bootstrap; restored older schema54
database; upgraded legacy `server.api_key`; systemd owned files; local
password reset; single-user normal client operations; adding a second
user; retiring a shared key without accidental privilege extension;
restarting/reloading with changed TOML; backup/restore across versions;
partial/busy database; corrupted/missing state fail-closed; and rollback
instructions without implying older binaries can read newer SQLite.
No real credentials used. Keep paths shell-safe and ensure installed
`eggpool` command remains immediately usable per existing installer
closure.

### B — Representative capacity profiles

Measure single-user baseline with current release and new auth build on
the same hardware/config, then 1, 5, 10, 25, 49 locally created identities
with independently scoped credentials and profiles. For each named-user
count run bounded concurrency levels (for example 1, 2, 4, 8, 16
simultaneous requests as hardware allows) with explicit provider stub
latency, body size, stream mix and quota conditions; do not equate
named-user count with simultaneous active completions. Use reproducible
loopback fake providers, local/self-hosted model where available, and
fixed request corpora, not unbounded paid live tokens. Capture idle and
peak RSS, binary size, CPU, login Argon2 hash duration and memory
contention, auth cache hit/miss, SQLite gate wait, transaction duration,
WAL growth/checkpoint behavior, request p50/p95/p99 and TTFT/throughput.
Separate provider latency from proxy overhead. Compare Le Potato/Pi 5
when available plus ordinary Linux/macOS host; do not claim a target
without physical evidence.

### C — Security/correctness fault campaign

Re-run fresh/legacy shared token bypass matrix, member/admin/anon role
tests, first-login and CSRF, spoofed trusted proxy headers, unpriced local
model caps, selector/virtual-model bypass, quota simultaneous last slot,
retry/stream aborts, policy revoke midstream, process kill during
reserve/publication/finalization, power-loss/restart simulation,
DB corruption/read-only, and user-specific model discovery.
Ensure prompt/body/key privacy across diagnostics, traces,
`usage_rollups`, audit, backups and logs. Confirm data retention and
account deletion strategy preserves audit/usage without leaking
member-private details.

### D — Deployment/release documentation

Publish honest sizing and hardening table (LAN HTTP threat model,
trusted TLS proxy/VPN, firewall, operator's upstream terms, no SaaS
registration, private self-hosted upstream support, password/token
rotation, multi-user upgrade steps) with tested commands. Add
scripted regression guards to CI where reproducible and use physical
qualification artifacts for hardware claims. Avoid introducing remote
access feature or native HTTPS server.

## 6. Out of scope

No cloud load service, hosted billing or metering, cluster load sharing,
Kubernetes operator, 50 simultaneous generations guarantee, total
provider costs being inferred from flat-rate plans, WAN setup or
security compliance certification.

## 7. Ordered work packages

1. Freeze protocols/DB/CLI/browser compatibility matrix and test corpus,
   establish physical target baseline and acceptance gate thresholds.
2. Execute fresh/legacy/headless upgrade/recovery/backup and negative
   authentication regression harness. Repair any found bug through new
   narrowly scoped corrective plans when cross-subsystem.
3. Run paired pre-/post-change SBC and ordinary-host measured load
   at each named-user level with bounded concurrency; record raw/summary
   evidence and repeat outliers.
4. Reconcile docs, qualification scripts, release/security instructions,
   migrations and planning status; mark closure only with evidence.

## 8. Failure, cancellation, restart and contention

Any quota/policy bypass, cross-user data leak, lost-admin unrecoverable
state, or silent downgrade to keyless auth is release-blocking.
Missing hardware evidence leaves physical-capacity claims unqualified.
No config/database mutation of contributor's real deployment, no real
upstream account tokens in qualification artifacts. Shutdown and reload
must preserve recoverable user reservation and attribute active requests
correctly; on error, fail closed for protected endpoints. Detect
stale-SQLite/backup scripts and migration checksum drift.

## 9. Compatibility/migration

Backups include identity, credential hashes, session state and policy
while provider secrets remain config/env-owned; restore must preserve
token invalidation and stable user IDs. Old-client `EGGPOOL_API_KEY`
environment semantics continue with personal tokens. Migrations are
forward append-only and older executables may require restore of a
matching pre-upgrade backup to roll back. Maintain dashboard parity,
`newkey/getkey` legacy transition documentation and installer provenance.

## 10. Focused validation

Add tooling coverage (for example
`tests/tooling/test_on_prem_user_qualification.py`) with entirely
synthetic users/models/traffic; extend
`tests/tooling/test_qualification_sbc.py`,
`test_qualification_stability.py` if present or add a scoped fixture,
`test_installer.py`, `test_service_transition.py`, and
`rust/tests/database_compatibility.rs`,
`runtime_lifecycle_r009.rs`, `coordinator_finalization.rs`.
```bash
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
```
## 11. Required verification commands

Run the following after focused tests; preserve exact results for closure:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
If new dependencies/feature flags: `cargo deny --manifest-path rust/Cargo.toml check`,
`cargo tree --manifest-path rust/Cargo.toml -e features`,
`cargo build --manifest-path rust/Cargo.toml --locked --release`.
No test may be declared passed unless actually run.

CI is necessary but physical SBC results and resource observations
are additional acceptance evidence.

## 12. Documentation

Update `README.md`, `docs/raspberry-pi.md`,
`docs/deployment.md`, `docs/backup-restore.md`,
`docs/firewall.md`, `docs/agent-configuration.md`,
`docs/cli-reference.md`, `architecture/overview.md` and
deployment/security deep dives only to reflect verified behavior.
Use model/provider compatibility-specific caveats where needed.

## 13. Acceptance criteria

A fresh single-owner install is straightforward, an existing owner
upgrades with no data loss or surprise lockout, two users exhibit
independent permissions/quotas and correctly scoped client catalogs,
and all negative/security/restart/backup tests pass. Physical
measurements establish feasible **named-account** scaling and practical
concurrency boundaries with quantified regression against single-user
reference. Published documentation never calls Eggpool SaaS or
enterprise identity platform and never claims 50 simultaneous LLM
requests without evidence.

## 14. Stop conditions

Stop on uncorrected high-severity privacy/auth/quota defect; absent
representative target hardware; unknown backup/migration guarantees;
uncontrolled resource growth; unrelated provider transport regression
or license-sensitive default sharing claim. Record blocker or issue a
bounded follow-up, never waive security with an unsubstantiated metric.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/007-status.md`: complete artifact
manifest, hardware OS/RAM/disk/build hashes, named users versus actual
concurrency, auth-cost measurement, TTFT/request/SQLite/WAL deltas,
all migration/restart/backup/security checks, docs diffs, actual CI
and serial test outcomes, severity findings, final capacity statement,
and downstream unblock/registry audit. Only this evidence can
qualify the 49-named-user operating target.

## 16. Handoff

Run physical tests only on explicitly provisioned isolated SBC and
ordinary target hosts; no real upstream credentials. Preserve stable
release bounds and existing routing/accounting semantics.
