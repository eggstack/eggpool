# Interim Planning Archive

Post-251 completed, superseded, rejected, or abandoned interim planning,
retained for traceability. Not an active work queue — use `plans/registry.md`
and current subsystem roadmaps to find executable work.

Pre-251 flat plans (`plans/001-*`…`plans/250-*`,
`python_hotpath_dispatch_compression_optimization.md`) are the legacy archive
and STAY at top level; they are not moved here (append-only history + link
stability).

## What belongs here

- completed post-251 milestone implementation plans after closure;
- superseded subsystem roadmaps;
- corrective plans whose closure is complete;
- rejected interim proposals worth retaining;
- status documents no longer needed in active directories.

## What does not belong here

- canonical long-term specification, terminology, roadmap, governance;
- accepted ADRs;
- active subsystem roadmaps;
- ready/active implementation plans;
- unresolved closure records.

## Archive layout

Preserve original category + subsystem:

```text
archive/
    subsystems/<subsystem>-roadmap.md
    implementation/<subsystem>/NNN-short-title.md
    closure/<subsystem>/NNN-status.md
```

When archiving: update inbound links from `plans/registry.md` and the
current roadmap; add a short archival note (final status + replacement);
prefer `git mv` to preserve history; never rewrite historical conclusions;
ensure active documents link to the replacement.
