# Application Boundary Milestone 004 — Closure Status

Status: conditionally closed

Disposition: `not measured`

Source implementation plan:

- `plans/implementation/application-boundary/004-shared-process-workload-record.md`

Source subsystem roadmap:

- `plans/subsystems/application-boundary-roadmap.md#m004--shared-process-workload-record`

Repository baseline reviewed: `e049025274a397d7799c3ae21ed106947c2a1572`

Implementation commits:

- Uncommitted working tree on that baseline. This milestone adds no
  production commit. The closure file and one roadmap sentence are the
  milestone.

## 1. Executive finding

The shared host was not the machine used for this closure. The disposition
is `not measured`. No optimization plan is opened. No percentile is
published. The ordinary-server corpus stays frozen.

## 2. Protocol

The unrun workload, recorded so a later plan cannot invent a threshold
from this milestone:

- One EggPool process.
- Two logical callers, each with a personal token.
- Chat Completions only.
- One sequence stands in for a coding loop. A longer sequence stands in
  for a research run.
- No tool execution.
- No request or response bodies retained.
- Loopback only.
- Stop after a predeclared request count.

This protocol has no latency, throughput, or RSS threshold and is not an
SLA. It was not executed.

## 3. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Host identified or `not measured` | This record | pass | Disposition `not measured`. |
| Shared host is the Ubuntu 24.04.5 NVMe host from the ordinary-server closure | Not this machine | not measured | This closure was written on a developer workstation (`darwin`). That host is not the shared host. |
| No laptop timings published | This record contains no latency, RSS, or percentile figures | pass | A partial measurement was not taken. |
| No edit to the frozen qualification script | `git diff --stat -- scripts/qualification_ordinary_server.py` is empty | pass | The matrix was not rerun. |
| No production change from this milestone | M004's file set is this record and one sentence in the concurrency roadmap | pass | `git diff --stat -- rust/` is nonempty because M001–M003 edited Rust in the same working tree. Those edits are not M004 evidence and do not authorize a concurrency change. |
| Non-authorization sentence | `plans/subsystems/on-prem-concurrency-performance-roadmap.md` points at this record | pass | |

## 4. Production implementation evidence

None. No Rust file, SQLite setting, Tokio runtime selection, routing lock,
qualification script, schema version, or fixture was changed for this
milestone.

## 5. Verification executed

### Commands run

```bash
git diff --stat -- scripts/qualification_ordinary_server.py
git diff --check
```

The serial workspace suite was not run for this milestone. It ran for
M001–M003 in the same working tree. This record does not use that suite
as measurement evidence.

### Results

- `git diff --stat -- scripts/qualification_ordinary_server.py`: empty, exit 0.
- `git diff --check`: exit 0.
- `git diff --stat -- rust/`: nonempty. The diff belongs to M001–M003. M004 adds no path under `rust/`.

## 6. Invariant review

The single SQLite gate, `current_thread` runtime, and routing lock are
unchanged by this milestone. No request body, prompt, or credential is in
this record.

## 7. Failure and recovery review

No production code runs because of this plan. There is no partial
measurement to discard.

## 8. Migration and compatibility review

No config, schema, or API change.

## 9. Security review

No artifact contains prompts or credentials. No new metric was added.

## 10. Documentation and operations

This record is the documentation. One sentence in
`plans/subsystems/on-prem-concurrency-performance-roadmap.md` points here
and says the record does not authorize a change.
`docs/deployment.md` performance claims were not edited for this milestone.

## 11. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Shared-host mixed workload was not measured | No capacity number exists from this milestone | None. `not measured` is the allowed terminal disposition. A future measurement needs its own plan and must not treat this record as authorization. |

## 12. Roadmap disposition

Conditionally closed only in the sense the roadmap allows `not measured`.
The disposition line is `not measured`. The application-boundary completion
definition accepts that disposition. No optimization plan is opened.

## 13. Registry updates

Application-boundary M004 leaves the dependency-ready table with
disposition `not measured`. The concurrency roadmap sentence points at
this record. Nothing is promoted.
