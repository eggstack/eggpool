# M10 Qualification Closure Records

This directory stores append-only closure evidence for M10 migration-wide qualification, portability, live interoperability, SBC characterization, and release-readiness review.

Expected records:

- `001-status.md` — Q001 qualification contract/target/evidence freeze
- `002-status.md` — Q002 deterministic aggregate differential qualification
- `003-status.md` — Q003 DB upgrade/rollback/backup/recovery compatibility
- `004-status.md` — Q004 dashboard DOM/static/visual parity review
- `005-status.md` — Q005 supported-target build/runtime portability
- `006-status.md` — Q006 disposable rootful Linux operational acceptance
- `007-status.md` — Q007 bounded live-provider interoperability smoke
- `008-status.md` — Q008 ARM64 SBC functional/resource characterization
- `009-status.md` — Q009 sustained failure/reload/stream/resource qualification
- `010-status.md` — Q010 aggregate M10 closure/M11 readiness report

Every record must identify the exact candidate commit and relevant environment class. Environment-specific records must include sanitized OS/architecture/hardware metadata and exact commands. Live-provider records must contain no credentials or raw sensitive response bodies.

A later defect receives a new corrective Q011+ plan/closure record. Historical Q-records are never rewritten to hide a failed or superseded closure.

Only accepted `010-status.md` may close M10 and make M11 eligible for its own planning review.
