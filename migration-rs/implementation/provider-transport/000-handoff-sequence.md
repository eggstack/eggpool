# M4 Provider Transport Handoff Sequence

Status: corrective pass active

Historical sequence:

1. T001 — contract and fixture freeze (closed);
2. T002 — direct Hyper/Rustls provider HTTP core (closed);
3. T003 — Eggress connector and proxy parity (historically closed);
4. T004 — provider/account client pool and lifecycle boundary (closed);
5. T005 — differential qualification and initial M4 closure (historically closed).

Post-T005 independent review identified one bounded acceptance-evidence gap: mandatory Shadowsocks/SSR/Trojan/SSH corpus rows have construction evidence but no deterministic runtime peer evidence, despite the T001/T003 requirement for runtime qualification or an approved supported-difference decision.

6. T006 — [extended proxy runtime interoperability closure](006-extended-proxy-runtime-qualification.md) (ready for handoff).

T006 does not reopen the direct HTTP/TLS, common CONNECT/SOCKS, provider-pool, timeout, cancellation, or dependency work unless its runtime tests expose a regression. It exists to complete mandatory extended proxy interoperability evidence and re-run the T005 closure matrix.

M5 implementation planning remains blocked until T006 has an accepted closure record. If T006 discovers an Eggress defect, correct/qualify Eggress or create an explicit ADR supported-difference decision; do not weaken the frozen EggPool contract silently.
