# On-Prem Concurrency Performance M001 — Pi 5/MMC tail-latency attribution

Status: implemented

Repository baseline: `3c7def1e35871209e68563b2ee88f36033aff76e` (Pi 5 M012 closure and release 0.8.2 candidate).

Source roadmap:
- `plans/subsystems/on-prem-concurrency-performance-roadmap.md#m001--pi-5mmc-concurrent-tail-attribution`

Long-term requirements:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)
- Existing persisted WAL/single-worker architecture; a new accepted ADR is
  **required** before modifying journal/concurrency topology.

Primary class: polish

## 1. Objective

Determine why the existing Pi 5/MMC qualification reports seconds-scale
p95/max while solo p50 remains milliseconds, without changing any
production SQLite/storage/identity behavior until reproducible causal
evidence exists. This plan is **nonblocking to PR #7 merging** given
the existing restricted concurrency envelope and passing correctness.

## 2. Why ready

Hard: accepted M012 closure and frozen artifacts exist.
Interface: existing SBC/diagnostic instrumentation runners and
immutable one SQLite gate contract. Operational: a physical Pi 5
with its microSD/MMC boot storage is the required target.
Alternate storage is an optional diagnostic control, not a
qualification prerequisite. Local code/tooling instrumentation
can start now, but no metric may be invented without physical
target evidence.

## 3. Current evidence and why prior work missed attribution

M012 Pi 5 8 GB, Ubuntu 24.04, ext4/MMC, release schema 62:
solo p50 ~4–5 ms, streaming TTFT p95 ~4–19 ms,
4-way benchmark concurrent p50 ~13–15 ms with p95
10–14 s and maxima similar; 8-way p50 ~4.1 s.
Measured p95/max fail predeclared G3 gate (1/3 s).
RSS stays ~20 MB, WAL converges, 49 named users
provision but that is account count, not throughput.
Target had background `opencode` CPU, `kblockd` MMC
pressure and an observed transient D-state worker;
so neither workload contention nor an EggPool-specific
SQLite bottleneck has been isolated. `plans/closure/on-prem-user-access/012-status.md`
keeps a medium finding with restricted envelope.
Previous persistence M007/M008 Pi/MMC checkpoint experiments
failed their own tail/WAL gates, so a blind WAL reset is unsafe.

Relevant owners:
`scripts/qualification_sbc.py`,
`scripts/qualification_stability.py`,
`tests/tooling/test_qualification_sbc.py`,
`rust/src/db/connection.rs`,
`rust/src/coordinator/publication.rs`,
`rust/src/coordinator/finalization.rs`,
`rust/src/user_budget.rs`, `rust/src/instance_budget.rs`.
Historical baseline evidence lives in
`artifacts/qualification/on-prem-m012-pi5-*.json`
and persistence Pi artifacts.

## 4. Invariants that must not regress

- Single worker/gate, WAL/NORMAL, generation and retry/finalization,
  deterministic quota enforcement and profile gates remain unchanged.
- No extra services, cloud usage, producer DB connection,
  global thread explosion, authenticated API bypass,
  protocol body rewrite or dynamic checkpoint retune.
- Operator provider TOML/keys and downstream SQLite identity
  retain current ownership; no sensitive data in diagnostics.
- Preserve real observed poor tail results; no relabeling
  as pass and no fabricated Pi capacity statement.

## 5. Scope

### In scope

Controlled replay of the M012 fake-provider corpus
on Pi 5 microSD/MMC with 1,2,4,8 in-flight requests across
configured 1/5/49 named accounts, comparing (a) idle clean
device, (b) deliberate bounded competing disk I/O,
(c) microSD/MMC against tmpfs and, only if useful and available,
optional alternate storage,
(d) managed auth with/without hard user/global budgets,
(e) matched solo pre-multitenancy candidate control.
Correlate disk service/queue/major faults/scheduler,
SQLite gate wait vs transaction tenure, WAL bytes/
checkpoint/fsync, Argon2 login activity, latency and TTFT.
Do phase instrumentation only where needed and behind a
qualifier/test feature with zero production default overhead.

### Out of scope

No guaranteed optimizations, permanent extra tracing
service, SQL schema migration, adoption of PERSIST/EXTRA,
second writer/checkpointer, changing provider transport
or model client wire, retry algorithm rewrite, benchmark
using production provider keys, WAN load generator, or
unverified 50-concurrent operating claim.

## 6. Required production changes

