# M12 Python Retirement Closure Records

Status: corrective requalification open after post-P006 hosted-CI failure; P007 is current corrective closure authority when accepted

This directory stores append-only closure evidence for M12 Python application/runtime retirement and final Rust-only migration closure.

Expected records:

- `001-status.md` — P001 final Python reference boundary and fixture freeze
- `002-status.md` — P002 Rust production package/catalog/cross-era authority
- `003-status.md` — P003 Python application source/runtime-asset retirement
- `004-status.md` — P004 oracle/differential/test/Python-tooling retirement
- `005-status.md` — P005 repository/installer/release/docs consolidation
- `006-status.md` — P006 Rust-only aggregate qualification and historical M12 closure evidence
- `007-status.md` — P007 provider-transport fixture determinism and hosted-CI M12 requalification

Machine-readable aggregate evidence for P006 is `006-run.json`.

Each record must include implementation commit(s), exact verification commands/results, removed/retained evidence, source-freshness decisions, unresolved findings, and the registry transition it authorizes.

Historical M11/M10 records are never rewritten to make retirement appear successful. A failed M12 gate receives a new corrective P-plan and closure record.

`006-status.md` remains append-only and records what the local P006 qualification proved. Because hosted CI subsequently failed the provider-transport identical-proxy account-isolation test, P006 is not the current final closure authority. Only an accepted `007-status.md` that records root-cause resolution and a successful hosted CI run on the corrected tree may restore M12 closure.
