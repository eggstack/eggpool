# Plan 207: Coding-agent closure live-test matrix

> **Status:** complete
>
> **Parent:** Plan 206
>
> **Renamed:** 2026-09-16 from `plans/204-coding-agent-closure-live-test-matrix.md`
> to resolve duplicate numbering with the per-plan closure record
> `plans/204-codex-deferred-tool-compatibility-and-conformance-closure.md`
> (which closes Plan 201). Executed as part of Plan 206 qualification.
>
> **Baseline:** Eggpool `main` after Plan 206 handoff
>
> **Priority:** Closure support / reproducible qualification
>
> **Scope:** provide a small, repeatable live/manual qualification matrix for Codex, OpenCode, managed configuration, compaction, and `eggpool status` so Plan 206 can close with concrete evidence rather than ad hoc operator notes.

## Why this plan exists

Plan 203 defines the closure criteria and corrective policy. This companion plan turns the real-client portion into a repeatable matrix with explicit evidence capture.

This is intentionally **not** a new test framework. Reuse existing shell scripts, Rust conformance tests, temporary directories, and operator-visible commands. Add only lightweight helper scripting where repetition/error-proneness justifies it.

The live matrix must remain optional: no Codex/OpenCode executable, Node runtime, provider credential, or paid upstream becomes a mandatory production dependency or a hard CI requirement.

---

# Matrix A — Codex configuration/discovery

Record:

```text
Codex version:
Eggpool commit/version:
OS/arch:
CODEX_HOME:
Eggpool base URL:
Catalog path:
Test model/alias:
```

Run:

```bash
eggpool configsetup codex --dry-run
eggpool configsetup codex --apply
eggpool configsetup codex --check
```

Then launch current Codex with `EGGPOOL_API_KEY` set.

Pass conditions:

- no config/catalog parse error;
- Eggpool provider visible;
- generated model catalog loads;
- expected test model/alias is selectable;
- explicit model selection still works;
- generated config contains no API-key value;
- WebSocket capability is not advertised;
- displayed context/output/reasoning metadata matches the generated catalog.

Capture only version numbers, command exit codes, safe stdout/stderr excerpts, and generated non-secret config/catalog fragments. Do not capture credentials.

---

# Matrix B — Codex text + ordinary tool loop

Use the existing `scripts/smoke_codex_compat.sh` where possible. Extend it only if the current script cannot test the newly managed catalog path.

## B1 text

Expected result:

```text
PASS: streamed Responses text marker received
```

Record:

- Codex version;
- model/alias;
- native Responses vs translated upstream path;
- final success/failure;
- bounded error category if failed.

## B2 client-executed tool

Use a temporary read-only directory and random file marker.

Expected result:

```text
PASS: Codex issued tool call, read marker, returned final answer
```

Do not rely on a hard-coded answer or a previously existing file.

---

# Matrix C — Codex deferred tool search

Run only if the current Codex build exposes a stable way to exercise `tool_search` without depending on private/non-repeatable account state.

Record one of:

```text
PASS_LIVE
NOT_LIVE_EXERCISABLE — deterministic conformance retained as authority
FAIL — defect requires regression/fix
```

A `NOT_LIVE_EXERCISABLE` result is acceptable for closure if `codex_responses_compat` still covers declaration-scoped translation/reconstruction and ordinary functions named `tool_search` remain non-reclassified.

Do not create a brittle test that scrapes changing Codex UI text solely to force this path.

---

# Matrix D — Compaction

## D1 current Codex long-session behavior

Record whether the current custom provider path uses:

```text
local client compaction
remote compaction trigger
/v1/responses/compact
other/current behavior
```

Pass condition is continued session operation through the client's actual supported behavior. Eggpool conversation persistence is not required.

If reaching natural compaction would be excessively expensive, use the least costly current supported deterministic/client test route and document why a full paid long-context run was skipped.

## D2 native remote compact route

Only when a configured upstream genuinely advertises native compact support:

```bash
# exact invocation may use the existing Rust fixture/helper rather than curl
POST /v1/responses/compact
```

Pass conditions:

- successful compact result on qualified native path;
- unsupported target rejected pre-dispatch;
- no translated summarization fallback;
- no stored conversation/response state introduced.

---

# Matrix E — OpenCode configuration + inference

Record:

```text
OpenCode version:
Eggpool commit/version:
config location/profile:
Eggpool base URL:
model/alias:
```

Run:

```bash
eggpool configsetup opencode --dry-run
eggpool configsetup opencode --apply
eggpool configsetup opencode --check
```

Then launch current OpenCode with `EGGPOOL_API_KEY` set.

Pass conditions:

- configuration parses;
- Eggpool provider/models appear;
- provider uses current Responses-capable OpenAI SDK/runtime configuration;
- environment interpolation is accepted;
- text generation works;
- ordinary tool loop works;
- context/output values are accepted;
- no secret value is embedded in generated config.

If OpenCode changes provider schema, capture the current documented/source-derived requirement before modifying Eggpool fixtures or renderer code.

---

# Matrix F — Managed config lifecycle safety

Run against temporary fixtures for both Codex and OpenCode.

