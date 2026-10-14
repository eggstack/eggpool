# On-Prem User Access Milestone 012 — Closure Status

Status: blocked

Source implementation plan:

- `plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m012--physical-sbc-small-team-qualification-and-final-disposition`

Repository baseline reviewed: `b1f6d20b`

Implementation commits or pull requests:

- `89012fe7` — Activate M012 at `10eeee25` (schema 62, hard M008–M011 closures accepted)
- `b1f6d20b` — M012 SBC runner boundary contracts (`tests/tooling/test_on_prem_sbc_qualification.py`, 9 tests)

## 1. Executive finding

M012 cannot close: no physical Le Potato/Pi-class target and no
ordinary Linux reference host were provisioned in this session, and
the plan explicitly forbids substituting macOS synthetic results or
hosted CI for physical evidence. The only work package executable
without hardware — the tooling contract foundation — landed and is
green. The runner's own exact-target gate was exercised on this
Darwin/x86_64 host and reports `blocked` with the physical-SBC
reason, proving the fail-closed boundary works. No capacity,
latency, Argon2, RSS, WAL, or named-user claim is made anywhere in
this record.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A — Freeze actual target and work corpus | `qualification_sbc.py --binary /not/a/candidate` on Darwin/x86_64 | blocked | Reports `blocked`: "SBC qualification requires Linux on a physical aarch64 SBC". No target frozen, no corpus run. |
| A — Loopback-only, secret-free fixtures | `test_m012_sbc_fixtures_carry_no_user_identity_matrix`, existing `test_q008_*_secret_free` | pass | Both SBC fixtures use `__SBC_UPSTREAM__` placeholders and test-only keys; no `https://`, no user material. |
| B — 1/5/10/25/49 named users × concurrency 1/2/4/8 | Repository inspection + `test_m012_no_named_user_or_concurrency_matrix_flags`, `test_m012_runner_records_actual_concurrency_not_named_users` | blocked | No matrix harness exists; runner has a fixed 4-client/32-request contention observation only. Absence is pinned, not implemented. |
| B — Actual concurrency/completion totals | `CONCURRENCY_WORKERS == 4`, `CONCURRENCY_BATCH_SIZE == 32`; blocked report carries no named-user/capacity fields | pass (contract) | Reports record actual counts when measured; nothing measured here. |
| C — Physical Argon2id 1-and-2-way under idle/load | — | not run | No hardware; 19 MiB/two-worker sizing from M001/M007 remains unmeasured. |
| C — RSS/CPU/gate/WAL/TTFT posture + solo deltas | — | not run | No hardware; binary-size/RSS/gate-wait observations do not exist. |
| D — Predeclared pass/fail gates | — | blocked | No thresholds fabricated; none could be grounded without baseline hardware evidence. |
| D — Benchmark corpus/fixture binding | `test_m012_benchmark_corpus_requires_benchmark_fixture` | pass (contract) | Corpus refuses to run against the ordinary fixture. |
| D — Baseline/candidate pairing discipline | `test_m012_control_candidate_modes_are_explicit_and_exclusive` | pass (contract) | Control/candidate modes explicit, mutually exclusive, phase-diagnostic-bound. |
| E — M008–M011 closure cross-reference | Closure records `008/009/010/011/013-status.md` | pass | Hard contracts (token/cost admission, global intersection, crash recovery, browser qualification, transcode eligibility) accepted on local evidence. |
| E — Focused regression targets | `on_prem_budgets` 27, `on_prem_identity` 3, `server_transport` 14, `runtime_lifecycle_r009` 6 | pass | macOS local; explicitly not physical evidence. |
| E — Whole-workspace hosted CI | Branch CI at pre-closure baseline | pass (prior) | `check` 7m51s pass on rerun; installer shells pass; `cargo-deny` fails on pre-existing rustls advisories (separate triage). Current-head CI pending push. |
| Work package 1 — tooling harness + fixture validations | `tests/tooling/test_on_prem_sbc_qualification.py` 9 passed; `test_qualification_sbc.py` 41 passed | pass | Manifest, redaction, target binding, concurrency-vs-users, pairing contracts green. |

## 3. Production implementation evidence

Zero production diff. No runtime, migration, config, fixture, or
documentation change: `git status` shows only the activation edit,
the new tooling test, and this closure pass. Implemented
(planned-but-absent distinguished): the boundary-contract suite.
Planned but absent: the named-user matrix harness, Argon2/resource
measurement, predeclared gates, and every physical artifact in plan
§15 (fingerprints, target/control corpora, percentiles, WAL and
recovery evidence, eligibility checklist).

## 4. Verification executed

### Commands run

