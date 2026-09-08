# M8 Runtime Lifecycle Closure Records

This directory stores append-only closure evidence for M8 runtime generations, rehash, background tasks, and process lifecycle.

Expected records:

- `001-status.md` — R001 runtime/reload oracle freeze
- `002-status.md` — R002 process runtime/generation factory/candidate ownership
- `003-status.md` — R003 active manager/publication/leases
- `004-status.md` — R004 retirement/finalization drain/resource close
- `005-status.md` — R005 config diff/reload policy/redaction
- `006-status.md` — R006 task supervisor/spec staging
- `007-status.md` — R007 transactional live rehash
- `008-status.md` — R008 generation-leased background/recovery integration
- `009-status.md` — R009 startup/signals/shutdown
- `010-status.md` — R010 active-generation authority/diagnostics
- `011-status.md` — R011 historical aggregate M8 qualification/initial closure
- `012-status.md` — R012 wire-negotiation runtime-authority and reload-diagnostics corrective re-closure

Closure records must include implementation commit(s), failing-before/passing-after evidence where applicable, exact verification commands, unresolved findings, and the registry transition they authorize.

The accepted R011 record remains append-only historical evidence. A post-close
audit found two unqualified M8 defects; R012 corrects them and re-closes M8.
Do not rewrite `011-status.md` to hide the earlier acceptance.

If another post-close defect is found, add a new corrective plan and closure record. Do not rewrite the historical closure that originally accepted the work.
