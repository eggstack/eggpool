# Q012 Closure — Dashboard State, Semantic Content, and Visual Requalification

Status: accepted; closed 2026-09-10

Plan: [Q012 — Dashboard State, Semantic Content, and Visual Requalification](../../implementation/qualification/012-dashboard-state-semantic-content-and-visual-requalification.md)

## Decision

Q012 is accepted. It closes the post-Q011 dashboard qualification gap and re-closes M10. No M11 cutover, installer switch, release publication, updater-authority change, or M12 retirement work was performed.

Implementation commits:

- `bcc96c8f0b93d862b0bc2075e603c12a94e7d811` — bounded Rust dashboard read plane, populated renderers, fixture-driven runner and focused tests.
- `b41a9ae07b086154a3efbcdeae663e991f022ce8` — deterministic fixture correction for canonical provider/startup-recovery state.

Machine evidence:

- [012-run.json](012-run.json), SHA-256 `e1c6d25cedae880594af467c5bc231a79642f2e222a6386ff94d9dffc0c5948c`, 27,778 bytes.
- [012-run.md](012-run.md), SHA-256 `e30e5cbf149bebf9aee1d580908c800c5cba72acd14c387bf227408c54f7b624`.
- Candidate SHA recorded by the report: `b41a9ae07b086154a3efbcdeae663e991f022ce8`.

## Fixture and state matrix

The fixture applies the existing canonical migrations and inserts data only; it does not add a qualification-only schema. It contains two providers (`q012-alpha`, `q012-beta`), three accounts, three meaningful models, five requests including an error, six request attempts, two account events, three provider pings, five routing decisions, five usage rollups, model price snapshots, and a finalized reservation. The fixture includes long/Unicode/HTML-sensitive values, a missing optional model-info case, and the escaped error detail `Quota & <retries>`. It contains no credentials, API keys, raw provider bodies, or private machine identifiers.

| Route | Empty | Populated | Error/missing | Private auth |
|---|---|---|---|---|
| `/` | exercised | exercised | empty-state cards/messages | 401 unauthorized / 200 authorized |
| `/accounts` | exercised | exercised | missing optional values | 401 / 200 |
| `/models` | exercised | exercised | missing optional values | 401 / 200 |
| `/models/{model_id}` | exercised | escaped model detail exercised | missing model-info detail exercised | 401 / 200 |
| `/latency` | exercised | exercised | empty-state path | 401 / 200 |
| `/events` | exercised | escaped event detail exercised | empty-state path | 401 / 200 |
| `/timeseries` | exercised | populated rollups exercised | empty-state path | 401 / 200 |
| `/bandwidth` | exercised | populated byte totals exercised | empty-state path | 401 / 200 |
| `/pings` | exercised | populated provider pings exercised | empty-state path | 401 / 200 |
| `/reliability` | exercised | retry/error aggregates exercised | empty-state path | 401 / 200 |
| `/routing` | exercised | routing decisions exercised | empty-state path | 401 / 200 |
| `/traces` | exercised | ordered traces and error status exercised | error request exercised | 401 / 200 |
| `/runtime` | exercised | runtime counters exercised | empty-state path | 401 / 200 |
| `/cache` | exercised | cache counters and guardrail exercised | empty-state path | 401 / 200 |

The public populated pair compared all 14 routes in both implementations. The private pair requested all 14 routes with dashboard authentication disabled for public access: every route returned 401 without the configured header and 200 with it. Static assets and the default plus Catppuccin Latte theme paths were also checked.

## Semantic qualification

The strengthened projection and comparator cover:

- document title, first heading, canonical internal navigation, active navigation, forms and period controls;
- stable IDs, duplicate IDs, internal links, static assets and unsafe external links;
- page status/empty/error messages;
- route-specific metric cards and displayed values;
- selected table headers plus ordered rows and projected cell text;
- model/provider/account availability and status text where displayed; and
- script/static hooks needed by the dashboard contract.

The comparator preserves semantic values, punctuation, escaping, table row order and cell order. Its only text normalization is collapsing HTML presentation whitespace while parsing DOM text; it does not sort, discard, or otherwise normalize meaningful content. It deliberately ignores only optional operational diagnostics explicitly outside the Q012 contract (`No operational events`, `No loss warnings`, and `No health state`).

