# Live Configuration Rehash

## Overview

`eggpool rehash` applies supported configuration changes to a running
EggPool server without a restart. The command validates the config
locally, contacts the running server's control socket, and the server
atomically swaps the active configuration generation when safe.

## Quick start

```bash
# Edit config, then apply supported LIVE changes
$EDITOR ~/.config/eggpool/config.toml
eggpool rehash

# Disruptive changes (host, port, database path, middleware) require a restart
eggpool restart
```

## Supported LIVE fields (closure pass)

The closure pass enables the following families of fields as `LIVE`:

- **Provider definitions and accounts**: ``[providers.<id>]`` blocks
  (base URL, protocols, headers, models endpoint, static models,
  authentication, routing priority) and ``[[providers.<id>.accounts]]``
  blocks (name, API key, weight, enabled flag). Adding, removing,
  or editing a provider or account publishes a new generation
  without restarting the process.
- **Routing and scoring knobs**: every field under ``[routing]``
  including strategy, fairness, scoring penalties, retry limits,
  quota advisory mode, and routing trace policy.
- **Model overrides and per-model capability overrides**:
  ``[model_overrides.<id>]`` and ``[model_capabilities.<id>]``.

### Closure pass D1 — request-policy expansion

The D1 milestone adds the request-path policy fields that the
candidate builder already constructs as generation-owned objects:

- **Transcoder policy** (``[transcoder]``) — every field including
  ``transcoder_loss_policy``, ``protocol_safety_mode``, and
  ``http_status_overrides``. A change rebuilds the generation's
  ``TranscoderPolicy`` and rewires the new ``RequestCoordinator``.
- **Compression policy** (``[compression]``, including
  ``[compression.synthetic_cache_controls]``) — every field.
  ``RuntimeCompressionPolicyOverrideRegistry`` is rebuilt per
  generation so per-provider overrides take effect without restart.
- **Cache synthesis controls** (``[cache]``,
  ``[cache.synthetic_cache_controls]``).
- **Models subset** (``[models]``) — ``expose_mode``,
  ``collapse_models``, ``refresh_interval_s``, ``stale_after_s``,
  ``allow_stale_catalog``. Startup-only fields
  (``startup_refresh``, ``ping_retain_days``,
  ``catalog_withdrawal_policy``) remain ``RESTART_REQUIRED``.
- **Security error-detail persistence**
  (``security.persist_redacted_error_detail``) — the flag is
  threaded into the candidate ``RequestCoordinator`` via the
  ``persist_error_detail=`` kwarg.

### Closure pass D2 — background and observability expansion

The D2 milestone adds background-task cadences and retention
durations as LIVE fields.  It introduces a dual-ownership model
via ``TaskOwnership`` (``src/eggpool/runtime_task_inventory.py``):

- **Process-owned** tasks (``checkpoint``, ``metrics_flush``,
  ``update_checker``, ``automatic_backup``) register on
  ``process.process_supervisor`` and survive generation swaps.
  Only one instance exists; reconfiguration mutates the schedule
  in place.
- **Generation-leased** tasks (``catalog_refresh``,
  ``retention_cleanup``) acquire a generation lease on
  every tick and are retired when their generation is retired.

D2 LIVE families:

- **Retention durations**: ``dashboard.retain_request_stats_days``,
  ``dashboard.retain_event_days``, ``models.ping_retain_days``.
- **Upstream timeout**: ``upstream.read_timeout_s`` and provider-bound
  ``providers.<id>.stream_timeouts`` are restart-required transport idle
  bounds. They never define a maximum stream lifetime or drive stale cleanup.
- **Metrics flush cadence**: ``metrics.flush_interval_s``.
- **Backup scheduling**: ``backup.enabled``, ``backup.interval_s``,
  ``backup.retain_count``, ``backup.startup_delay_s``.  Toggling
  ``enabled`` adds/removes the task.
- **Model-info scheduling**: ``model_info.enabled``,
  ``model_info.refresh_interval_s``.  Toggling ``enabled`` adds/removes
  the tasks; changing ``refresh_interval_s`` replaces the schedule.

The ``_run_periodic_loop`` in ``src/eggpool/background/__init__.py``
re-reads ``self._interval_s`` and ``self._initial_delay_s`` each
iteration so live interval changes take effect at the next tick
boundary — not from the last completion time.  For tasks changed via
``apply_spec_diff``, the old task is stopped and a new one is started
immediately with the new interval.  Toggling ``model_info.enabled`` or
``backup.enabled`` adds/removes the corresponding task; ``apply_spec_diff``
logs the transition (added/removed/changed) at INFO level.  Process-owned
task resources retain identity across reloads — no duplicated schedules,
no orphaned tasks.

