# Rust Migration Implementation Plans

Implementation plans are bounded handoff artifacts tied to a specific EggPool repository baseline.

Layout:

```text
implementation/<subsystem>/NNN-short-title.md
```

Every plan must include objective, dependencies/readiness, Python oracle evidence, invariants, in/out scope, expected production changes, ordered work packages, failure/restart/contention semantics where relevant, compatibility/migration effects, required tests/commands, documentation updates, acceptance criteria, stop conditions, closure evidence, and handoff hazards.

Plans may adjust file-level mechanics after repository inspection but may not weaken the canonical migration specification or silently add supported differences.

A corrective pass receives a new plan and references the failed closure evidence.

The active cutover sequence is complete through K012. K011, K013, and K014
have accepted append-only recovery evidence, and K012 is accepted/closed in
`closure/cutover/012-status.md`. M11 is closed. M12 planning review is complete
under `subsystems/python-retirement-roadmap.md`; P001 is the sole
dependency-ready plan and does not authorize Python removal by itself.
