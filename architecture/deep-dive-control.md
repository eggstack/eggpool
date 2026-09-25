# Deep Dive: Control Plane and Rehash

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md) and the generation lifecycle in
[Runtime](deep-dive-runtime.md).

## Reload policy

`rust/src/config_reload_policy.rs::classify_transition` is the single
reload-vs-restart authority. It is pure and deterministic: it compares two
validated configs and returns one redacted typed `ConfigTransition`
(unchanged, live-reloadable, or restart-required). Mixed changes are wholly
restart-required. `[integrations].advertise_base_url` is `Live`; binding,
database topology, `[server].threads`, and other disruptive resources are
restart-required. Diagnostics may display the redacted result; secrets never
enter it.

## Reload service

`rust/src/reload.rs` (`ReloadService`, process-owned with one lock and one
supervisor) coordinates server-side validation, canonical transition
classification, candidate construction, atomic publication, and retirement.
A candidate generation is complete before publication; failure leaves the
active generation intact. In-flight leases continue using their acquired
generation, while retiring generations drain finalization and background
work before resources close. `eggpool rehash` serializes reloads over the
control socket (see below); a restart-required change is reported before
publication, never partially applied.

## Control socket

`rust/src/operations/control.rs` owns the Unix-domain control endpoint and
client commands: `ControlError`/`ProtocolError`, `ControlRequest`
(`reload`, `parse_frame`), `ControlResponse` (`error`, `from_reload`),
`ControlClient` (`new`, `with_timeout`, `reload`, `send`), and
`ControlServerHandle` (`path`, `close`) via `start`. It serves
`rehash` and `runtime-status` queries. Control responses are bounded and
metadata-only. They report validation, publication, retirement, or busy
outcomes without secrets, request bodies, or raw provider responses.
Binding, database topology, and other restart-owned resources are rejected
as disruptive changes rather than partially applied.

## Config mutation

`rust/src/operations/config_mutation.rs` owns narrow atomic TOML edits:
`MutationError` (`TooLarge`, `Read`/`Write`, `Interrupted`, `Busy`,
`Config`, `Invalid`, `Template`/`TemplateParse`, `Control`, `Restart`),
`ApplyMode` (`LiveOrReport`, `RestartIfRunning`), `ApplyOutcome`
(`ServerNotRunning`, `RehashApplied`, `RehashNoop`,
`RestartRequired`, `ControlUnavailable`, `RehashFailed`, `Restarted`), and
`MutationResult<T>` carrying the pre-write `classify_transition` result
into apply logic. The line editor preserves unrelated comments and
sections; the typed config parser remains the final validation authority.
Restart-after-mutation is composed by `operations/lifecycle.rs`; the
runtime adapter only presents the outcome.

## Terminal selector

`rust/src/operations/terminal.rs` owns raw-mode lifecycle only, with no
config or provider semantics: `KeyAction` (`Next`, `Previous`, `Confirm`,
`Cancel`, `Ignore`, `Interrupted`), `EscapeOutcome`, `SelectError`
(`Io`, `Interrupted`, `NotInteractive`), `is_interactive` (true only when
both stdin and stdout are TTYs), `next_index`/`prev_index` (clamped,
non-wrapping), `classify_single`/`classify_escape_sequence`,
`render_menu`, and `select_one` with a deterministic line-oriented
fallback for non-TTY use. Behavior matches the documented contract:
`j/k` and Up/Down navigation, Enter to select, `q`/Esc to cancel.
`Ctrl-C` stays `Interrupted`, surfacing as `MutationError::Interrupted`
to `BootstrapError::Interrupted` (exit 130) behind one termios guard.
Provider/account selection lives in `config_mutation.rs`, which maps the
returned index back to its own domain.

## Process and paths

`rust/src/operations/process.rs` is the primitive owner for PID files,
probes, identity evidence, and signaling: `ProcessError`, `read_pid`,
`write_pid_atomic`, `clear_pid_if_matches`, `clear_stale_pid`,
`process_exists`, `ProcessIdentityProof` (`proves_eggpool`), `signal_term`,
`wait_for_exit`, `StartGuard`/`acquire_start_guard`, `HealthProbe`
(`probe_health`, `probe_readiness`), `ControlProbe` (`probe_control` over
the Unix socket), and `ProcessState` (`classify`).

`rust/src/operations/paths.rs` is the shared authority for config, data,
state, log, PID, and control-socket paths: `PathEnvironment` (aware of
`$EGGPOOL_CONFIG`, `$EGGPOOL_ENV`, `$EGGPOOL_RUNTIME_DIR`,
`$EGGPOOL_PID_FILE`/`$EGGPOOL_LOG_FILE`, XDG homes) and `RuntimePaths`
(`config_path`, `config_dir`, `data_dir`, `state_dir`, `env_path`,
`runtime_dir`, `pid_file`, `log_file`, `control_socket`) via
`resolve`/`prepare`/`resolve_with`. Production `/etc/eggpool`,
`/var/lib/eggpool`, `/var/log/eggpool` applies only when the resolved
config is the production path; otherwise the XDG personal layout applies.

## Lifecycle composition

`rust/src/operations/lifecycle.rs` composes the primitives above into safe
detached start, stop, restart, identity-proof, and watchdog workflows:
`LifecycleError`, `StopOutcome`, `RestartOutcome`,
`EnsureRunningOutcome`, `ensure_start_safe` (plus
`ensure_start_safe_with_listener` for port-conflict evidence),
`spawn_detached`, `stop`, `restart`, `restart_for_mutation`, and
`ensure_running`/`server_is_running`. `process.rs` remains the authority
for PID files, independent health/control evidence, and signaling; the CLI
(`rust/src/runtime.rs`) keeps prompts, presentation, and exit-code mapping.
