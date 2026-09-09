# Q007 — Bounded Live-Provider Interoperability Smoke

Status: queued behind Q006

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q006.

Primary class: invariant/polish

Hard dependency: accepted Q006.

Operational dependency: maintainer-provided credentials for the Q001 live-provider cells. Credentials must not be stored in the repository or closure artifacts.

## Objective

Validate a deliberately small set of real upstream interactions that deterministic fixtures cannot fully prove: public TLS/API edge behavior, live auth/header expectations, endpoint paths, model identifiers, SSE framing, request-id headers, and provider-specific success envelopes.

This is not a performance test, traffic mirror, provider certification program, or failure-abuse exercise.

## Provider/surface selection

Q001 freezes the exact mandatory live cells. For M10 closure, target at least two structurally distinct available upstream surfaces, preferably:

- one OpenAI-compatible provider/account; and
- one different wire family or materially different surface, such as Anthropic Messages, Gemini generateContent, or a provider requiring an alternate declared wire surface.

Choose low-cost accounts/models that are already intentionally configured for EggPool testing. Record only provider/surface/model identifiers that are safe to disclose; redact account names if they carry personal information.

If required credentials are unavailable, Q007 remains blocked rather than silently substituting mocks and claiming live qualification.

## Cost and safety budget

Freeze an explicit per-run budget before execution:

- tiny deterministic prompts;
- low `max_tokens`/output cap;
- at most a small fixed number of finite and streaming requests per cell;
- no automatic retry loop outside EggPool's ordinary bounded retry policy;
- no large multimodal/document payloads unless Q001 marks one live media cell mandatory;
- no concurrency/load test against paid upstreams;
- no deliberate rate-limit exhaustion;
- no deliberate invalid credential/revocation test;
- no requests intended to trigger moderation/safety incidents.

The closure must report request counts and approximate token/cost envelope where available, not secret billing/account data.

## Required success observations

For each provider/surface cell capture bounded semantic facts:

- TLS/HTTP connection succeeds through the intended direct/Eggress path;
- provider catalog/model identifier resolves as expected where catalog is part of the path;
- correct upstream path/method/auth shape is accepted;
- finite inference returns successful normalized client response;
- streaming inference produces incremental events and a valid terminal event;
- usage/token fields are present/absent according to provider reality and normalized correctly;
- upstream request-id header/evidence is captured when the provider supplies one;
- final request/attempt/reservation rows converge;
- account health/circuit/quota state remains sane after success;
- no credential/proxy header leaks into client response, logs, or evidence.

## Cross-surface observations

Where a chosen provider supports more than one qualified client/wire surface, include one small transcode/adaptation cell that is likely to expose real envelope differences, for example:

- Chat client -> Anthropic/native upstream;
- Messages client -> OpenAI-compatible upstream;
- Responses client -> OpenAI-compatible Responses/native upstream;
- declared alternate wire profile selected through configured preference.

Do not add a new codec solely to satisfy Q007; an unsupported live surface is a finding against existing claims.

## Proxy path

If a safe maintainer-owned proxy test endpoint/account is available and Q001 marks proxy live smoke mandatory, run one tiny request through Eggress and compare with a direct request. Do not require third-party open proxies or expose proxy credentials.

Deterministic T006 tests remain authoritative for destructive/edge proxy protocol failures.

## Failure handling

Q007 does **not** intentionally create invalid-auth, 429 storm, malformed-stream, timeout, or model-absence conditions against live providers. If a normal live request naturally returns an error:

- capture only bounded status/error classification and request id;
- let EggPool's existing failure policy run normally;
- do not automatically hammer retries beyond configured budget;
- treat repeated unexplained provider mismatch as a finding and reproduce with deterministic fixture if possible.

Any live-discovered defect must gain a deterministic regression using a sanitized fixture before closure.

## Harness

Add an opt-in qualification script that:

- is disabled unless an explicit environment flag/CLI switch is set;
- reads credentials through the normal EggPool env/config mechanisms;
- checks the Q001 per-run request budget;
- prints the planned provider/surface/request count before sending;
- redacts secrets in all output;
- writes only the bounded Q001 evidence schema;
- never runs from normal CI by default.

## Required tests

Offline tests must prove the live harness:

- refuses to run without explicit opt-in;
- refuses a request plan exceeding the frozen budget;
- redacts configured secrets/proxy URLs;
- does not persist raw response bodies;
- classifies unavailable credentials as blocked, not passed;
- emits deterministic evidence from a loopback fake provider before real use.

## Verification

Run offline regression first:

```text
cargo test --manifest-path rust/Cargo.toml --all-targets --no-run
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# Q007 offline harness tests
```

Then run the explicitly authorized live command and record exact non-secret arguments/environment class in closure.

After any source fix, rerun Q002 deterministic aggregate and the affected M4-M7 suites.

## Non-goals

Q007 does not mirror production traffic, benchmark throughput, certify every provider/model, deliberately trigger account penalties, store credentials, or make live calls part of normal CI.

## Closure evidence

Write `migration-rs/closure/qualification/007-status.md` with:

- provider/surface classes exercised;
- candidate/model identifiers safe to disclose;
- finite/stream/transcode/proxy matrix results;
- request count/token/cost envelope;
- bounded request-id/usage observations;
- live-discovered defects and deterministic regressions;
- credentials/redaction review;
- blocked cells if any;
- registry transition.

## Acceptance criteria

Q007 closes only when every mandatory Q001 live cell has real evidence, at least two structurally distinct upstream surfaces are qualified where required, all discovered defects have deterministic regression coverage, evidence is secret-free, and no unresolved high/medium live interoperability finding remains.

Accepted Q007 promotes only Q008.