``ProcessRuntime`` (``src/eggpool/runtime_manager.py``) now carries
``process_supervisor``, ``task_spec_version``, and
``last_task_transition`` fields.  Diagnostics are exposed under
``/api/stats/runtime`` via ``_snapshot_runtime_manager``.

Process-bound storage/deployment fields remain ``RESTART_REQUIRED``
(database path, backup destination paths that cross permission
boundaries, control socket).

Tests: ``tests/unit/test_runtime_task_inventory.py`` (35 tests),
``tests/unit/test_d2_transitions.py`` (15 tests), and
``tests/unit/test_runtime_tasks.py`` extended with
``TestProcessSupervisorRouting`` and
``TestProcessSupervisorSurvival``.

### Closure pass D3 — release validation and closure

D3 is a release-hardening milestone with no new LIVE fields.  It
closes security gaps in operational-event and CLI output redaction,
adds failure-injection and inventory-audit tests, and establishes
performance baselines.

**Security hardening** — ``_record_event`` (``src/eggpool/control/reload_manager.py:547``)
now passes the ``error`` payload through
``sanitize_text_for_audit()`` (``src/eggpool/config_reload_policy.py:348``)
before persisting to ``operational_events``.  The helper scrubs
secret-shaped substrings (``sk-*``, ``Bearer *``, ``key-*``, etc.)
via a compiled regex.  ``_redact_message``
(``src/eggpool/cli_rehash_format.py:65``) calls the same helper on
CLI output so human-readable messages never leak raw credential
values.  Previously only ``<old>``/``<new>`` placeholder tokens
were redacted; raw secret-shaped error text passed through.

**Test-only seams** — three instance attributes on
``ReloadManager`` (``src/eggpool/control/reload_manager.py:178-184``)
allow tests to inject deterministic failures at the start of a
named phase:

- ``TEST_INJECT_BUILD_FAILURE`` — raised at the start of
  ``_build_candidate_generation`` (line 635).
- ``TEST_INJECT_RECONCILE_FAILURE`` — raised at the start of
  ``_reconcile_persistence`` (line 1094).
- ``TEST_INJECT_PUBLISH_FAILURE`` — raised at the start of
  ``_publish_generation`` (line 1138).

Set the attribute on the ``ReloadManager`` instance before
triggering a reload; the exception is raised unconditionally at
the top of the named phase.

**Release-validation tests**:

| File | Count | Scope |
|------|-------|-------|
| ``tests/integration/test_rehash_d3_acceptance.py`` | 10 | end-to-end acceptance (invalid TOML, server-side validation, digest mismatch, background convergence, provider removal under active stream, mixed diff rejection, concurrent burst, retirement timeout, lease drain, pending-request leak) |
| ``tests/unit/test_reload_failure_injection.py`` | 12 | build/reconcile/publish failure injection via the three seams |
| ``tests/unit/test_reload_security.py`` | 35 | secret redaction in events, CLI output, ``<old>``/``<new>`` tokens, raw credential patterns, ``sanitize_text_for_audit`` edge cases |
| ``tests/unit/test_reload_inventory_audit.py`` | 5 | exhaustive audit of ``_FIELD_DISPOSITION`` coverage — caught and fixed ``dns_cache.ttl_seconds`` → ``network.dns_cache.positive_ttl_seconds`` and added 14 ``pricing.catalogs.*`` entries |
| ``tests/integration/test_rehash_d3_soak.py`` | 1 | Repeated alternating reloads asserting fresh per-resource identity |
| ``tests/perf/test_rehash_d3_performance.py`` | 3 | reload latency (p50 ≈ 480 ms, p95 ≈ 750 ms), memory-delta (skipped when ``psutil`` absent), concurrent-traffic p95 < 750 ms |
| ``tests/integration/test_rehash_d3_operator_workflow.py`` | 6 | operator happy-path, restart-required reject, JSON contract, no-op, dead-server exit 3, concurrent busy |

**Policy inventory audit** — the exhaustive field-by-field audit in
``tests/unit/test_reload_inventory_audit.py`` discovered two gaps
closed in D3:

1. ``dns_cache.ttl_seconds`` was missing from the inventory; the
   actual config path is ``network.dns_cache.positive_ttl_seconds``
   (plus six sibling fields under ``network.dns_cache``).
2. ``pricing.catalogs`` was missing 14 sub-path entries
   (``pricing.catalogs.<provider>.*`` for ``opencode_zen`` and
   ``openrouter``).  Both are ``RESTART_REQUIRED``.

