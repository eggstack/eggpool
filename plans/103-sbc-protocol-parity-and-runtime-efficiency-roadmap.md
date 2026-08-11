# Plan 103 — SBC Protocol Parity and Runtime Efficiency Roadmap

Date: 2026-08-11
Status: planned
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`

Implementation plans:

- `plans/104-local-exposure-and-log-redaction.md`
- `plans/105-openai-anthropic-transcode-parity.md`
- `plans/106-provider-native-prompt-cache-translation.md`
- `plans/107-request-memory-and-body-limit-reduction.md`
- `plans/108-compression-cache-surface-simplification.md`
- `plans/109-test-corpus-reduction-followup.md`
- `plans/110-sbc-live-characterization-and-closure.md`

## Purpose

Perform one bounded follow-on pass after Roadmap 093 to close concrete defects and compatibility gaps found in the post-closure repository review without reopening broad hardening, routing, database, CI, or architecture work.

EggPool is already accomplishing its intended goal: a lightweight local/LAN provider router that isolates upstream failures, routes among multiple accounts, exposes OpenAI/Anthropic-compatible surfaces, persists usage in SQLite, and runs acceptably on Raspberry Pi-class hardware. The remaining work is narrower:

1. correct unsafe local/LAN exposure and logging defaults that can reveal credentials or request content;
2. update OpenAI/Anthropic transcoding for current structured-output, strict-tool, parallel-tool, reasoning-control, and prompt-cache semantics;
3. reduce avoidable request-body/object-graph copying and lifetime retention on memory-constrained SBCs;
4. simplify optional compression/cache tuning where configuration surface exceeds demonstrated runtime value;
5. reduce another tranche of semantically redundant retained tests without weakening the already-lean CI gate;
6. close with one representative live-provider Raspberry Pi 5 characterization if credentials/workload are available, recording `not measured` rather than inventing evidence otherwise.

This roadmap is deliberately not another generalized optimization or resilience initiative. Roadmaps 086 and 093 already established the failure-isolation, finalization, database ownership, backoff, rehash, persistence, and CI architecture. Those systems are protected invariants here.

## Confirmed findings driving this roadmap

### 1. Non-loopback SBC sample can run without server authentication

The shipped SBC example binds to `0.0.0.0`, while server API-key configuration is optional and the request authentication boundary returns early when no key is configured. For a local/LAN appliance this does not justify production-grade identity infrastructure, but an unauthenticated all-interface copyable example is too permissive.

The correction should be simple: either require an API key for non-loopback binds unless an explicit unsafe/local escape hatch is selected, or keep the SBC copyable example on loopback by default. Do not build users, roles, OAuth, TLS termination, or secret-management infrastructure.

### 2. Debug/transcode observability can expose secret/request bytes

The coordinator's authorization-shape debug logging currently preserves portions of credential values. Malformed tool-argument/input transcode warnings can also carry raw request content, and the coordinator emits loss warnings. Neither credential fragments nor malformed tool payload bodies are necessary for local diagnostics.

Logging should preserve only bounded metadata such as header name/scheme, value length, parse reason, value type, and byte length.

### 3. OpenAI/Anthropic transcoding is behind current provider semantics

The current translation path still uses prompt coercion for some structured-output requests and treats strict tools and parallel-tool disabling as unsupported even though current provider APIs expose native controls. Capability contracts also need to distinguish providers/models that actually support these controls rather than assuming protocol name alone guarantees support.

The target is narrow protocol parity, not a generic schema compiler or provider feature framework.

### 4. Prompt-cache translation is behind current provider-native controls

EggPool's synthetic cache policy is Anthropic-oriented and predates current explicit OpenAI prompt-cache breakpoints. Current provider APIs have overlapping but non-identical cache semantics, including different TTLs, breakpoint locations, tool-definition handling, and automatic/implicit caching behavior.

Translate only semantics that are representable. Explicitly surface lossy/unrepresentable cases instead of synthesizing misleading equivalence.

### 5. Request payload handling still amplifies memory and CPU

`ParsedRequestPayload` retains raw bytes plus a decoded object and currently bypasses the shared hot-path JSON backend. `ProviderBoundRequest` recursively freezes/thaws object graphs and deep-copies provider payloads. Long streaming requests can retain original bytes, parsed structures, transformed structures, and provider bytes well past the point where retry is possible.

For large coding-agent histories/documents, these full-graph walks and retained copies are more important SBC costs than another connection-pool or SQLite tuning pass.

### 6. Request-size semantics are internally inconsistent

The proxy has a fixed approximately 10 MiB request-body ceiling while transcode/document handling advertises larger provider document limits. A request that cannot pass the proxy cannot meaningfully claim the larger provider limit. The body limit should become a clear configurable proxy invariant, and document/transcode validation must report the effective limit truthfully.

Do not simply increase the SBC default to a large value; memory-constrained deployments need a bounded default.

### 7. Optional compression/cache tuning is more complex than its demonstrated value

Compression is disabled by default, but the retained subsystem includes multiple transforms, per-policy overrides, cache interaction, recommendation/tuning modes, and configuration semantics that are partly dormant or ambiguous. A validator also appears to check a child override without access to the global opt-in needed to make that override legal.

The roadmap should keep safe useful compression behavior but delete or reject dormant/adaptive surface that has no real runtime path. Do not build a new adaptive compression system.

### 8. CI is already appropriately lean; the retained test corpus is not

Ordinary CI is one Python 3.11 job with Ruff format/lint, Pyright, and smoke tests. That shape should remain. The retained corpus still collects roughly 8.3k tests after Plan 100, so further reduction should target duplicate semantic coverage, historical intermediate states, and optional observability/compression permutations.

### 9. Database architecture is not a target of this pass

The single aiosqlite worker, WAL, `synchronous = NORMAL`, explicit transaction ownership, fail-closed ambiguity handling, recent round-trip reduction, and evidence-based index pruning are already appropriate for the intended SBC profile. Do not add writer pools, an ORM, automatic VACUUM/REINDEX/ANALYZE, or a new SQLite tuning framework.

A future pragma change is allowed only if Plan 110 produces direct evidence of a concrete problem, and even then it must be a narrow closure correction.

## Governing constraints

1. Preserve EggPool's local/LAN SBC threat and deployment model. Do not turn this into a public multi-tenant security project.
2. Preserve request-local fault containment, provider/account failure isolation, bounded 1,800-second transient suppression/backoff, scoped model quarantine, and retry only before downstream handoff.
3. Preserve generation-owned finalization and startup crash reconciliation.
4. Preserve live rehash and runtime-generation ownership. Do not redesign it.
5. Preserve SQLite WAL, `synchronous = NORMAL`, one primary aiosqlite worker/connection, and current migration immutability rules.
6. Do not add Redis, PostgreSQL, an ORM, a durable queue, another process, or another default database worker.
7. Do not replace FastAPI, Granian, HTTPX/httpcore, aiosqlite, Pydantic, Click, or SQLite.
8. Do not rewrite EggPool in Rust or add native extensions for this roadmap.
9. Do not reduce provider pool defaults merely because smaller values look leaner; require representative stream evidence.
10. Do not add a new core runtime dependency. Improve use of the existing optional `orjson` path instead of adding another JSON package.
11. Keep compression/cache features optional and default-off/low-overhead where currently designed that way.
12. Prefer provider-native controls over prompt tricks or EggPool-specific synthetic semantics when the native protocols now support the feature.
13. Capability checks must be explicit enough to avoid sending unsupported controls to arbitrary OpenAI-/Anthropic-compatible providers, but must not become a general capability-discovery service.
14. Never log credential bytes, malformed tool payload bodies, document bodies, or stream content for diagnostics introduced by this roadmap.
15. Keep the current one-job CI shape. Do not add coverage thresholds, full-suite gates, matrices, benchmark jobs, soak jobs, hardware jobs, release automation, or permanent performance evidence formats.
16. Full retained-suite execution remains optional/manual. Child plans require focused tests plus the ordinary gate.
17. Each child plan should produce one reviewable implementation commit where practical.
18. Stop when the explicit acceptance criteria are met; do not opportunistically refactor protected routing/finalization/database systems.

## Roadmap phases

### Plan 104 — Local Exposure and Log Redaction

Correct the unsafe non-loopback/no-auth deployment combination with the smallest configuration rule that fits a LAN appliance. Remove partial credential logging and raw malformed tool argument/input content from transcode observability. Preserve useful bounded metadata and existing error classification.

### Plan 105 — OpenAI/Anthropic Transcode Parity

Update structured-output, strict-tool, parallel-tool-disable, and capability-aware reasoning/control translation. Reconcile the `TranscoderFeatures.tools` contract with actual body/stream behavior. Use native provider semantics where currently supported and make unsupported/lossy mappings explicit.

### Plan 106 — Provider-Native Prompt Cache Translation

Translate current explicit prompt-cache boundaries between OpenAI and Anthropic only where semantics match. Represent TTL/tool-definition/automatic-cache mismatches explicitly. Reassess whether the existing Anthropic-only synthetic cache policy should shrink once native translation exists.

### Plan 107 — Request Memory and Body-Limit Reduction

Route central request JSON parsing through `eggpool.jsonx`; eliminate recursive physical freeze/thaw/deepcopy work where logical ownership is enough; preserve original bytes for untouched native dispatch; release heavy request buffers after downstream handoff when retry is impossible; make request-body/document limits configurable and internally truthful.

### Plan 108 — Compression/Cache Surface Simplification

Fix the static-prefix override validation boundary, remove/reject dormant tuning modes with no production execution path, centralize duplicate cheap token-estimation logic, and delete compression/cache coupling that native cache translation makes unnecessary. Preserve safe suffix compression where actually supported and useful.

### Plan 109 — Test Corpus Reduction Follow-up

After Plans 104–108 settle production surfaces, delete/consolidate another tranche of redundant tests, especially historical implementation-state, optional observability, transcode permutation, and compression/cache matrix tests. Preserve high-value regression coverage and the current one-job CI gate.

### Plan 110 — SBC Live Characterization and Closure

Run aggregate correctness verification and one representative Raspberry Pi 5 live-provider workload if available. Measure actual EggPool-controlled RSS/CPU/socket/WAL/local-preparation effects using existing tools only, with specific attention to large prompt/tool requests and long streams. Correct only demonstrated regressions and close the roadmap.

## Dependency order

```text
104 exposure/redaction -----------------------------+
105 transcode parity -------------------------------+--> 109 test reduction --> 110 closure
106 native cache translation --+                    |
                               +--> 108 simplify ---+
