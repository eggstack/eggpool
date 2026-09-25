# EggPool Long-Term Specification

Status: canonical long-term implementation directive

Companion documents:

- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md` (normative governance)

This document defines the intended end state for EggPool. It establishes
product scope, runtime boundaries, ownership, protocol expectations, storage
properties, and acceptance criteria. It is grounded in
`architecture/overview.md` plus the `architecture/deep-dive-*` series, which
remain the module-level design authority; this file does not duplicate
deep-dive detail.

The keywords MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, and MAY are
normative.

## 1. Product definition

EggPool is a native Rust, LAN-hosted LLM provider proxy. It aggregates
multiple provider accounts behind OpenAI Chat Completions
(`POST /v1/chat/completions`), OpenAI Responses (`POST /v1/responses`,
including `/v1/responses/compact`), and Anthropic Messages
(`POST /v1/messages`) compatible paths. The shipped runtime lives under
`rust/`; the native wheel (see `packaging/pypi/pyproject.toml`) contains the
executable plus metadata/assets only. Repo-root `pyproject.toml` is
tooling-only.

## 2. End-state invariants (normative)

1. **Generation-owned execution.** All inference work runs under a
   generation lease (`GenerationLease`) on an immutable `RuntimeGeneration`
   published atomically by `RuntimeManager` (`rust/src/runtime_lifecycle/`).
   In-flight work finishes on the retiring generation; new work uses the
   active generation. All transitions fail closed on validation, commit, or
   ownership ambiguity.
2. **Single admission path.** Inference admission is owned by
   `rust/src/coordinator/endpoints.rs`: one endpoint execution call per
   request, shared finite/streaming selection, depth validation, and
   `from_admitted` construction. `is_inference_path()` covers all public
   inference routes (Plan 249).
3. **Thin server adapters.** `rust/src/server/` holds no coordinator
   retry/finalization logic. EggServe 0.3 drives the pre-bound downstream H1
   listener and adapts into the existing Axum router; EggPool retains app
   policy and process lifecycle (Plans 246–248, 250).
4. **Single SQLite gate.** One connection/gate, WAL/NORMAL, existing
   publication/finalization ownership, and the pre-existing passive
   maintenance checkpoint stay as-is until a reviewed design with
   restart/reload/backup/restore/recovery bounds and loopback evidence lands
   (Plan 240; diagnostics in Plans 237–239 remain diagnostic-only).
5. **Canonical wire boundary.** `rust/src/wire/ir.rs` is the canonical
   intermediate representation. Codecs never chain translated payloads.
   Native Responses observation shares the canonical SSE decoder and folds
   bounded terminal/usage facts without buffering arbitrary native streams.
6. **Reload vs restart authority.** `rust/src/config_reload_policy.rs::classify_transition`
   is the only reload-vs-restart authority. Mixed changes are wholly
   restart-required. `[integrations].advertise_base_url` is live-reloadable
   profile output only and never changes the listen socket.
7. **Error mapping authority.** `rust/src/error.rs` owns HTTP/status
   mappings. New variants keep context explicit and retain causes.
8. **Secret-free diagnostics.** Credentials, prompts, raw bodies, and cache
   keys stay out of persistence, logs, and diagnostics.
9. **No Python runtime fallback.** Repo-root `scripts/` and
   `tests/tooling/` are tooling only, never a runtime fallback.
10. **No-default build parity.** `--no-default-features` MUST compile and
    test; it keeps direct/non-SSH proxy paths and rejects SSH proxy config
    as `TransportError::ProxyConfiguration` before dialing.

## 3. Ownership boundaries (normative)

- `runtime.rs` adapts CLI to operations; reusable lifecycle lives in
  `operations/lifecycle.rs`; health aggregation in `operations/status.rs`
  (shared readiness with `readyz`, no outbound probes, secret-free).
- Attempt preparation may borrow generation/request data only synchronously;
  `PreparedUpstreamAttempt` is fully owned before `submit_once` is awaited.
- `ProviderClientPool` publishes an immutable nested provider/account
  topology and closes it atomically; per-request topology mutexes and
  allocated tuple lookup keys MUST NOT be reintroduced.
- Public `FiniteRequest` and `CompactAdmittedRequest` shapes are
  compatibility surfaces; compact execution may use a private single-owner
  admission representation internally.

## 4. Protocol and compatibility

EggPool serves OpenAI-compatible Chat Completions, Responses (finite,
streaming, compact), and Anthropic Messages surfaces. Cross-surface
Responses preparation rejects native-only semantics or emits bounded
adaptation notices. Streams require wire terminal evidence; transport EOF is
never synthesized into success.

## 5. Performance posture

The Plans 230–234 residual campaign is evidence-gated: keep the single
SQLite gate, streaming mpsc bridge, Tokio `current_thread` runtime, and
routing selection lock unless comparable loopback measurements justify a
narrowly scoped change.

## 6. Non-goals

No second HTTP abstraction or retry owner; no new restart/reload key lists;
no buffering of arbitrary native streams; no EggPool SSH executor fallback
(Eggress 1.0.8 outbound owns proxy transport); no Windows proxy support
implied by the `eggpool-connect` helper.
