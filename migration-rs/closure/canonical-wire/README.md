# M6 Canonical Wire Closure Records

This directory stores accepted closure evidence for W001-W010 and any later bounded corrective plans in the M6 canonical request/wire workstream.

Each closure record must name implementation commit(s), verification commands actually run, Python/Rust differential evidence, resource/security/dependency findings, unresolved issues or supported differences, and the exact registry transition it authorizes.

Historical closure records are append-only. Post-W010 review found two material gaps: Rust EOF handling can silently drop an incomplete UTF-8 suffix instead of matching Python replacement semantics, and W010's integrated cross-surface request/finite/stream assertions do not prove the full Python-derived semantics claimed by its closure. W010 therefore remains historical evidence while aggregate M6 is reopened for W011/W012.

W011 closure must record failing-before/passing-after invalid/truncated UTF-8 EOF fixtures and may promote W012 only. W012 closure must record the full Python-derived 15-pair request, finite-response, and stream/client requalification matrix and may re-close aggregate M6 only if no mandatory M6 finding remains.

M6 closure records must not claim provider dispatch/retry/finalization parity; those are M7 responsibilities.