Focused regressions prove that comparison fails for changed card values, missing/changed/reordered rows, changed status/error text, missing controls, changed escaping, and changed active navigation. The Q012 focused suite has 9 tests; the combined Q004/Q012 focused command has 12 passing tests.

## Rust parity corrections

The populated corpus exposed that Rust rendered only the overview with data while the other dashboard routes returned placeholder bodies. The narrow correction adds a bounded `DashboardRepository` over the canonical SQLite tables and data-backed SSR for the remaining dashboard pages. It also aligns the overview and page-specific empty states, availability/status rendering, retry/cache/runtime aggregates, ordered trace content, and private authentication across all dashboard HTML routes. No schema, runtime authority, provider wire, coordinator, deployment, or frontend redesign change was made.

## Visual evidence

The separate qualification-only browser capture command was run as:

```text
uv run python scripts/qualification_dashboard.py --skip-build --screenshots --screenshot-dir /tmp/eggpool-q012-captures-final-b41a9ae
```

It created 28 actual PNG files, not planned filenames: one populated capture for each of the 14 routes in Python at 1440×900 desktop/default theme and one in Rust at 390×844 mobile/Catppuccin Latte. The set includes the escaped Unicode model detail, dense model/reliability tables, timeseries chart hooks, navigation, cards, tables, and empty/populated semantic states in the machine evidence. The screenshot manifest SHA-256 is `7eebaf858f9c5eca06313c35eabd60a2fec50b156409434ba5f50520e86edd08`; every entry records its relative artifact, actual dimensions, byte hash, capture result, and manual disposition in `012-run.json`. The capture root is `/tmp/eggpool-q012-captures-final-b41a9ae` and remains outside git.

All 14 Rust mobile captures were visually inspected, with Python desktop captures cross-checked for representative overview, accounts, models, latency, events, timeseries, bandwidth, pings, reliability, routing, traces, runtime, cache, and model-detail rendering. Review disposition: pass for navigation, theme application, populated content, Unicode/escaping, model detail, chart hooks, and mobile layout. Dense tables retain the existing intentional horizontal-overflow behavior.

## Required evidence and gates

- Q001 frozen manifest test: 6 passed (`tests/migration_rs/test_q001_manifest.py`). The frozen manifest was not modified.
- Q002 aggregate rerun: 18 passed, 0 failed, 0 infrastructure errors, 0 skipped.
- Q003 database qualification rerun: 4 passed, 0 failed, 0 infrastructure errors.
- Q012 dashboard matrix: 14 routes × empty/populated semantic comparison passed, plus the all-route private 401/200 matrix and screenshot capture checks.
- Full Rust qualification: 445 passed across 52 test suites.
- Full migration suite: 170 passed, 3 skipped.
- Smoke suite: 14 passed.
- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`, Clippy with `-D warnings`, Ruff format/check, Pyright (0 errors/warnings/information), and `git diff --check` passed.

## Freshness, dependencies, and risk review

Q005 remains fresh: the Rust changes are portable SQLite/HTML read-plane logic with no target-specific, OS, deployment, or egress behavior. Q006 remains fresh because deployment sources and lifecycle tooling were untouched. Q007/Q011 remain fresh because provider, wire, and coordinator behavior was untouched. Q008 remains fresh because no SBC-specific path or resource contract changed; the added bounded dashboard queries introduce no unbounded process-owned state. Q009 remains fresh because the sustained workload/runtime lifecycle path was not changed. The accepted Q003 evidence was directly rerun, and the Q002/Q010-equivalent aggregate gates were rerun after the final implementation candidate.

No Cargo dependency or database migration was added. The browser/CDP capture code is qualification-only and outside the Rust production dependency graph. The report is bounded and secret-free; screenshots are external artifacts. No unresolved high/medium dashboard, compatibility, security, data-loss, lifecycle, target, provider, or resource finding remains.

## Registry transition and future plans

The active registry now records Q012 as accepted/closed, removes the dependency-ready implementation-plan entry, and records M10 as closed after the Q012 corrective pass. Historical Q004 and Q010 closure records remain unchanged and append-only; Q011 remains accepted. There is no future M11 implementation plan in `migration-rs/implementation/` to unblock or promote. M11 is now eligible for a separate planning review only, and M12 remains sequenced behind M11.
