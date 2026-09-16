---
name: plan
description: Plan lifecycle for the append-only plans/ maintenance record.
---

# Plan Maintenance

`plans/` is an append-only institutional record (~200 files), not a task
queue. Most entries are closed; Git history is the archive. Do not rewrite or
delete closed plans to reflect later work — add a new corrective/closure pass.

## Lifecycle

- Statuses used in-tree: `draft` → `ready for implementation` →
  `implementation handoff` → `complete`/`completed` → `closure`/`corrective-pass`.
  Keep the status line in the plan header so future agents can tell active
  from historical at a glance.
- Numbering is `NNN-slug.md` (zero-padded). Check `ls plans/` for the next free
  number; do not reuse numbers. The `146-*` duplicate pair is a known historical
  accident — do not replicate it.
- A closure pass references the plan(s) it closes by filename, states the
  evidence (commands + test targets run), and leaves the original untouched.

## Before writing a plan

- Read the matching skill (`architecture`, `development`, `deployment`,
  `documentation`) and the review index in `architecture/overview.md`.
- Cite authority paths (`rust/src/...`, `rust/tests/...`, `scripts/...`) with
  exact filenames. Streaming files are `coordinator.rs`, `execution.rs`,
  `terminal.rs`, `timeout.rs`, `types.rs`, `diagnostics.rs` — there is no
  `contract.rs` and no `coordinator_c012` test target.
- Keep credentials, prompts, raw bodies, and cache keys out of the plan.

## Checks

```bash
ls plans/ | tail -n 5
git diff --check
```
