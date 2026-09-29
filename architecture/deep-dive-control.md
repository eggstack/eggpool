# Deep Dive: Control Plane and Rehash

Back to [Architecture](README.md). See also the review index in [overview.md §11](overview.md) and the generation lifecycle in [Runtime](deep-dive-runtime.md).

## Ownership

- `rust/src/config_reload_policy.rs::classify_transition` is the single reload-vs-restart authority. `rust/src/reload.rs` (`ReloadService`) is the single publication authority. `rust/src/operations/control.rs` is a transport adapter only: it owns neither diff, staging, publication, nor diagnostics.
- `rust/src/operations/lifecycle.rs` composes process workflows; `rust/src/operations/process.rs` owns PID/probe/signal primitives; `rust/src/operations/paths.rs` owns path resolution; `rust/src/operations/config_mutation.rs` owns atomic TOML edits; `rust/src/operations/terminal.rs` owns raw-mode selection only.

## Reload policy

`classify_transition` is pure and deterministic over two validated configs. It returns one redacted typed `ConfigTransition` (unchanged, live-reloadable, or restart-required) with secret-scrubbed displays. Mixed live plus restart-required changes are wholly restart-required. `[integrations].advertise_base_url` is `Live`: it changes client-facing profile output only, never the listen socket. Binding (`server.host`/`port`), database topology, `[server].threads`, proxies, and other disruptive resources are restart-required via `disposition_for` / `FIELD_DISPOSITIONS`, with blanket live rules for `providers.*`, `accounts.*`, `model_overrides.*`, `model_capabilities.*`, and `transcoder.*`.

## Reload service

`ReloadService::new(process, manager)` binds one process runtime to one active-generation manager over the process-owned reload lock. `reload` runs owned work in a spawned task so caller cancellation never drops a staged candidate or leaves admission unresolved. The transaction order is: read and digest input, `verify_expected_digest`, parse/validate, `classify_transition` (noop and restart-required return before publication), prepare the provider/account persistence delta, build the complete candidate via `RuntimeGenerationFactory::prepare_with_durable_accounts`, preflight the task diff, `stage`, apply persistence in a caller-owned `DatabaseTransaction`, `commit_pointer`, commit tasks, commit the SQLite transaction, then `accept` (or `accept_during_shutdown`). Post-commit acceptance failure goes fail-closed (`fail_closed`), never restoring the old pointer. `eggpool rehash` serializes reloads through the control socket below.

## Control socket

`rust/src/operations/control.rs` owns the Unix-domain JSON protocol, one request per connection, `PROTOCOL_VERSION = 1`, `MAX_REQUEST_BYTES = 65_536`, `CONTROL_TIMEOUT = 30s`:

- `ControlRequest` (`reload`, `parse_frame`): the only accepted command is `reload_config`, with an optional 64-char lowercase hex `validated_digest`. Depth pre-checks bound parsing before DOM allocation.
- `ControlResponse` (`error`, `from_reload`): bounded metadata-only outcomes (stage, generation, changed sections, restart-required paths, retirement flag); no secrets, bodies, or provider error text.
- `ControlClient` (`new`, `with_timeout`, `reload`, `send`): typed one-shot client used by `rehash`.
- `ControlServerHandle` (`path`, `close`) via `start`: sole process-local listener, socket mode `0o600`, runtime dir `0o700`, stale-socket identity checks; `close` unlinks only its own socket while a retained reload may still finish.

The control plane carries reload only. `runtime-status` is not a control command: `runtime.rs::fetch_runtime_status` reads the authenticated `GET /api/stats/runtime` projection (`server/health.rs::runtime_status`), which renders `RuntimeDiagnosticsSnapshot`.

## Config mutation

`rust/src/operations/config_mutation.rs` owns narrow atomic TOML edits: `MutationError` (`TooLarge`, `Read`/`Write`, `Interrupted`, `Busy`, `Config`, `Invalid`, `Template`/`TemplateParse`, `Control`, `Restart`), `ApplyMode` (`LiveOrReport`, `RestartIfRunning`), `ApplyOutcome` (`ServerNotRunning`, `RehashApplied`, `RehashNoop`, `RestartRequired`, `ControlUnavailable`, `RehashFailed`, `Restarted`), and `MutationResult<T>` carrying the pre-write `classify_transition` result into apply logic. The line editor preserves unrelated comments and sections; the typed parser is the final validation authority. Restart-after-mutation is composed by `operations/lifecycle.rs`; the runtime adapter only presents the outcome.

## Terminal selector

`rust/src/operations/terminal.rs` owns raw-mode lifecycle only: `KeyAction` (`Next`, `Previous`, `Confirm`, `Cancel`, `Ignore`, `Interrupted`), `EscapeOutcome`, `SelectError` (`Io`, `Interrupted`, `NotInteractive`), `is_interactive` (true only when stdin and stdout are both TTYs), `next_index`/`prev_index` (clamped, non-wrapping), `classify_single`/`classify_escape_sequence`, `render_menu`, `select_one` with a deterministic line-oriented fallback. Behavior: `j/k` and Up/Down navigate, Enter selects, `q`/Esc cancels, `Ctrl-C` stays `Interrupted` (surfacing as `MutationError::Interrupted` to `BootstrapError::Interrupted`, exit 130). Provider/account selection lives in `config_mutation.rs`.

## Process and paths

`rust/src/operations/process.rs` is the primitive owner: `ProcessError`, `read_pid`, `write_pid_atomic`, `clear_pid_if_matches`, `clear_stale_pid`, `process_exists`, `ProcessIdentityProof` (`proves_eggpool`), `signal_term` (TERM only with identity proof), `wait_for_exit` / `wait_for_exit_or_pid_clear`, `StartGuard`/`acquire_start_guard`, `HealthProbe` (`probe_health`, `probe_readiness`), `ControlProbe` (`probe_control`), `ProcessState` (`classify`).

`rust/src/operations/paths.rs` is the shared authority: `PathEnvironment` (`$EGGPOOL_CONFIG`, `$EGGPOOL_ENV`, `$EGGPOOL_RUNTIME_DIR`, `$EGGPOOL_PID_FILE`/`$EGGPOOL_LOG_FILE`, XDG homes) and `RuntimePaths` (`config_path`, `config_dir`, `data_dir`, `state_dir`, `env_path`, `runtime_dir`, `pid_file`, `log_file`, `control_socket`) via `resolve`/`resolve_with`/`prepare`. Production `/etc/eggpool`, `/var/lib/eggpool`, `/var/log/eggpool` applies only when the resolved config is the production path; otherwise the XDG personal layout applies.

## Lifecycle composition

`rust/src/operations/lifecycle.rs` composes safe detached start, stop, restart, identity-proof, and watchdog workflows: `LifecycleError`, `StopOutcome`, `RestartOutcome`, `EnsureRunningOutcome`, `ensure_start_safe` (plus `ensure_start_safe_with_listener` for port-conflict evidence), `spawn_detached`, `stop`, `restart`, `restart_for_mutation` (never starts a stopped service as a side effect), `ensure_running`/`server_is_running`.

## Invariants

- Policy classifies; the reload service publishes; the socket only transports.
- Restart-required input is reported before publication, never partially applied.
- A failed candidate leaves the active generation and process-owned state unchanged.
- Reload diagnostics are owned by the reload operation, never by a caller that may finish early.