**None by default**. Add bounded local qualification
instrumentation or opt-in counters only if the current
runner cannot isolate a phase. Prefer existing spans/SQLite
gate metrics and OS block I/O counters before writing Rust
hot-path code. If a production defect is actually isolated,
do not implement it inside this diagnostic: author the
next narrowly scoped corrective with regression/test
and architecture review (ADR if persistence topology changes).

For a diagnostic-only mode that now 401s due to shared
legacy key (M012 low finding), update only the disposable
qualification runner to use local `users bootstrap` personal
token for managed mode and verify secrets never enter JSON.
Do not alter server auth to make an old script pass.

## 7. Ordered work packages

A. Freeze source/hardware/control artifact, write hypotheses,
   exact target environment/competing I/O controls and
   predeclared p95/p99/MAX/WAL/RSS acceptance and abort gates.

B. Add or reuse minimal opt-in instrumentation for
   publish/finalize SQL gate queue/wait/hold vs provider/response;
   record zero-production-change baseline where possible.

C. Run paired/repeated target trials (prefer >=3 per
   condition) with bounded login and concurrency; record
   I/O queue/fsync, CPU/background process load, same fake
   provider/wire corpus and isolated data dirs.

D. Compare distribution shifts and falsify causes
   (e.g. remove competing microSD activity, move isolated
   DB to tmpfs or optional alternate storage, disable budget rules while keeping model
   metadata fixed). Publish cause confidence, operator
   mitigation and exact next-plan/no-code decision.

## 8. Failure, cancellation, restart and contention

Abort before memory/swap/OOM or sustained disk
saturation damages a shared SBC. No process kill
outside test-managed PID group. All burst runs
must settle pending/active user/instance holds
to zero, recover on restart, preserve WAL
quick-check and show no last-slot oversubscription.
Record test fixture's competing I/O details and
monitor WAL nonconvergence; stop immediately
on any correctness or encryption regression.

## 9. Compatibility and migration

No product DB migration, TOML change or API schema
change permitted. New output artifacts must be
versioned, redacted, self-describing and comparable
with original M012 artifacts. Preserve M012
correctness and existing constrained capacity docs.
If method changes make old gate percentiles
incomparable, label the difference rather than
claim performance gain.

## 10. Required tests

Extend `tests/tooling/test_qualification_sbc.py`
and add narrow `test_on_prem_mmc_tail_attribution.py`
if necessary; use `scripts/qualification_sbc.py --help`
to resolve **real** CLI flags. Validate 49 named
users versus 1/2/4/8 active, secret redaction,
target attestation, WAL and pending/active convergence,
bounded output, abort controls and paired candidate
identity. If Rust diagnostic spans are added,
write opt-in zero-runtime-default overhead tests.
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
uv run pytest tests/tooling/test_qualification_sbc.py -q
```

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv sync --frozen
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```
Any additional script/feature change requires its
corresponding existing targeted tooling/build gate.
Physical data on a real Pi/MMC is mandatory for closure;
report exact commands and actual results only.

## 12. Documentation updates

Update `docs/raspberry-pi.md` only when a
**reproducible** environment mitigation or
resource requirement changes practical operator
guidance. Link qualified artifacts under
`artifacts/qualification/`; do not retroactively
edit M012 accepted closure evidence.

## 13. Acceptance criteria

Each candidate tail source has a controlled
falsification attempt and measured contribution.
Reproduction and negative/control runs include
physical device attestations, repeatable work
and the actual concurrency, with p50/p95/p99/max,
gate hold/wait, WAL/checkpoint/IO telemetry and
zero leaked authentication material. Emit a
supported cause/uncertain/no-code conclusion and
either an operator mitigation or a precise new
bounded corrective proposal. No current PR #7
release gate depends on this work.

## 14. Stop conditions

Absent provisioned target or clean control,
nonconverging WAL, orphan budget holds,
unbounded I/O pressure, raw secret artifacts,
nonreproducible claimed improvement, or proposal
to change journal/multiwriter topology without ADR.
Mark the diagnostic blocked/conditional and
preserve the documented Pi envelope rather
than declaring a fix.

## 15. Closure evidence required

`plans/closure/on-prem-concurrency-performance/001-status.md`
must list hardware/media/OS/build, frozen thresholds,
hypotheses and attempted falsifications, paired
3x raw artifacts, variance and confounders,
service wait/IO/WAL latency attribution, serial
tests and hosted result (if source changes),
specific operator/next-plan decision and registry
unblock audit.

## 16. Handoff notes

Separate parallel/nonblocking workstream.
No claim of fixed concurrency performance before
a later measured implementation closure.
