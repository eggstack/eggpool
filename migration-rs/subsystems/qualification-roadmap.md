# M10 Full Qualification, Portability, and SBC Characterization Roadmap

Status: corrective pass active; Q012 dependency-ready; M11 blocked

Repository baseline for corrective planning: `e7d226aedc23740a3fe7c25210923d1703fca6c9` (historical Q011/Q010 M10 closure before post-close dashboard audit).

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, `../003-planning-process.md`, ADR-0001 through ADR-0003, closed M4-M9 roadmaps/closures, Q001 manifest, and append-only Q001-Q011 qualification records.

## Purpose

M10 is the final migration-wide qualification milestone before Rust cutover planning. It does not create another runtime subsystem. It proves that the completed Rust implementation behaves compatibly across deterministic client/operator/database/dashboard surfaces and representative deployment environments, especially Linux ARM64 SBCs.

Python remains the behavioral oracle through M10. Rust remains side-by-side and cannot become the canonical public install/release/update authority until M11.

## Current evidence state

Accepted evidence before the Q012 corrective pass includes:

- Q001 frozen manifest/target/evidence schema;
- Q002 deterministic migration-wide aggregate;
- Q003 bidirectional Python/Rust DB upgrade/rollback and backup/recovery compatibility;
- Q005 supported Linux x86_64/Linux aarch64 and macOS arm64-development portability;
- Q006 real disposable rootful Ubuntu/systemd/cron/logrotate acceptance;
- Q011 bounded seven-cell live-provider qualification correcting Q007's historical blocked attempt;
- Q008 physical Raspberry Pi 5 Ubuntu/aarch64 functional/resource evidence;
- Q009 bounded sustained failure/reload/stream/restart/resource convergence.

Q004 and Q010 are retained as historical accepted evidence, but post-close audit found the dashboard qualification incomplete. Q012 is now the current M10 closure authority.

## M10 invariants

1. Qualification cannot redefine behavior; mismatches are findings.
2. Python remains the oracle until M11.
3. Normalization is narrow: ephemeral IDs/timestamps/ports/temp roots only where already approved; semantic content, ordering, status, terminal state and durable effects are not normalized away.
4. Live-provider work is opt-in, bounded and secret-free.
5. Normal CI stays lean; rootful/live/physical/browser-heavy work remains explicit qualification.
6. Representative supported targets are proven; unsupported/unqualified targets are named explicitly.
7. Real Linux ARM64 SBC evidence is mandatory.
8. Rootful operations run only on disposable Linux systems.
9. Resource measurements are characterization unless they reveal correctness defects such as leaks, unbounded growth, crash, deadlock, replay or clearly impractical operation.
10. Python-created and Rust-mutated databases remain mutually readable where rollback is claimed.
11. Backup/recover fault qualification must protect the last good copy.
12. Dashboard qualification preserves the product design; it does not re-theme or redesign the UI.
13. Mandatory dashboard state evidence must execute real empty, populated, unauthorized and error/missing states.
14. Dashboard semantic comparison must include displayed values/rows/ordering, not only page shell/navigation.
15. Planned screenshot filenames are not screenshot evidence; actual captures must exist and be reviewed for the declared visual matrix.
16. Live-provider tests do not manufacture destructive failures.
17. M10 does not publish canonical Rust release assets, switch installers/update authority, or retire Python.
18. High/medium correctness, security, data-loss, compatibility, lifecycle, resource, target, provider or dashboard findings block M10 closure.
19. Evidence is reproducible, bounded and secret-free.
20. Historical closure records remain append-only; later findings use new Q-plans.

## Sequence and corrective history

```text
M9 O010 accepted
  |
  v
Q001 qualification contract/target/evidence freeze
 -> Q002 deterministic migration-wide differential runner
 -> Q003 DB upgrade/rollback/backup/recovery
 -> Q004 dashboard DOM/static/visual review (historical closure; later gap found)
 -> Q005 supported-target portability
 -> Q006 rootful Linux operational acceptance
 -> Q007 live-provider attempt (historical blocked)
 -> Q008 physical ARM64 SBC evidence (initially dependency-blocked)
 -> Q009 sustained stability evidence (initially dependency-blocked)
 -> Q010 aggregate closure attempt/re-acceptance (historical)
 -> Q011 corrective live-provider closure; Q008-Q010 re-accepted
 -> Q012 populated/error dashboard semantic + actual visual requalification
  |
  v
M11 planning eligibility only after accepted Q012 closure
```

Only `../registry.md` authorizes implementation. Q012 is the sole dependency-ready M10 plan.

## Q001 — Qualification contract and target/evidence freeze

Accepted. The versioned `m10-q001.v1` manifest remains frozen and defines mandatory cells, including `q001.dashboard.states` for representative empty, populated, unauthorized and error states. Q012 must satisfy that contract rather than modifying it.

## Q002 — Migration-wide deterministic differential qualification

Accepted. Reuse the aggregate runner and existing focused suites. Q012 reruns it after any dashboard correction to catch migration-wide regressions.

## Q003 — DB upgrade/rollback/backup/recovery compatibility

Accepted. Python→Rust→Python migration/read/write and cross-implementation backup/recovery evidence is valid. Q012 may reuse Q003 fixture/schema helpers to build deterministic dashboard seeds but must not create a qualification-only schema.

## Q004 — Dashboard SSR/DOM/static/visual review

Historical accepted closure, superseded for the specific post-close findings below.

The implemented Q004 runner qualified 14 routes, static assets/themes, escaping, navigation and private auth against fresh empty databases. However:

