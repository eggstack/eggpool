# Application Boundary Milestone 004 — Shared-process workload record

Status: implemented

Repository baseline: `e049025274a397d7799c3ae21ed106947c2a1572`

Source roadmap:

- `plans/subsystems/application-boundary-roadmap.md` M004

Long-term requirements:

- `plans/000-long-term-specification.md#5-performance-posture`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:

- `plans/adrs/ADR-0009-downstream-application-boundary.md` (accepted)

Primary class: polish

## 1. Objective

Record whether a mixed, content-free call pattern similar to EggUI plus
CodeGG was measured on the shared host. If that host is not the machine
running the closure, close with `not measured`. Authorize no runtime
change either way.

## 2. Why this milestone is ready

ADR-0009 says measurement is not a runtime change. The frozen
ordinary-server corpus already exists and must be left alone. Host
availability is operational: the closure disposition accounts for it,
so the plan is not blocked in the registry.

## 3. Current implementation evidence

- `plans/000-long-term-specification.md` keeps the single SQLite gate,
  current-thread runtime, and routing lock unless a reviewed measurement
  justifies a narrower change.
- `scripts/qualification_ordinary_server.py` is the frozen M002 matrix
  (`on-prem-ordinary-server-matrix.v1`). Its closure is
  `plans/closure/on-prem-concurrency-performance/002-status.md`.
- `scripts/qualification_sbc.py` refuses hosts that are not physical
  SBC class.
- EggUI deep research and CodeGG agent loops are many inference calls
  from two processes. This repository does not contain those workloads.

## 4. Invariants that must not regress

- No edit to production Rust, SQLite settings, Tokio runtime selection,
  or the routing lock.
- No edit to `scripts/qualification_ordinary_server.py`, its schema
  version, or its fixtures.
- No request or response bodies, prompts, or credentials in any
  artifact.
- A workstation run is not reported as the shared-host result.

## 5. Scope

### In scope

- A short measurement note in the closure record describing the call
  mix: sequential chat completions standing in for a coding loop, and a
  longer sequence standing in for a research run, both through one
  EggPool process, content-free, loopback only.
- Either aggregate scalars collected on the shared host with the
  existing status and runtime diagnostics, or the literal disposition
  `not measured`.
- An explicit statement that no optimization plan is opened.

### Explicitly out of scope

- A new qualification runner, a new Cargo feature, or a new runtime
  metric.
- Changing WAL, the gate, worker counts, or admission fairness.
- Driving the real EggUI or CodeGG binaries. Those stay in their own
  repositories. This record uses EggPool's own loopback inference
  against a fixture provider only when the shared host is actually
  available and the operator chooses to run it.
- Treating Pi/MMC or a laptop result as the shared-host number.

## 6. Required production changes

None.

## 7. Ordered work packages

### Work package A — Protocol

Intent: the closure names the workload and the non-authorization
before any command is run.

Required changes: write the closure draft's protocol section first:
two logical callers, personal tokens, Chat Completions only, no tool
execution, no bodies retained, stop after a predeclared request count.
Identify the host in the same terms the ordinary-server closure uses
(machine class, storage class) or write `not measured`.

Acceptance evidence: the protocol contains no threshold that could be
read as a new SLA.

### Work package B — Disposition

Intent: finish with evidence or with `not measured`.

Required changes: if the shared host is unavailable, the closure
status is `conditionally closed` only in the sense the roadmap allows
`not measured`, and the record's disposition line is `not measured`.
Do not run the frozen ordinary-server matrix again to fill the gap.
If the operator supplies a run, attach aggregate counts, latency
summaries, and process RSS only, and say they authorize nothing.

Acceptance evidence: the closure file exists and changes no file under
`rust/` or `scripts/qualification_ordinary_server.py`.

## 8. Failure, cancellation, restart, contention semantics

No production code runs because of this plan. A partial measurement is
discarded rather than published as a percentile. Cancellation is
stopping the optional manual run. No restart behavior changes.

## 9. Compatibility and migration

No config, schema, or API change.

## 10. Required tests

None added. Do not weaken existing tests.

## 11. Required verification commands

```bash
git diff --stat -- rust/ scripts/qualification_ordinary_server.py
git diff --check
```

Both must show no change from this milestone. Do not start the serial
workspace suite for a docs-and-closure-only result. If a command was
not run, the closure says so.

## 12. Documentation updates

The closure record is the documentation. Add one sentence to
`plans/subsystems/on-prem-concurrency-performance-roadmap.md` pointing
at this milestone so a later performance plan does not mistake it for
authorization. Do not edit `docs/deployment.md` performance claims.

## 13. Acceptance criteria

The registry can show M004 closed or conditionally closed with
`not measured`, and no runtime file differs. The record forbids opening
an optimization from this evidence alone.

## 14. Stop conditions

Stop immediately if the work seems to require a Rust change, a new
script, or an edit to the frozen corpus. That is a different plan.
Stop rather than publish laptop timings as shared-host evidence.

## 15. Closure evidence required

Host identity or `not measured`. Confirmation the runtime diff is
empty. The non-authorization sentence. No percentiles invented from an
unrun workload.

## 16. Handoff notes

This milestone is intentionally smaller than a feature plan. The
useful outcome on a developer machine that is not the shared host is
`not measured`. Do not borrow EggUI or CodeGG checkouts to manufacture
a number.
