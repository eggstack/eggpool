# On-Prem User Access Milestone 007 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/007-small-team-qualification-and-migration.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md`

Repository baseline reviewed: `e035b9a` (M006 conditional closure)

Implementation commits or pull requests:

- `a3f2d00a` — M006 admin/member implementation and qualification alignment
- `e035b9a` — conditionally close M006 and promote M007
- `29c4e2b6`, `235d717` — activate M007 and synchronize its registry status
- `64f0062` — begin M007 closure evidence collection

## 1. Executive finding

Local migration, auth, lifecycle, backup, installer, and synthetic loopback
qualification pass. The final local fake-provider run completed its bounded
seven-request matrix, recorded all seven attempts/completions, and left no
pending requests or active reservations. It ran on macOS x86_64 and is not SBC
capacity evidence. No representative Pi/Le Potato hardware was available, so
paired 1/5/10/25/49-user load, SBC Argon2/RSS costs, TTFT/latency percentiles,
WAL behavior under team load, and single-user versus team overhead were not
measured. M007 is therefore conditionally closed: the available local work is
complete, while the physical capacity claims in its plan remain unqualified.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Fresh bootstrap and personal-token operation | `on_prem_identity`, `on_prem_sessions`, `on_prem_admin_ui`, synthetic Q007 loopback run | pass | Fresh local admin bootstrap and personal token authenticate discovery/inference; shared legacy key is not used after managed initialization. |
| Two-user policy, account, and admin isolation | `on_prem_model_policy`, `on_prem_admin_ui`, `on_prem_client_profiles` workspace tests | pass | Distinct browser roles, profile policy, member data fences, token revocation and scoped discovery are exercised. |
| Legacy schema migration | `database_compatibility` (9 passed), migration inventory/build manifest tests | partial | A historical schema-11 fixture migrates through v60; a schema-54 semantic seed is exercised after current migrations. A restored schema-54 snapshot was not used as a source database. |
| Fresh/legacy install ownership and service transitions | tooling tests `test_installer.py`, `test_service_transition.py` | pass | Disposable/synthetic test coverage; no live installation was modified. |
| Backup and restore safety | `cargo test --lib operations::backup -- --test-threads=1` | partial | One backup module test passed; a cross-version schema-54 identity/policy restore was not performed. |
| Server transport and restart/lifecycle | `server_transport` (14), `runtime_lifecycle_r009` (6) | pass | Local loopback and lifecycle tests. |
| Durable terminalization and recovery | `coordinator_finalization` (11), serial workspace suite | pass | Existing cancellation/finalization invariants remain green. |
| Synthetic provider interoperation | `scripts/qualification_live_provider.py --offline-fake` | pass | 7/7 planned finite/streaming surface cases; catalog resolved all 3 fixture models; 7 completions, 0 pending requests, 0 active reservations. This is a short loopback correctness run, not load qualification. |
| Installer/SBC/stability/service tooling | focused tooling selection | pass | 51 passed; fixtures were disposable and did not target a real install or hardware. |
| 1/5/10/25/49 named-user capacity and explicit concurrency levels | physical qualification | not measured | No Pi 5/Le Potato target and no paired ordinary-Linux performance run were available. No capacity claim is made. |
| M006 browser parity screenshots/interaction manifest | frozen dashboard oracle/browser harness | not run | Oracle worktree and required environment paths were unavailable. See M006 closure. |
| Hosted CI | GitHub Actions | not run | All stated verification below is local. No hosted CI result is claimed. |

## 3. Production and qualification artifact manifest

- `artifacts/qualification/on-prem-user-access-m007-local.json` — sanitized local Q007 fake-provider evidence; SHA-256 `7942776b031523494afdc1c2f316a8bf99fedcae590b7f5358f0ea544cc25c04`.
- Candidate used by that run: `rust/target/debug/eggpool`; SHA-256 `479c02eac9b9300767e5199f164dc3855536b984b9a616cf0d638443a2691216`.
- Local environment in the artifact: macOS 26.6.2, x86_64 process, Rust 1.98.1, direct transport, loopback fake provider. Host memory was 24 GiB and the workspace volume reported 13 GiB available at inspection time. These facts describe the development host only.
- Physical SBC OS/RAM/disk/build hashes: **not measured**. No physical SBC artifact exists.
- Named-user/concurrency corpus beyond the separate two-user M006 correctness journey: **not run**.
- Performance output (RSS, CPU, Argon2 duration/memory contention, auth cache, SQLite wait/transaction, WAL/checkpoint deltas, TTFT, throughput, p50/p95/p99): **not measured**.

The saved JSON contains only bounded aggregate facts and synthetic fixture
labels. A review found no personal token, provider key, server key, password,
request body, or prompt in the artifact.

## 4. Verification executed

All commands below ran locally; hosted CI was not run.

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::backup -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
uv sync --frozen
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run pytest tests/tooling/test_qualification_sbc.py tests/tooling/test_qualification_stability.py tests/tooling/test_installer.py tests/tooling/test_service_transition.py -q --tb=short
uv run python scripts/qualification_live_provider.py --binary rust/target/debug/eggpool --offline-fake --output artifacts/qualification/on-prem-user-access-m007-local.json
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

### Results

