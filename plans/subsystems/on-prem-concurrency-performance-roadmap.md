# On-Prem Concurrency Performance Roadmap

Status: active

Long-term references:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Related ADRs:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)
- Existing persistence ADRs governing one SQLite worker and WAL/NORMAL
  (do not revise without explicit new architecture decision).

## 1. Purpose and ownership boundary

Improve the *measured* concurrent latency envelope of one local/SBC
EggPool instance without regressing the fast single-user path or
introducing SaaS/cluster complexity. This is a **separate non-release-
blocking diagnostic workstream**, opened because the original on-prem
M012 Pi 5/MMC qualification exceeded predeclared tail-latency gates.
It does **not** retroactively reopen the M012 closure or block
on-prem M016 final security/merge-readiness reconciliation. It may
find a new bounded optimization opportunity; no such implementation
is authorized until evidence isolates cause and a plan is accepted.

`scripts/qualification_sbc.py` and `scripts/qualification_stability.py`
own physical reproducible qualification. `rust/src/db/`,
`rust/src/coordinator/`, `rust/src/user_budget.rs`,
`rust/src/instance_budget.rs`, `rust/src/identity.rs` and
`rust/src/server/` own source evidence. Existing Persistence
roadmap and previously failed checkpoint experiments remain
the authority for storage policy; performance work here may not
move DB ownership or change WAL policy without a separate ADR.

## 2. Work classification

### Invariants

- Single-server worker, one SQLite gate/worker WAL/NORMAL and
  generation/finalization identity/fencing remain authoritative.
- Single-owner latency, requests, SSE/TTFT, recovery and model
  authorization must not regress to improve concurrency averages.
- No unsupported capacity marketing: 49 named account rows do not
  equal 49 simultaneous generations; measured Pi/MMC saturation
  and tails remain documented.
- No real provider/user keys or live DB in test data.

### Capabilities

- None currently added. This is root-cause attribution, not a
  concurrency/SLA guarantee.

### Infrastructure

- Bounded opt-in instrumentation of foreground DB gate, WAL and
  external block-device contention for controlled qualification
  only, with no steady-state hot-path allocation.

### Polish

- Precise tail-latency failure attribution and a supportable
  recommendation (environment, operator guide, or next bounded
  optimization/architecture plan).

## 3. Non-goals

No implicit second SQLite writer, checkpoint policy retune, async
persistence redesign, background worker migration, public WAN/TLS,
model/request-body rewrite, cluster capacity, web framework rewrite,
external DB/queue, forceful OOM testing on a shared Pi, or new
50-concurrent-request service claim.

## 4. Current state

The accepted `plans/closure/on-prem-user-access/012-status.md`
reports one Pi 5 8 GiB, ext4 on MMC, schema 62, 1 DB worker,
baseline RSS ≈20 MB and solo p50 ≈4–5 ms, but batch/concurrent
p95 10–14 s and 8-way p50 ≈4.1 s. G3 p95≤1 s,
max≤3 s was **failed**; no other severe correctness problem or
WAL convergence failure was reported. A loaded device had
background `opencode` CPU and `kblockd` / MMC disk pressure;
one transient D-state worker cleared. Earlier persistence M007/M008
Pi/MMC WAL experiments already demonstrated why checkpoint
changes based on synthetic averages are unsafe.

Potential causes (hypotheses, not findings): host/device I/O
contention, foreground serialized DB hold times, periodic
checkpoint or fsync, admission/budget query frequency,
login hashing, or external test workload. Physical evidence
must distinguish these before code is changed.

## 5. Target architecture

Keep current production layout. Use opt-in, low-overhead,
redacted phase and block-I/O instrumentation on a
**pre-provisioned isolated target** with paired baseline
and candidate, then reason quantitatively about p95/p99
contributor/causal share. Only if a specific EggPool-owned
cost is isolated and the existing persistence safety gates
can be preserved should a new bounded implementation plan
be proposed (or a new ADR if ownership/journal changes).
Otherwise update hardware/storage/workload guidance without
claiming a runtime optimization.

## 6. Dependency graph

- Diagnostic M001: hard — none; input M012 closure accepted.
- Operational for M001 full closure: physical Pi 5-class
  target, controlled MMC and optionally SSD/external drive,
  repeatable host activity and performance counters.
- Soft/interface: ongoing Persistence roadmap owns any
  architectural storage-policy experiment; a future M002
  must not be registered until M001 identifies measurable
  cause and independently accepted constraints.
- **No dependency edge** from this subsystem to on-prem
  M014/M015/M016 security or merge readiness. The current
  documented small-team envelope is limited but truthful.

## 7. Milestones

### M001 — Pi 5/MMC concurrent tail attribution

Class: polish. Ready for instrumentation/experiment design.
Collect bounded phase-correlated observation of high p95/max
and repeat same corpus under idle/background I/O, MMC/alternate
media, solo and 2/4/8-way loads. Exit: deterministic
reproduction or explicit unreproducible finding; workload/
device fingerprints, paired baselines, attribution hypothesis
with falsification evidence, resource cost and decision to
stop as environment-only or propose one safe targeted
optimization. Source:
`plans/implementation/on-prem-concurrency-performance/001-pi5-mmc-tail-latency-attribution.md`.

Future optimization milestone: **not registered**. Requires
separate scoped plan, architectural check and measurable
regression guard; not authorized by the diagnostic alone.

## 8. Cross-cutting requirements

- Same principal/model-policy and global/user budget scope,
  hard/estimated mode, published request/response wire,
  provider-account health and request-level idempotence.
- No production key exposure, no mutable tracing of
  request bodies, identity, tokens, upstream URLs or cookies.
- Instrumentation opt-in, bounded, removable and disabled
  by default; JSON artifacts redacted and independently
  validated.
- Record CPU/scheduler activity, disk queue and fsync
  contribution, SQLite gate wait/hold, WAL size/checkpoint,
  request p50/p95/p99/max, stream TTFT and memory.
- Frozen thresholds **before** interpreting new runs;
  preserve the original failed G3 result unedited.

## 9. Verification strategy

Use existing `scripts/qualification_sbc.py` with
on-prem personal-token bootstrap and Q007 managed-auth
compatibility, `tests/tooling/test_qualification_sbc.py`,
current M012 frozen artifacts, and exact target attestations.
Prefer 3 paired/repeated physical run groups with controlled
background conditions and bounded concurrency. Qualify any
added instrumentation with serial Rust default/no-default
and tooling tests; do not claim physical measurements from
a normal developer workstation.

## 10. Risks and decision points

- A shared development Pi with competing MMC writes
  can dominate high percentiles; uncontrolled performance
  changes risk false causal attribution.
- Earlier persistence checkpoint/split experiments were
  rejected under WAL convergence/tail gates. Avoid
  optimistic retuning that reopens those defects.
- Diagnostic-only shared-key topology modes still 401
  in managed context (M012 low finding); repair the
  narrow synthetic token path if needed to get comparable
  evidence without changing production auth.
- If evidence points to external block device rather than
  EggPool, recommend faster storage/guidance, not a
  speculative storage rewrite.

## 11. Completion definition

M001 accepted closure with reproducible physical artifacts
and an explicit causal/no-causal conclusion; scope can close
even when an environment-only tail remains, provided
the restricted Pi/MMC envelope continues to be documented.
No optimization promise or new 8-way SLA is inferred.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| M001 | ready | `plans/implementation/on-prem-concurrency-performance/001-pi5-mmc-tail-latency-attribution.md` | — | Hard: none. Operational: provisioned Pi/MMC target and paired workload for qualification. Nonblocking to on-prem M016. |