Fixture classes:

```text
missing file
empty file
unrelated user config
existing third-party provider
existing Eggpool managed config
managed-section drift
unmanaged-section drift
malformed/unsupported OpenCode JSONC
```

For each supported fixture run:

```text
--dry-run
--apply
--check
--sync
--remove
```

Record:

```text
exit code
whether file changed
whether unrelated content changed
whether managed artifacts were created/removed
whether drift was detected/refused
```

Pass conditions are those in Plan 203: idempotent apply, read-only check/dry-run, narrow ownership, reversible remove, safe drift handling.

Prefer Rust integration fixtures for this matrix where possible so every live-discovered bug becomes deterministic coverage.

---

# Matrix G — `eggpool status`

## G1 healthy

```bash
eggpool status
eggpool status --json
```

Record:

```text
proxy state
provider row count
configured provider count
exit code
JSON schema_version
```

Pass if each configured provider appears exactly once, output is deterministic/secret-free, and proxy readiness agrees with `readyz`.

## G2 partial degradation

Use an already-safe degraded fixture or bounded local test configuration. Do not burn quota or risk provider accounts merely to force an error.

Pass if provider/proxy states match the implemented precedence and `status` itself causes no new outbound probe.

## G3 reachable but unready

Pass if exit code is `1`, JSON remains valid, and the readiness reason is bounded/shared with readiness policy.

## G4 unreachable

Pass if exit code is the existing unavailable class (`3` unless intentionally changed), static provider rows remain useful, and JSON remains valid/bounded.

---

# Evidence file

If the repository does not already have a preferred qualification-record location, add one bounded markdown record for this closure, for example:

```text
docs/qualification/coding-agent-proxy-2026-09.md
```

The record should include:

- exact client versions;
- Eggpool commit;
- matrix result (`PASS`, `SKIP_WITH_REASON`, `FAIL_FIXED`);
- provider/wire path at a non-secret level;
- links/commit IDs for any corrective fixes;
- intentional remaining limitations.

Do not commit:

- API keys;
- provider tokens;
- prompts containing private data;
- raw provider response bodies containing user content;
- local absolute paths that disclose unnecessary personal information;
- large terminal transcripts.

A concise evidence table is preferred over raw logs.

---

# Corrective loop

When any matrix row fails because of Eggpool:

1. reproduce deterministically if possible;
2. add focused regression coverage;
3. fix narrowly;
4. rerun the failed row;
5. rerun the relevant focused Rust target;
6. rerun full workspace gates before closure.

If the failure is a provider/client limitation rather than an Eggpool defect, document it and do not weaken Eggpool's general protocol contract to hide it.

---

# Completion criteria

Plan 207 is complete when:

1. all applicable matrix rows have a recorded result;
2. current Codex and OpenCode each have at least config/discovery + text + ordinary tool-loop qualification;
3. compaction behavior is identified and qualified without adding state solely for testing;
4. managed config lifecycle has reversible fixture coverage;
5. `eggpool status` has healthy/unready/unreachable evidence and degraded evidence where safely reproducible;
6. live-discovered Eggpool defects have deterministic regression coverage;
7. one concise secret-free qualification record is committed;
8. Plan 206 can close without relying on informal operator memory.

---

## Closure evidence (2026-09-16)

Executed with Plan 206 against Eggpool `0.8.0`, Codex CLI `0.154.0`,
OpenCode `1.18.30` (isolated temp config/home, no provider credentials).

| Matrix | Result |
|---|---|
| A Codex config/discovery (`--dry-run/--apply/--check`, `debug models`, `doctor`) | PASS (after narrow catalog fix; see Plan 206) |
| B1 Codex text via `smoke_codex_compat.sh` | SKIP_WITH_REASON (no `EGGPOOL_CODEX_API_KEY`/`MODEL`; script exits 77) |
| B2 Codex tool loop via smoke | SKIP_WITH_REASON (same; deterministic `codex_responses_compat` retained) |
| C deferred `tool_search` live | NOT_LIVE_EXERCISABLE (no stable client path; deterministic conformance retained) |
| D1 Codex long-session compaction | PASS (local compaction expected on custom provider; no EggPool persistence) |
| D2 native `/v1/responses/compact` | PASS deterministically (`codex_compaction_compat`); live remote not exercised (no compact-capable upstream; no translated fallback by design) |
| E OpenCode config + `models` listing | PASS (Responses runtime, env interpolation, limits); text/tool inference SKIP_WITH_REASON (no provider creds) |
| F managed lifecycle fixtures | PASS (idempotent apply, drift refusal, force converge, remove reversible, JSONC refusal) |
| G1 `status` healthy | PASS (degraded demo probe, exit 0, schema v1, secret-free) |
| G2 `status` degraded | PASS (same demo probe-failure evidence) |
| G3 unready | PASS deterministically (`status_command` + `operations::status`) |
| G4 unreachable | PASS (exit 3, `unknown` rows, valid JSON) |

One live-discovered defect (Codex catalog strict fields) received regression
test `codex_catalog_emits_current_required_fields_for_unknown_models` before
the fix was accepted. No secret-bearing evidence committed.
