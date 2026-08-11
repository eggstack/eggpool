# Plan 104 — Local Exposure and Log Redaction

Date: 2026-08-11
Status: planned
Parent roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`

## Purpose

Close three concrete local/LAN safety defects without expanding EggPool into a production identity/security platform:

1. prevent copyable non-loopback deployment from accidentally exposing an unauthenticated listener;
2. remove credential substrings from authorization-shape logging;
3. remove raw malformed tool argument/input content from transcode loss/warning logging while preserving bounded diagnostic metadata.

This plan is intentionally small. It must not redesign authentication, trusted-proxy handling, provider credential storage, TLS, routing, or error persistence.

## Confirmed current behavior

- The ordinary default host is loopback, but the SBC example binds `0.0.0.0`.
- Server API-key configuration is optional.
- The request authentication boundary returns without enforcing authentication when no server key resolves.
- The coordinator's authorization-shape helper currently preserves a credential prefix and suffix for sufficiently long values.
- OpenAI→Anthropic and Anthropic→OpenAI malformed tool parsing can attach raw malformed values to warning metadata.
- Transcode observability logs warning metadata, making those raw values reachable by normal logs.

These are local safety/diagnostic defects, not evidence that EggPool needs a broader security subsystem.

## Governing constraints

1. Keep local/LAN deployment as the design center.
2. Do not add user accounts, roles, sessions, OAuth/OIDC, JWT infrastructure, mTLS, a secret manager, or a database-backed auth subsystem.
3. Preserve the existing simple server API-key mechanism.
4. Preserve loopback/no-auth as a valid local-only mode.
5. Preserve `security.trusted_proxies` behavior and forwarded-IP attribution rules.
6. Do not log any credential bytes, even at DEBUG.
7. Do not log raw malformed tool arguments, tool input, document bodies, request messages, or stream content.
8. Diagnostic replacement metadata must be bounded and non-secret.
9. Do not change provider/account failure penalties, retry behavior, or persistence because of redaction changes.
10. Do not add a new runtime dependency, database migration, CI job, or telemetry service.

## Workstream A — Discover the authoritative authentication/config boundary

Before editing, locate the current symbols and all callers:

```bash
rg -n \
  'require_auth|api_key|server.*host|0\.0\.0\.0|trusted_proxies|ConfigStartupAuthError' \
  src config*.toml deploy docs architecture tests
```

Classify:

- server host validation performed at config parse/startup;
- runtime request authentication enforcement;
- CLI `check-config` behavior;
- example/bundled config copies under `src/eggpool/_share/`;
- tests that intentionally exercise loopback/no-auth and non-loopback/auth behavior.

Do not duplicate the same rule in many layers unless required for a clear startup error plus runtime defense-in-depth.

## Workstream B — Define the minimal non-loopback authentication rule

Preferred policy:

- loopback binds (`127.0.0.1`, `::1`, equivalent supported loopback forms) may run without a server API key;
- non-loopback/wildcard binds require a configured server API key by default;
- if the project already has a clearly named explicit unsafe/local bypass suitable for this purpose, reuse it;
- if no such bypass exists, prefer correcting the SBC copyable example to loopback plus documentation over inventing a broad `allow_unsafe` security mode solely for one example.

The executor must inspect current deployment/documentation behavior before choosing between:

### Option A — startup validation

Reject non-loopback + no server API key during `check-config`/startup with an existing configuration error type and actionable message.

Use this if it does not break an intentional documented LAN/no-auth contract.

### Option B — safe copyable example only

Keep the runtime API behavior unchanged but change `config.sbc.example.toml` and bundled copy to loopback when no key is supplied, with a concise comment that LAN binding requires configuring an API key.

Use this only if current public behavior intentionally supports unauthenticated LAN binding and changing that behavior would be too disruptive for this narrow pass.

Roadmap preference is Option A because accidental all-interface/no-auth startup is the actual unsafe state, but do not create a new compatibility layer to achieve it.

## Workstream C — Keep examples and bundled configs synchronized

If the rule or sample changes, audit:

```bash
rg -n 'config\.sbc\.example|host = "0\.0\.0\.0"|api_key' \
  config*.toml src/eggpool/_share deploy docs README.md AGENTS.md architecture
```

Requirements:

- source and bundled SBC examples must agree;
- `check-config` must provide the same validation result as startup;
- docs must not tell users to bind all interfaces with no auth;
- no new deployment guide is needed if an existing note can be updated.

## Workstream D — Remove credential substrings from auth diagnostics

Locate the current helper/callers:

```bash
rg -n '_redact_auth_shape|authorization.*shape|Authorization|Proxy-Authorization' src tests
```

Replace any prefix/suffix output with metadata only. Acceptable fields include:

- header name;
- parsed scheme name if it can be extracted without exposing token bytes;
- total value length;
- whether the header was absent/present/malformed.

Examples of acceptable diagnostic shapes:

```text
Authorization: scheme=Bearer length=51
Authorization: present length=51
```

Do not include hashes/digests unless an existing operational requirement actually uses them. A digest adds complexity and can still become an unnecessary stable identifier.

## Workstream E — Remove raw malformed tool content from loss warnings

Locate both directions and the observability emitter:

```bash
rg -n \
  '_parse_tool_arguments|_parse_tool_input|loss_warnings|_emit_transcode_observability|"raw"' \
  src/eggpool/transcoder src/eggpool tests