```bash
uv run pytest tests/tooling/test_on_prem_sbc_qualification.py -q
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
uv run pytest tests/tooling/test_qualification_sbc.py tests/tooling/test_on_prem_sbc_qualification.py -q
uv run python scripts/qualification_sbc.py --binary /not/a/candidate --output /tmp/m012-blocked-proof.json
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

### Results

Host: macOS on Apple Silicon (x86_64 execution), development
machine — local contract/synthetic evidence only, never physical.

- New M012 contracts: 9 passed.
- Focused Rust: 27 + 3 + 14 + 6 passed, 0 failed.
- SBC suites combined: 50 passed.
- Physical gate proof: `status: blocked`, exit 1, reason names Linux/aarch64 SBC.
- Full default workspace: 1008 passed, 0 failed (exit 0; identical to post-M011 baseline).
- Full no-default workspace: 1009 passed, 0 failed (exit 0).
- Tooling suite: 185 passed, 1 skipped (176 pre-existing + 9 new).
- ruff format (62 files), ruff check, pyright (0 errors), `git diff --check`: clean.
- No concealed partial execution; physical scope explicitly not run.

## 5. Invariant review

Per plan §4 (single admin/SBC instance, SQLite owns user state, one
gate/worker WAL/NORMAL, no SaaS/tenancy/billing, provider keys stay
in TOML/environment): all hold trivially — zero production diff, no
migration, schema stays 62, no second writer/connection introduced,
no upstream key moved. The new tests assert fixtures carry no user
tables/keys and reports retain only bounded scalars.

## 6. Failure and recovery review

Off-target execution fails closed before measurement (proven above:
no `candidate` key, exit 1). Cancellation, forced-shutdown/restart,
WAL growth/convergence, revocation-during-flight, and
memory-pressure abort behavior on target hardware are unmeasured —
carried as the hardware blocker, not asserted. Local crash-recovery
semantics remain those accepted by the M010 closure; nothing here
re-opens them.

## 7. Migration and compatibility review

No migration (schema 62 unchanged), no config-surface change, no
protocol change. Single-user TOML/provider/agent setup, v54-restore
path (M010), backup permissions, discovery, readiness probes, and
`EGGPOOL_API_KEY`/personal-token compatibility are untouched and
unregressed per the full local suites. No rollback involved.

## 8. Security review

Secret-free: new tests use only the runner's public test-only
fixture keys and assert blocked reports contain no credentials,
bodies, URLs, or paths beyond `{system, architecture}`. Fixture
placeholders verified loopback-only. No password/Argon2/token/audit
material added. No new network, privilege, or DoS surface (tooling
tests only, bounded local loopback provider already owned by the
existing suite).

## 9. Documentation and operations

No documentation changed — deliberately: plan §12 updates require
qualified capacity/requirements that do not exist, and publishing
unmeasured SBC guidance would violate the plan's stop conditions.
`docs/raspberry-pi.md`, `docs/deployment.md`,
`docs/agent-configuration.md`, `docs/backup-restore.md`,
`architecture/deep-dive-database.md`, and `README.md` are untouched.
No operator action is authorized by this record.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| high | No provisioned physical SBC or ordinary Linux reference host | Milestone cannot close; no named-user, concurrency, Argon2, RSS, WAL, TTFT, or capacity claim exists | Provision Le Potato/Pi-class target + reference host, then execute work packages 2–4 with predeclared gates |
| medium | Named-user/client/profile/quota matrix harness does not exist | Even with hardware, the 1/5/10/25/49 × 1/2/4/8 matrix cannot run today | Design the bounded matrix harness against provisioned hardware; extend the §10 test file to drive it |
| medium | Predeclared pass/fail gates never chosen | No acceptance baseline for a future hardware run | Choose gates from first-target baseline evidence before candidate measurement, per plan §D |
| low | Installer harness exceeds its 180s budget on loaded macOS hosts | Tooling-suite timing only; harness passes 78/78 directly | Deployment-plan scope (carried from M011), not M012 |

## 11. Roadmap disposition

Blocked + reason: operational physical-SBC and reference-Linux-host
evidence missing. The on-prem user-access roadmap stays `active`
with this blocker explicit. M001/M003/M005/M006/M007 conditional
closures are unchanged; M008/M009/M010/M011/M013 closures stand.
When hardware is provisioned, resume at plan work packages 2–4 —
this record plus the landed contract suite is the handoff, and no
re-qualification of the tooling foundation is needed.

## 12. Registry updates

Changes required in `plans/registry.md` + source roadmap (applied in the same commit).

- `plans/registry.md`: blocked-work M012 row notes the landed
  contract suite and unchanged hardware blocker; unblock-audit
  paragraph records this blocked closure.
- `plans/subsystems/on-prem-user-access-roadmap.md`: M012 row stays
  `blocked`; blocker cites this closure record.
- `plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md`:
  `active` → `blocked` (foundation landed, physical evidence pending).
