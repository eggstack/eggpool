# C004 Closure — Provider-Bound Attempt Construction and Upstream Submission

Status: closed

Recommendation: closed; C005 is complete and C007 remains gated by C006.

Implementation commit: [`97a4846`](https://github.com/eggstack/eggpool/commit/97a48464b775514f90d36d021607c091881a36d3)

Plan: [C004 — provider-bound attempt construction and upstream submission](../../implementation/coordinator/004-provider-attempt-construction-and-submission.md)

Repository baseline: `9b730c59`

## Outcome

C004 adds `AttemptBuilder` and `PreparedUpstreamAttempt`. One selected M6
profile is prepared into one encoded body, safe provider-relative path, static
and auth headers, stream intent, and durable identity. Submission goes through
the existing M4 `ProviderClientPool` and `ProviderHttpClient`; the builder has
no retry loop and does not create a second HTTP stack.

## Requirement-to-evidence matrix

| C004 requirement | Evidence | Result |
|---|---|---|
| One immutable prepared attempt | `attempt.rs::PreparedUpstreamAttempt` | Pass |
| M6 source-of-truth request preparation | `AttemptBuilder::prepare` calls `WireRuntime::prepare_request` | Pass |
| Provider path/model expansion | Safe `{model}`/`{model_id}` expansion and stream-path selection; focused test | Pass |
| Auth/static header precedence | Explicit auth and static header construction; missing credentials fail before send | Pass |
| M4 account client selection | `submit_once` calls `ProviderClientPool::get_client` then one `send` | Pass |
| Secret redaction | Custom `Debug` reports header names and byte counts only; focused test | Pass |
| Exactly one send per invocation | `submit_once` has one transport call and no replay path | Pass |

## Compatibility evidence

The implementation preserves the M4/M6 ownership split and uses the canonical
schema identity carried by C002. The focused test verifies path, stream path,
authentication, body preparation, and redacted diagnostics without making a
live provider request. Public response handoff remains intentionally deferred
to C007.

## Verification commands actually run

```text
rtk cargo fmt --all
rtk cargo test --test coordinator_boundaries -- --nocapture  # 3 passed
rtk cargo clippy --all-targets -- -D warnings                 # clean
rtk cargo test --all-targets                                  # passed
rtk git diff --check                                           # passed
```

No second TLS stack, schema migration, credential, or external network
behavior was added.

## Future-plan audit and registry transition

C004 is removed from the active queue and recorded as completed. Its accepted
boundary unblocks C005, which is closed by the same implementation/evidence
set. C006 remains the hard gate for C007; C008-C011 and M8 remain blocked by
their documented serial predecessors.

Unresolved mandatory findings: none.