- Rust workspace: 964 passed, 1 ignored across 78 serial suites.
- Focused Rust targets: server transport 14 passed; database compatibility 9; runtime lifecycle R009 6; coordinator finalization 11; backup module 1.
- Strict Clippy default/no-default and no-default workspace check passed; formatting check passed; locked debug build passed.
- Full tooling suite: 166 passed, 1 skipped. Focused SBC/stability/installer/service tests: 51 passed.
- Offline Q007 run: pass, 7/7 requests, 3/3 catalog models; 7 durable completions, no pending request, no active reservation; 4.832 seconds wall duration on the local host.
- Ruff: 59 files formatted, checks passed. Pyright: 0 errors, 0 warnings, 0 informations.
- Release documentation validator: pass, 7 docs checked. Runtime package-boundary validator: pass. `git diff --check`: pass.
- The full Rust suite passed before the final test-only member-role denial assertion; the focused M006 browser target passed again after that assertion. No-default tests were not run because CI only requires no-default check/Clippy and the full default serial suite passed.

## 5. Invariant review

- Qualification used isolated temporary config/database/runtime paths and a fake loopback provider; it used no real provider credential or contributor deployment.
- The Q007 synthetic admin was created with stdin-only password input. Its personal token was used only for inference/discovery and is redacted from diagnostic output.
- User identity and policy remain downstream of upstream provider accounts; provider credentials remain in environment/config ownership.
- Database ownership remains one connection/gate/worker with WAL/NORMAL; this closure added no runtime concurrency or storage architecture change.
- No hosted/SaaS, enterprise identity, 50-concurrent-generation, or 49-user capacity claim was introduced.

## 6. Failure, restart, and recovery review

The two-user M006 loopback covers first-login password change, admin/member
role denials, CSRF, token issue/revoke, model visibility and last-admin safety.
The synthetic provider run confirms all bounded cases terminalized without
leftover reservations. Database compatibility, lifecycle, finalization,
server transport, installer, and service-transition suites pass. A true
restore from a schema-54 backup into this build, power-loss test on SBC, and
paired cross-version backup rollback were not performed; they remain explicit
conditions rather than inferred guarantees.

## 7. Migration and compatibility review

Migrations are append-only through v60, checksum-locked, and runtime-manifest
registered. The v11 historical fixture exercises migrations through v60; a
schema-54 routing seed validates existing semantic state after migration. The
backup guide now identifies user, policy, budget, usage, and audit data in the
SQLite snapshot and states that downgrade requires restoring a matching
pre-upgrade backup. Exact schema-54 snapshot restoration remains unverified.

## 8. Security review

Synthetic qualifications used no real secrets. Browser-role/CSRF/token revocation
coverage is in the M006 two-user journey; member tokens are denied admin pages.
The local Q007 artifact was checked for token/key/password/body leakage. The
plan's larger selector-bypass, quota-last-slot, process-kill, database
corruption/read-only, and target-specific power-loss campaign has no new
physical result here; existing Rust policy, budget, finalization, and recovery
tests passed within the serial suite.

## 9. Documentation and operations

`docs/backup-restore.md` now identifies schema migrations 55–60, included
identity/policy/budget/usage/audit state, and forward-only rollback behavior.
The deployment, API, CLI, agent configuration, dashboard, integrations,
database, and security docs describe verified M006 behavior. Raspberry Pi and
release docs continue to make no user-count or concurrency claim. Browser
passwords on remote LAN access still require an operator-managed TLS proxy and
trusted-proxy configuration.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Medium | No physical Pi 5/Le Potato SBC or paired ordinary-Linux target was available. | M001 Argon2 latency/RSS and M007 resource/capacity operating points are unqualified; 49 named users and any concurrent completion target cannot be claimed. | Run the frozen corpus on explicitly provisioned hardware with OS/RAM/disk/build hashes, 1/5/10/25/49 named users, bounded concurrency, paired single-user baseline, and all required CPU/RSS/auth/SQLite/WAL/TTFT/percentile observations. |
| Medium | A restored schema-54 backup and cross-version identity/policy restore were not exercised. | Existing v11-to-v60 migration coverage and backup tests do not prove the exact schema-54 restore path. | Restore an actual protected schema-54 fixture into the target build; verify user IDs, credential revocation semantics, policy, budget holds/rollups, audit, and documented rollback to the matching old binary. |
| Medium | M005 exact output-token and unknown-price USD caps, global instance ceilings, crash/restore qualification, and overhead measurements remain open. | Some user limits are estimates and there is no instance-wide aggregate cap; M006 UI labels those limits as estimates. | Preserve the M005 conditions; create a separate corrective plan if exact enforceable limits are required. |
| Medium | M006 browser parity captures for newly added admin/member pages were not run. | Route/security evidence passes, but matched screenshot/interaction evidence is absent. | Run the pinned dashboard oracle/browser parity harness and record matched pages or intentional differences. |
| Medium | Hosted CI was not observed. | Local verification is comprehensive but no remote CI status is claimed. | Confirm branch CI after push; record its actual result separately from this local closure. |

## 11. Roadmap disposition

M007 is conditionally closed for the local code and synthetic qualification work.
The subsystem roadmap has no remaining implementation plan in this line and is
closed for current engineering work. Physical SBC capacity, exact schema-54
restore, dashboard parity, and inherited M005 limits remain conditions; they
do not qualify the 49-named-user target. Any later capacity claim or corrective
implementation needs an explicitly bounded follow-up with the required hardware
and evidence.

## 12. Registry updates

`plans/registry.md` and the source roadmap record M007 as conditionally closed,
link this closure, mark the subsystem roadmap closed, and remove M007 from the
active/ready/blocked tables. The M007 unblock audit found no registered
downstream plan to promote and no successor in this line. All M001–M007 plans
now have closure records; M001/M003/M005/M006/M007 retain their explicitly
named conditional evidence.
