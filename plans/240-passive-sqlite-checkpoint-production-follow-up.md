# Plan 240 — Passive SQLite Checkpoint Production Follow-up

Date: 2026-09-21
Status: proposed
Planning baseline: `c41d3533cd8398d9483be2f3cd8e0b3d6e0bd500`
Follows: `plans/239-sqlite-publication-commit-checkpoint-phase-diagnostic.md`
Evidence: `artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json`
Priority: P1 production design follow-up

## Purpose

Plan 239 localized the Raspberry Pi MMC tail to foreground SQLite automatic
checkpoint work. The default 1000-page runs produced multi-second publication
`COMMIT` tails. Disabling automatic checkpoints removed those tails and all
measured checkpoint-sequence changes. The 256-page comparison traded the large
publication pauses for more frequent smaller pauses, including finalization.

This plan is a design and implementation handoff only. It does not authorize
changing production SQLite behavior until the invariants and recovery behavior
below are reviewed.

## Required invariants

- Preserve one SQLite connection/gate and one DB worker unless new evidence
  proves that ownership boundary insufficient.
- Preserve WAL mode, `synchronous = NORMAL`, transaction atomicity, and the
  existing publication/finalization ownership model.
- Keep request admission and durable publication semantics unchanged.
- Bound WAL growth and define behavior across restart, reload, backup, restore,
  and recovery.
- Keep diagnostic-only fields and the qualification override out of ordinary
  release builds and production configuration.
- Do not replace a foreground checkpoint with an unbounded background task or
  make shutdown/recovery depend on an unbounded drain.

## Design questions

1. Identify the narrowest passive-checkpoint scheduling boundary that can run
   from the existing DB worker without adding a second writer.
2. Define a bounded cadence/threshold policy and the backpressure behavior
   when WAL growth or checkpoint work exceeds the available budget.
3. Specify how checkpoint progress is observed and how errors are surfaced
   without putting secrets or arbitrary request data in logs or persistence.
4. Add focused loopback evidence for publication and finalization latency,
   WAL bounds, backup/restore, restart, and cancellation paths.
5. Compare the candidate against the unchanged production policy before any
   default change is considered.

## Exit criteria

- A reviewed design explains ownership, failure, shutdown, and recovery.
- A focused implementation plan names the exact runtime files and tests.
- No production default or runtime code changes are made in this planning
  follow-up.