```

For malformed tool arguments/input:

- preserve the warning code/reason;
- preserve safe structural metadata such as source type (`str`, `dict`, `list`, etc.);
- preserve encoded/character length if useful and cheap;
- preserve parse-error category without embedding the original content;
- do not include exception messages if the JSON parser can echo raw input into them; sanitize or replace with a stable reason where necessary.

The warning object itself should be safe before it reaches logging. Do not rely only on the final logger to recursively scrub arbitrary warning dictionaries.

## Workstream F — Bound warning/log amplification

Review whether a single malformed request can generate an unbounded list of near-identical loss warnings.

If the current warning collector is already bounded, document and preserve it.

If it is not bounded, add the smallest local cap to observability output while preserving the full request rejection/translation result. For example:

- retain the first small fixed number of warnings;
- add an omitted-warning count;
- do not change the actual translation/loss-policy decision merely because logging is capped.

Do not create a general logging-rate-limit framework.

## Workstream G — Focused regression coverage

Add/update capability-based tests, not plan-numbered tests, covering at minimum:

### Authentication/exposure

- loopback + no server API key remains valid if that is the retained policy;
- non-loopback + API key is valid;
- non-loopback + no key is rejected at the selected authoritative boundary if Option A is chosen;
- `check-config` and startup validation agree;
- shipped and bundled SBC examples pass validation after edits.

### Credential logging

- an authorization token with distinctive prefix/suffix bytes does not expose those bytes in captured DEBUG logs;
- scheme/length metadata remains available if retained.

### Tool-warning logging

- malformed OpenAI tool arguments do not appear verbatim in warning/log output;
- malformed Anthropic tool input does not appear verbatim;
- safe warning code/reason/type/length metadata remains;
- loss-policy `warn`/`reject` behavior remains otherwise unchanged.

Use sentinel secret strings and assert they are absent from the complete captured record, not just one formatted field.

## Workstream H — Documentation and release-note scope

Update only affected operator guidance:

- copyable SBC config comment/documentation;
- server auth/config docs if startup policy changes;
- changelog/release note only if non-loopback/no-auth startup becomes an intentional behavior change.

Do not add a security whitepaper or public-internet deployment guide.

## Verification

Run focused config/auth/transcoder/logging tests identified by repository search, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

If startup validation changes, explicitly exercise one temporary non-loopback/no-key config and one non-loopback/with-key config through the same validation entry point used by production startup.

## Acceptance criteria

- [ ] The selected policy for non-loopback/no-auth startup is explicit, minimal, and documented.
- [ ] Copying the shipped SBC example cannot accidentally produce an unauthenticated all-interface listener without an explicit operator action.
- [ ] Loopback/no-auth remains available for local-only use unless existing project policy already requires auth everywhere.
- [ ] Non-loopback + valid server API key remains supported.
- [ ] `check-config` and production startup enforce the same auth/bind rule.
- [ ] Source and bundled SBC examples remain synchronized.
- [ ] Authorization-shape logging contains zero credential prefix/suffix/token bytes at all log levels.
- [ ] No new credential digest/stable fingerprint is introduced without demonstrated need.
- [ ] OpenAI malformed tool-argument warnings contain no raw malformed argument content.
- [ ] Anthropic malformed tool-input warnings contain no raw malformed input content.
- [ ] Parser exception messages cannot reintroduce the raw malformed payload into logs.
- [ ] Safe bounded metadata remains sufficient to identify the warning type/reason and approximate size.
- [ ] Any observability warning cap does not change translation/loss-policy semantics.
- [ ] Provider/account failure classification, retry, quarantine, and finalization behavior are unchanged.
- [ ] No new runtime dependency, DB migration, auth service, logging framework, or CI job is added.
- [ ] Focused auth/config/redaction/transcoder tests pass.
- [ ] Ruff, Pyright, smoke tests, and both shipped config checks pass.

## Rejection conditions

Reject the implementation if:

- the fix introduces users/roles/OAuth/JWT/mTLS or other production-SaaS auth machinery;
- ordinary loopback development becomes unnecessarily difficult;
- a wildcard/non-loopback sample still starts unauthenticated by accident;
- credential prefixes/suffixes or malformed tool payload bytes remain in any captured log path;
- redaction is implemented only at final string formatting while raw secret data remains in warning/diagnostic objects that can be logged elsewhere;
- request rejection or provider penalty semantics change because warning metadata was sanitized;
- a generic logging scrubber/rate-limiter subsystem is added without need;
- CI or dependency surface expands.

## GPT-5.6 Luna implementation sequence

1. Read Plan 103, this plan, `AGENTS.md`, current config validation/auth code, SBC examples, and transcode observability code.
2. Use `rg` to identify the authoritative startup/config boundary and choose the smallest non-loopback policy implementation.
3. Fix examples/bundled copies together with the rule.
4. Remove credential-byte logging at the producing helper.
5. Remove raw malformed tool content at the warning-producing parsers.
6. Add focused sentinel-secret regression tests using existing test modules/fixtures.
7. Run focused tests, then the ordinary repository gate and both config checks.
8. Record implementation SHA, exact policy choice, and verification results in this plan.
9. Stop; do not broaden into general security hardening.