- populated and multi-provider states were only labeled as reserved fixture shapes;
- the semantic projector captured text but did not compare meaningful displayed data/table rows;
- the deliberate mismatch regression tested navigation rather than data-content drift;
- the screenshot cross-product was metadata, while only three actual captures were documented as manually reviewed.

Those gaps conflict with Q004's own plan and the mandatory Q001 dashboard-state cell. Q012 corrects and requalifies this boundary without rewriting Q004 history.

## Q005 — Supported-target portability

Accepted. Linux x86_64 and Linux aarch64 are supported; macOS arm64 is supported-development. Windows is unsupported and other Unix remains unqualified. Rerun only when Q012 production changes materially affect target-sensitive server/dashboard behavior; otherwise document freshness.

## Q006 — Disposable rootful Linux acceptance

Accepted. Real Ubuntu/systemd/cron/logrotate/service-user/install/uninstall behavior was exercised and defects corrected. Q012 does not rerun Q006 unless deployment sources change.

## Q007/Q011 — Bounded live-provider interoperability

Q007's blocked OpenCode edge remains historical. Q011 accepted the live-provider requirement using two authorized structurally distinct upstreams and seven bounded requests. Q012 does not rerun live-provider traffic unless it changes coordinator/provider/wire behavior.

## Q008 — Physical ARM64 SBC qualification

Accepted by append-only re-acceptance after Q011. Raspberry Pi 5 Ubuntu/aarch64 functional/resource evidence remains valid. If Q012 touches server/dashboard production code used by the SBC path, perform a bounded freshness follow-up rather than silently assuming unchanged evidence.

## Q009 — Sustained resource/failure stability

Accepted by append-only re-acceptance after Q011. The deterministic workload covers mixed requests, faults, reload/background churn, abrupt restart and final convergence. Rerun if Q012 changes runtime/server behavior exercised by this workload; otherwise record a source-freshness justification.

## Q010 — Aggregate M10 closure/readiness

Historical aggregate closure. Q010 originally blocked on Q007 and was later re-accepted after Q011. The post-Q011 dashboard audit means it is no longer the current M10 closure authority. Q012 must rerun the Q010-equivalent aggregate gates before it can re-close M10.

## Q012 — Dashboard state, semantic content, and visual requalification

Primary class: invariant/corrective

Q012 must:

- create deterministic shared dashboard state covering empty, populated multi-provider/account/model/request/routing/statistics data, long/Unicode/escaping values, missing optional values, private auth, and real error/missing outcomes;
- run all existing dashboard routes against meaningful populated state where applicable;
- compare page-level semantic messages, metric/card labels/values, table headers, ordered rows/cells, controls, links, escaping and stable script/static hooks;
- add deliberate negative regressions proving changed/missing/reordered dashboard data fails comparison;
- capture actual Python/Rust screenshot artifacts for a bounded visual matrix that covers every major page at least once while distributing desktop/mobile and representative themes across the set;
- distinguish theoretical screenshot metadata from captures that actually exist and have SHA-256/manual-review evidence;
- correct only narrowly scoped dashboard parity defects exposed by the fixtures;
- rerun Q001/Q002 and the Q010-equivalent final gates;
- audit freshness of Q005-Q009/Q011 evidence and rerun only affected environment qualification;
- clean the registry dependency-ready table so Q012 is the only active handoff.

Exit: every mandatory Q001 dashboard cell is actually exercised; meaningful dashboard content differences fail qualification; actual visual review covers the declared page set; no high/medium M10 finding remains; aggregate final gates are green.

## Qualification environment posture

- Deterministic local: Python/Rust side-by-side with isolated temp state and loopback providers.
- Linux x86_64 disposable: supported build/runtime and rootful deployment evidence.
- Linux aarch64: supported build/runtime plus mandatory physical SBC evidence.
- macOS arm64: supported-development build/non-root runtime evidence.
- Live-provider: explicit opt-in, bounded credentials outside repository.
- Browser visual review: qualification-only tooling outside Rust runtime dependencies.

No broad always-on OS × architecture × provider matrix is introduced.

## Dashboard evidence model

Q012 dashboard evidence should contain bounded machine-readable semantic projections and screenshot metadata. It may record route, fixture state, labels/values/rows, artifact filename/hash, viewport/theme and review disposition. It must never retain credentials, raw provider bodies, arbitrary environment dumps, private machine identity or redistribute font files.

Actual screenshots may remain outside git. The closure must prove each required capture was created and reviewed; a generated filename alone is not evidence.

## Performance/resource posture

Existing Q008/Q009 characterization remains the M10 resource authority unless Q012 invalidates it. No new dashboard performance SLA is introduced. Any new unbounded state, crash, leak or clearly impractical SBC behavior remains a blocker.

## Non-goals

M10/Q012 does not:

- redesign the dashboard;
- add a SPA/frontend framework;
- add a browser dependency to Rust production code;
- publish canonical Rust release assets;
- flip `scripts/install.sh`/README quick start;
- make Rust update metadata public authority;
- remove Python packaging/runtime;
- create a qualification-only DB schema;
- rerun paid/live/rootful/physical qualification without a freshness reason;
- create a broad permanent CI matrix.

## Closure posture

Historical Q004/Q007/Q010/Q011 closure evidence remains append-only. Only Q012 may now re-close M10. Accepted Q012 must leave no dependency-ready M10 plan and may make M11 eligible only for a separate planning review; it does not authorize or auto-promote M11 implementation.