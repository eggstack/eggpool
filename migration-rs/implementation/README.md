# Rust Migration Implementation Plans

Implementation plans are bounded handoff artifacts tied to a specific EggPool repository baseline.

Layout:

```text
implementation/<subsystem>/NNN-short-title.md
```

Every plan must include objective, dependencies/readiness, Python oracle evidence, invariants, in/out scope, expected production changes, ordered work packages, failure/restart/contention semantics where relevant, compatibility/migration effects, required tests/commands, documentation updates, acceptance criteria, stop conditions, closure evidence, and handoff hazards.

Plans may adjust file-level mechanics after repository inspection but may not weaken the canonical migration specification or silently add supported differences.

A corrective pass receives a new plan and references the failed closure evidence.

The active cutover sequence is K011 -> K012. K011 is currently blocked because
the production PyPI Trusted Publisher is not configured. K013 corrected the
recovery workflow but stopped at that external publisher exchange; K014 owns
the maintainer-side configuration and exact-bundle recovery. K012 must not be
unblocked until K011 has an accepted closure record.
