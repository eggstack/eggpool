# M4 Provider Transport Handoff Sequence

Status: closed after corrective pass

Historical sequence:

1. T001 — contract and fixture freeze (closed);
2. T002 — direct Hyper/Rustls provider HTTP core (closed);
3. T003 — Eggress connector and proxy parity (historically closed);
4. T004 — provider/account client pool and lifecycle boundary (closed);
5. T005 — differential qualification and initial M4 closure (historically closed).

Post-T005 independent review identified one bounded acceptance-evidence gap: mandatory Shadowsocks/SSR/Trojan/SSH corpus rows have construction evidence but no deterministic runtime peer evidence, despite the T001/T003 requirement for runtime qualification or an approved supported-difference decision.

6. T006 — [extended proxy runtime interoperability closure](006-extended-proxy-runtime-qualification.md) (closed; [closure record](../../closure/provider-transport/006-status.md)).

T006 does not reopen the direct HTTP/TLS, common CONNECT/SOCKS, provider-pool, timeout, cancellation, or dependency work unless its runtime tests expose a regression. It exists to complete mandatory extended proxy interoperability evidence and re-run the T005 closure matrix.

M5 implementation planning and handoff work is unblocked by the accepted T006
closure record. M6-M12 remain sequenced behind their independent hard
dependencies. The T006 implementation corrected Eggress's missing SSH session
cache at the adapter boundary without weakening the frozen contract.
