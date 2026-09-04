# Foundation Milestone F005 — Axum SSR Shell and Static-Asset Parity Baseline

Status: blocked on F004 interface

Repository baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

Source roadmap: `migration-rs/subsystems/foundation-roadmap.md#F005`

Applicable ADR: ADR-0002.

Primary class: capability

## 1. Objective

Stand up the first real Rust EggPool HTTP server and prove that the finished Python SSR dashboard/static frontend can be mirrored in Rust without redesign: health/readiness plus a selected dashboard/read-plane slice, compatible auth/body-limit behavior, copied static assets, and renderer/escaping foundations.

## 2. Dependencies

Hard: F001 and F002. Interface: F004 must expose stable DB read interfaces for any DB-backed page included. F003 config structures should be used if closed; otherwise F005 waits rather than create a temporary incompatible server config model.

## 3. Python oracle evidence

Primary sources: `src/eggpool/app.py`, API route modules, auth/body readers, dashboard routes/render/escape/theme/static resources, stats service/query interfaces, API reference, and dashboard architecture docs/tests.

## 4. Invariants

- no frontend redesign or SPA conversion;
- same selected route paths/methods/status/auth behavior;
- static resources copied byte-for-byte from the frozen baseline and drift-guarded;
- HTML escaping remains safe for provider/model/source-controlled text;
- no raw prompts/tool outputs/request bodies/auth headers appear in dashboard/stats;
- request body limit is enforced before unbounded JSON parsing for routes that accept bodies;
- server shutdown closes listeners/tasks cleanly;
- dashboard/public-vs-auth policy matches the selected Python surfaces.

## 5. Scope

### In scope

Tokio/Axum/Tower inbound server; Rustls only if required for existing local behavior (do not invent inbound TLS); auth middleware; client attribution helpers only as needed; health/readiness; static file serving; theme/resource path behavior; HTML escaping and renderer helpers; selected initial pages such as overview/runtime shell or another low-dependency representative page; selected JSON read endpoints; deterministic server startup/shutdown for tests; copied assets and manifest/hash guard.

### Out of scope

Provider dispatch, Chat/Responses/Messages inference endpoints beyond explicit placeholder routing, complete stats/model-info port, all dashboard pages, runtime rehash, daemonization, deployment/systemd, WebSockets, frontend redesign.

## 6. Required production changes

Add Axum/Hyper/Tower dependencies only now that a server exists. Reuse the F003 config model and F004 repository traits rather than inventing temporary duplicates.

Static assets should be copied into a Rust-owned package path such as `rust/assets/dashboard/` with a generated/reviewable manifest of path + cryptographic hash. During dual implementation, a test should compare the frozen source asset set to the Rust copy and fail on unexplained drift.

SSR rendering may use direct string builders/helpers or a minimal template mechanism; choose based on fidelity and maintainability, not framework fashion. Preserve escape boundaries explicitly.

Server tests should bind `127.0.0.1:0` rather than fixed production port.

## 7. Work packages

A. Add Axum server state, listener lifecycle, graceful shutdown, health/readiness routes.

B. Port server API-key authentication and relevant public-dashboard gating.

C. Copy/freeze static assets/themes and add drift manifest.

D. Port escaping/resource/theme primitives.

E. Port one representative SSR page and its required read-model interface.

F. Port one representative JSON stats/read endpoint sharing the same underlying data.

G. Add DOM/static/API differential cases and targeted visual inspection instructions.

## 8. Failure/cancellation/restart/contention

Server startup bind/config failure exits cleanly. Graceful shutdown stops admission and awaits active low-risk read requests within a bounded testable policy. DB read errors return protocol-appropriate local errors without panics or secret leakage. Concurrent dashboard requests must not mutate shared renderer state unsafely.

## 9. Compatibility/migration

The Rust server is development-only at this stage and runs on explicit test ports. It does not replace Python `serve` or systemd. Static copy is a migration artifact; user-visible asset paths remain unchanged.

## 10. Tests

- health/readiness differential status/body/headers;
- auth allowed/denied/missing-key behavior;
- static path/content-type/body hash parity;
- theme/static inventory parity;
- HTML escaping attack strings;
- representative SSR page DOM/text/link/class/id facts;
- representative JSON endpoint schema/value parity on controlled DB fixture;
- empty DB stable shape;
- malformed query returns 4xx rather than 500;
- bind failure and graceful shutdown cleanup;
- no secret/request-content leakage.

Visual inspection should be targeted to the ported page(s) and fixed viewport; it supplements, not replaces, DOM/static tests.

## 11. Verification commands

Rust fmt/clippy/test; migration HTTP/SSR differential suite; targeted Python dashboard/API tests; static manifest check. No browser farm or broad visual-regression infrastructure is required.

## 12. Documentation

Document how to run the Rust development server on a non-conflicting port, which dashboard/API routes are currently ported, and how static asset parity is checked.

## 13. Acceptance criteria

A developer can run Python and Rust EggPool simultaneously on different ports, open the selected Rust dashboard page, and observe the same visual/content contract using the copied assets. Health/readiness and selected JSON/SSR surfaces pass differential tests.

## 14. Stop conditions

Stop if implementing a selected page pulls in provider dispatch/routing, if a frontend redesign becomes necessary, if static assets cannot be copied without licensing/packaging issues, or if F003/F004 interfaces are unstable enough to cause duplicate temporary architecture.

## 15. Closure evidence

Rust server run commands, route parity matrix, static asset manifest comparison, DOM/escape tests, representative visual inspection record, shutdown/bind error tests, dependency delta, and verification outputs.

## 16. Handoff notes

Choose the representative page for dependency value, not visual novelty. The goal is to prove the SSR porting pattern that later dashboard work can repeat.