The ``--json`` contract remains pinned at 9 keys (no new keys added
in D3); ``tests/unit/test_cli_rehash_format.py`` continues to lock
the contract.

Fields that stay ``RESTART_REQUIRED``:

- ``[upstream]`` — vestigial; only consulted at runtime-task
  startup. A future milestone would need to migrate the upstream
  registry into a generation-owned object.
- ``[model_info]`` — ``ModelInfoService`` is constructed in the
  FastAPI lifespan and is not rebuilt by the candidate manager.
  A future milestone must refactor the service to read from
  ``self._config``.

Mixed live + restart-required changes still reject entirely with
exit code `2`. Identity separation between active and candidate
policy objects is pinned by `TestMilestoneD1CandidateBuild` in
`tests/unit/test_reload_manager.py`. End-to-end behavioral reload
for each family is pinned by the `test_d1_*` tests in
`tests/integration/test_rehash_streaming_swap.py`.

All other fields remain ``RESTART_REQUIRED``: server binding
(host/port), Granian construction (threads, access log), database
path and topology, middleware (CORS, trusted hosts), metrics
storage topology, security header construction, and process-owned
background-task cadences. These need a full restart because the
runtime builds them once at startup.

When a `rehash` includes a mix of LIVE and RESTART_REQUIRED changes
the entire reload is rejected (no partial application), and the CLI
returns exit code `2` so scripts can detect the situation.

## Complete reload flow

The reload follows a strict transactional pipeline:

1. **Local validation** — CLI validates the config file via
   `validate_config_file()`. Invalid configs are rejected immediately
   (fail-closed); the running config is never touched.
2. **Control socket** — CLI connects to the Unix-domain socket and
   sends the validated `content_digest` (SHA-256 of the config bytes).
3. **Server-side re-validation** — The server independently re-validates
   the config to guard against TOCTOU drift.
4. **Diff** — Server computes a `ConfigDiff` against the active
   generation's config. Expanded per-key paths
   (``providers.<id>``, ``accounts.<provider>/<name>``) inherit the
   LIVE disposition of their parent collection.
5. **Restart-required check** — Any field with `RESTART_REQUIRED`
   disposition causes the entire operation to be rejected. The server
   lists the offending fields in the response.
6. **Candidate generation** — A new `RuntimeGeneration` is built
   (router, DB connections, app state) without touching the active one.
7. **Persistence reconciliation** — Database state is reconciled in a
   transaction; failures trigger rollback.
8. **Atomic publication** — The new generation is installed via
   `RuntimeManager`; new requests immediately use it.
9. **Old generation retirement** — Active streams continue on their
   original generation. Old resources close only after all leases
   drain (timeout: 300s default).

## Control socket

| Property | Value |
|----------|-------|
| Path | `~/.local/state/eggpool/eggpool.sock` |
| Permissions | `0o600` (owner-only read/write) |
| Protocol | Newline-delimited JSON v1 |
| Max request size | 64 KB |
| Command timeout | 30s |

The socket is created on server startup and cleaned up on stop. Stale
socket files are automatically removed before binding.

### Wire format (protocol v1)

Request (one JSON object per line):

```json
{
  "protocol_version": 1,
  "request_id": "<uuid>",
  "command": "reload_config",
  "validated_digest": "<sha-256-hex>"
}
```

Response (one JSON object per line):

```json
{
  "protocol_version": 1,
  "request_id": "<uuid>",
  "ok": true,
  "stage": "commit",
  "exit_code": 0,
  "generation": 3,
  "changed_sections": ["routing", "accounts"],
  "warnings": [],
  "restart_required": [],
  "retirement_pending": false,
  "message": "rehash applied"
}
```

## `eggpool rehash` command behavior

The CLI performs these steps:

1. **Preflight validation** — Runs `validate_config_file()` against
   the config file. On failure, prints a clear error and exits
   non-zero. The running configuration is never touched.
2. **Connect to control socket** — Sends the validated
   `content_digest` to the running server. If the server is not
   running, prints "Control socket unavailable. Is the server running?"
   and exits with code `3`.
3. **Render result** — On success, prints changed sections and the
   new generation number. On failure, lists restart-required fields.

### Exit codes

The `rehash` command returns stable exit codes that scripts and
deployment tooling can rely on:

| Exit code | Meaning |
|-----------|---------|
| `0` | Applied, no-op, or ignored-only success |
| `1` | Validation / configuration failure |
| `2` | Restart required (one or more fields need a full restart) |
| `3` | Control socket unavailable (server not running) |
| `4` | Reload conflict / busy (`reload_in_progress` stage — another reload in progress) |
| `5` | Candidate preparation or publication failure |
| `6` | Digest mismatch between CLI preflight and server read |