107 request memory/body limits ---------------------+
```

Plan 104 is independent and should land early because it addresses direct leakage/exposure. Plans 105 and 106 should be implemented in that order so cache translation can reuse the final capability/transcode contract. Plan 107 is largely independent. Plan 108 should follow Plan 106 so simplification decisions reflect the new native-cache path. Plan 109 follows production changes so tests are reduced against the final supported surfaces. Plan 110 is last.

## Cross-phase invariants

- Requests that previously succeeded on supported protocol semantics continue to succeed unless a formerly silent lossy translation is intentionally converted to an explicit capability/loss rejection.
- No provider/account penalty, quarantine, or retry is introduced for local transcode/configuration failures.
- Streaming retry remains impossible after downstream handoff.
- No credential/request-content bytes are added to logs, persistence, metrics, or closure evidence.
- Native structured-output/cache controls are sent only when the selected provider/model capability contract permits them.
- Loss warnings remain bounded and describe metadata, not raw content.
- Request-size rejection occurs before expensive avoidable transforms whenever practical and reports the proxy-effective limit clearly.
- Native no-transform requests do not decode/re-encode solely for architectural convenience.
- Heavy request buffers may be released only after the lifecycle no longer needs them for retry, response adaptation, accounting, or diagnostics.
- Optional compression/cache configuration must not construct work/tasks when disabled.
- Database durability/fail-closed semantics remain unchanged.
- CI remains one Python 3.11 format/lint/type/smoke job.

## Aggregate verification policy

Every child plan runs focused tests plus the existing ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Provider-protocol plans additionally run the surviving transcoder/capability contract suites. Memory plans may use temporary/manual tracemalloc or process observations during implementation, but no benchmark or memory threshold becomes permanent CI.

## Roadmap acceptance criteria

- [ ] Copyable non-loopback deployment can no longer silently expose an unauthenticated EggPool listener without an explicit operator choice.
- [ ] Authorization diagnostics never include credential substrings.
- [ ] Malformed tool argument/input transcode diagnostics never include raw request content.
- [ ] OpenAI structured-output requests map to Anthropic native structured-output controls when supported; fallback/loss behavior is explicit when unsupported.
- [ ] Strict tool semantics translate in both supported directions.
- [ ] OpenAI `parallel_tool_calls = false` and Anthropic parallel-tool-disable semantics translate correctly where supported.
- [ ] Transcoder feature/capability configuration matches actual body and streaming behavior; no stale `tools` contract remains.
- [ ] Reasoning/thinking controls are capability-aware rather than globally assumed for arbitrary compatible providers.
- [ ] Provider-native prompt-cache breakpoints translate only where semantically representable, with TTL/location mismatches explicitly surfaced.
- [ ] Cache translation does not invent false equivalence for Anthropic tool-definition caching or incompatible TTL semantics.
- [ ] Central request parsing uses the shared hot-path JSON backend.
- [ ] Provider-bound payload ownership no longer performs recursive freeze/thaw/deepcopy cycles when logical ownership suffices.
- [ ] Native no-transform dispatch preserves original bytes when safe and behaviorally equivalent.
- [ ] Heavy request payload references are released after downstream handoff when no longer needed.
- [ ] Request-body limit is configurable, bounded by default, and document/transcode validation reports the effective proxy/provider limit truthfully.
- [ ] Compression static-prefix override validation is correct across global and per-policy configuration.
- [ ] Dormant/ambiguous compression tuning surface is implemented, removed, or rejected explicitly; no configuration mode claims runtime behavior it does not have.
- [ ] Duplicate cheap token-estimation logic is reduced if compression remains.
- [ ] No new core runtime dependency, database service, SQLite worker pool, or migration is introduced solely for this roadmap.
- [ ] Retained tests are materially simpler/smaller without losing high-value routing/failure-isolation/streaming/database/rehash/protocol regressions.
- [ ] Ordinary CI remains the current one-job Python 3.11 smoke gate with Ruff/Pyright.
- [ ] Plan 110 records real Raspberry Pi 5 live-provider measurements where available, or `not measured` without extrapolation where unavailable.
- [ ] Roadmap closes without creating a new benchmark, adaptive tuning, capability-discovery, or security framework.

## Rejection conditions

Do not close this roadmap if any of the following is true:

- a normal copyable SBC/LAN configuration still binds all interfaces without authentication by accident;
- logs expose any credential prefix/suffix or malformed tool/document body content;
- structured-output/tool/cache translation relies on prompt injection/coercion where a verified native protocol control should be used;
- protocol-name heuristics cause unsupported provider controls to be sent without a capability contract;
- cache translation silently changes TTL or breakpoint semantics without a loss signal;
- request-memory work replaces one copy with a shared-mutable alias that permits provider transforms to mutate client/canonical payload state;
- buffers are released before retry/finalization/accounting invariants no longer need them;
- the default request-body limit is raised substantially without SBC memory reasoning;
- compression simplification removes user-visible supported behavior without migration/deprecation handling;
- CI grows a benchmark/full-suite/hardware/coverage/release job;
- target-device measurements are fabricated or converted into brittle gates.

## GPT-5.6 Luna execution protocol

For every child plan:

1. Read this roadmap, the assigned child plan, `AGENTS.md`, and directly affected architecture/config documentation.
2. Re-verify current official OpenAI/Anthropic semantics immediately before changing provider mappings; do not rely on stale memory when the external API can change.
3. Use `rg` to find authoritative production call sites before editing or deleting configuration fields/helpers.
4. Prefer one explicit capability flag/translation rule over a generic extensible framework.
5. Preserve protected request lifecycle, routing, finalization, database, and rehash invariants.
6. Run the smallest focused tests first, then the ordinary repository gate.
7. Record exact verification commands/results and any intentionally unmeasured resource evidence in the child plan at closure.
8. Do not create plan-numbered tests, retained benchmark artifacts, or new CI jobs.
9. Stop when the child plan's acceptance criteria are satisfied.