The constants are pinned in `src/eggpool/cli_exit_codes.py`. Every
`--json` response always includes the `exit_code` key so programmatic
consumers do not need to map stages themselves.

### JSON output

For programmatic consumers (systemd hooks, configuration-management
tooling, future `connect`/`logout` automation) pass `--json`:

```bash
eggpool --config /etc/eggpool/config.toml rehash --json
```

Outputs the structured response as JSON on stdout, suitable for
`jq` pipelines. Secrets are redacted as `"<changed>"` in any
displayed fields.

Every outcome (success, failure, busy, no-op) always includes these
9 keys:

| Key | Type | Description |
|-----|------|-------------|
| `ok` | `bool` | Whether the reload succeeded |
| `stage` | `str` | Server stage at outcome (e.g. `commit`, `validation`, `reload_in_progress`) |
| `exit_code` | `int` | Stable exit code (0–6); mirrors the process exit code |
| `generation` | `int \| None` | New generation number on success, `None` on failure |
| `changed_sections` | `list[str]` | Config sections that changed (e.g. `["routing", "accounts"]`) |
| `warnings` | `list[str]` | Non-fatal warnings emitted during the reload |
| `restart_required` | `list[str]` | Fields that need a restart (empty on success) |
| `retirement_pending` | `bool` | `True` when the old generation is still draining |
| `message` | `str` | Human-readable summary |

The `format_rehash_json()` function in `src/eggpool/cli_rehash_format.py`
is the single source of truth; tests in
`tests/unit/test_cli_rehash_format.py` pin the contract.

### Error messages

| Message | Exit code | Meaning |
|---------|-----------|---------|
| "Live configuration is unchanged. Refusing to apply an invalid config…" | `1` | Local validation failed; config was not sent to the server |
| "Control socket unavailable. Is the server running?" | `3` | Server is not running; use `eggpool restart` |
| "Control socket request timed out." | `3` | Server did not respond within 30s |
| "A reload transaction is already in progress" | `4` | Concurrent `rehash` rejected; only one reload at a time |
| "Restart-required changes: …" | `2` | One or more fields require a restart; use `eggpool restart` |
| "Configuration refresh rejected; active configuration is unchanged." | varies | Validation/preparation failure; old config is untouched |

## Digests and fingerprints

The validation result carries two distinct hashes:

| Hash | Purpose |
|------|---------|
| `content_digest` | SHA-256 of the exact config file bytes. Guards against time-of-check / time-of-use drift. The CLI sends this to the server so it re-validates the same bytes. |
| `runtime_fingerprint` | Deterministic, secret-safe canonical hash. Secret fields (API keys, tokens) are redacted to `"<redacted>"` before hashing. Used for no-op detection ("is the running config identical to the new one?") and diagnostics. |

## Reload policy

Every `AppConfig` field is classified in the `_FIELD_DISPOSITION` map
in `config_reload_policy.py`:

| Disposition | Meaning |
|-------------|---------|
| `LIVE` | Can be hot-swapped without a restart |
| `RESTART_REQUIRED` | Changing the field requires a service restart |
| `IGNORED` | Field is ignored for reload purposes |

The closure pass enables provider/account/routing/model-override
families as `LIVE`. The D1 expansion extends the inventory to
request-policy fields: the entire `[transcoder]`, `[compression]`,
and `[cache]` blocks (including `[compression.synthetic_cache_controls]`
and `[cache.synthetic_cache_controls]`), the runtime-tunable subset
of `[models]` (`expose_mode`, `collapse_models`, `refresh_interval_s`,
`stale_after_s`, `allow_stale_catalog`), and
`security.persist_redacted_error_detail`. The D2 expansion adds
background-task cadences and retention durations:
`dashboard.retain_request_stats_days`, `dashboard.retain_event_days`,
`models.ping_retain_days`, `upstream.read_timeout_s`,
`metrics.flush_interval_s`, `backup.interval_s`, `backup.retain_count`,
and `backup.startup_delay_s`. Every other field remains
`RESTART_REQUIRED` because it is owned by the supervisor process
(Granian construction, DB connection, middleware, JSON backend,
deployment paths, `[upstream]` registry, `[model_info]` service
construction). When reviewing a future change, move the
corresponding entry to `LIVE` in the same diff that introduces the
live replacement path.

The default for any field not listed is `RESTART_REQUIRED` so a new
field can never silently slip through unclassified.

### Generating the live/restart table

The policy map is the single reviewable inventory.  To print a
live/restart summary from a checkout:

```python
from eggpool.config_reload_policy import _FIELD_DISPOSITION
for path, disp in sorted(_FIELD_DISPOSITION.items()):
    print(f"{disp.value:18s} {path}")
```

To regenerate this section in the future, re-run the snippet above
and replace the table.

## Transaction safety

- **Serialized** — One reload at a time. Concurrent `rehash` commands
  are rejected with `reload_in_progress`.
- **Content digest** — Prevents TOCTOU races: the server re-validates
  the exact bytes the CLI validated.
- **Fail-closed** — All failures before publication are rolled back.
  The old generation remains active.
- **No secrets in logs** — Secret fields render as `<changed>` in
  diagnostic output.

## Connect and logout fallback policy

`eggpool connect` and `eggpool logout` route through the same
validate-and-reload helper (`cli_rehash_helper.validate_and_rehash`)
that `eggpool rehash` uses. The safe-fallback decision tree in
`resolve_apply_outcome()` (`src/eggpool/providers/connect.py`) is:

1. **Validate locally** — invalid config → return immediately, no
   restart attempted.
2. **Probe the control socket** — if the server accepts the reload,
   apply live.
3. **Server healthy but socket missing** — return
   `(False, "control unavailable (server healthy)")` **without
   restarting**. The operator must intervene explicitly (check file
   permissions, restart the service, or investigate why the control
   socket disappeared).
4. **Server not running** — fall through to `restart_server()` so the
   change still applies on the next startup.

The old `apply_or_restart()` is now a thin wrapper that delegates
to `resolve_apply_outcome()` when `prefer_live=True` (the default).

## Operator-safe fallback cases

| Scenario | Behavior | Operator action |
|----------|----------|-----------------|
| Stale PID file, server dead | `restart_server()` fires | None — automatic |
| Server healthy, control socket missing | No restart; `(False, "control unavailable (server healthy)")` | Check socket permissions, restart the service |
| Server healthy, control socket reachable | Live rehash applied | None — automatic |
| Server dead, socket missing | `restart_server()` fires | None — automatic |
| Permission denied on socket | Control client error; exit 3 | Run as the same user as the server |

## Old generation retirement

After publication, active streams continue on their original
generation. New requests immediately use the new generation. The old
generation is retired after all leases drain (default timeout: 300s).
During retirement, `retirement_pending: true` appears in the response.

## Troubleshooting

### Server is not running

```
Control socket unavailable. Is the server running?
Use `eggpool restart` for a disruptive configuration reload.
```

The server must be running for `eggpool rehash` to work. Start it
with `eggpool serve` (daemon mode) or via systemd.

### Concurrent reload in progress

```
A reload transaction is already in progress
```

Only one reload can execute at a time. Wait for the current reload to
complete, then retry.

### Restart-required changes

```
Restart-required changes:
  - server.host
  - server.port
```

These fields cannot be changed without a process restart. Use
`eggpool restart` (or `systemctl restart eggpool`).

### Socket permissions error

The socket is created with `0o600` (owner-only). If you see permission
errors, ensure you are running `eggpool rehash` as the same user that
owns the running server process.

### Stale socket file

Stale socket files are automatically cleaned up on server start. If
you see connection errors after a crash, try `eggpool restart` to
ensure a clean socket state.

### Server is healthy but socket missing

```
Control socket unavailable. Is the server running?
```

If `eggpool rehash` (or `connect`/`logout`) reports this but the
server is actually running (e.g. `systemctl status eggpool` shows it
active), the control socket file may have been removed or its
permissions changed. The safe-fallback policy does **not** auto-restart
a healthy server — the operator must intervene:

1. Check that `~/.local/state/eggpool/eggpool.sock` exists and is
   owned by the same user as the server process.
2. If the socket was accidentally removed, restart the server:
   `eggpool restart` (or `systemctl restart eggpool`).
3. Verify permissions: `ls -la ~/.local/state/eggpool/eggpool.sock`
   should show `srw-------` (socket, owner read/write only).

## See also

- `architecture/README.md` § Live Configuration Rehash — validation
  contract, diff shape, wire types, and runtime generations
- `src/eggpool/config_validation.py` — reusable validation contract
- `src/eggpool/config_reload_policy.py` — typed diff and reload policy
- `src/eggpool/cli_rehash_format.py` — standardized JSON and human output
- `src/eggpool/cli_exit_codes.py` — stable exit code constants
- `src/eggpool/control/server.py` — control socket server
- `src/eggpool/control/client.py` — control socket client
- `src/eggpool/control/reload_manager.py` — transactional reload manager
- `src/eggpool/providers/connect.py` — safe connect/logout fallback policy
