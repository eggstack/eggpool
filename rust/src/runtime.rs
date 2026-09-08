use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    process::{Command as OsCommand, Stdio},
    time::Duration,
};

use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time::{Instant, sleep, timeout},
};

use crate::{
    BootstrapError, Cli, Command,
    cli::ServeArgs,
    config,
    operations::{paths::RuntimePaths, process},
    version::PACKAGE_VERSION,
};

const EXIT_VALIDATION: u8 = 1;
const EXIT_RESTART_REQUIRED: u8 = 2;
const EXIT_CONTROL_UNAVAILABLE: u8 = 3;
const EXIT_RELOAD_BUSY: u8 = 4;
const EXIT_PREPARATION_FAILED: u8 = 5;
const EXIT_DIGEST_MISMATCH: u8 = 6;
const MAX_STATUS_BODY_BYTES: usize = 1_048_576;
const STATUS_TIMEOUT: Duration = Duration::from_secs(5);
const WATCHDOG_START_TIMEOUT: Duration = Duration::from_secs(2);

/// Initialize process-local diagnostics and dispatch the operational CLI.
pub async fn run(cli: Cli) -> Result<(), BootstrapError> {
    let _ = tracing_subscriber::fmt()
        .with_target(false)
        .with_ansi(false)
        .try_init();
    tracing::debug!(
        version = PACKAGE_VERSION,
        "Rust migration candidate initialized"
    );

    let config_path = cli.resolved_config_path();
    match cli.command {
        Some(Command::Version) => println!("{PACKAGE_VERSION}"),
        Some(Command::CheckConfig) => check_config(&config_path)?,
        Some(Command::Serve(args)) => serve(&config_path, &args).await?,
        Some(Command::Stop(args)) => stop(&config_path, args.timeout).await?,
        Some(Command::Restart(args)) => restart(&config_path, args.timeout).await?,
        Some(Command::Rehash { json }) => rehash(&config_path, json).await?,
        Some(Command::RuntimeStatus { json }) => runtime_status(&config_path, json).await?,
        Some(Command::Croncheck) => croncheck(),
        Some(Command::EnsureRunning) => ensure_running(&config_path).await?,
        None => {
            println!("{}", crate::cli::help_text());
            println!("\nConfig file: {}", config_path.display());
        }
        Some(Command::Help) => println!("{}", crate::cli::help_text()),
        Some(command) => {
            return Err(BootstrapError::NotImplemented {
                command: command.unavailable_name().to_string(),
            });
        }
    }
    Ok(())
}

fn command_error(code: u8, detail: impl Into<String>) -> BootstrapError {
    BootstrapError::Command {
        code,
        detail: detail.into(),
    }
}

fn check_config(path: &Path) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    let digest = config::content_digest(path)?;
    println!(
        "Configuration loaded successfully from {}\n  Server: {}:{}\n  Accounts: {}\n  Database: {}\n  Content digest: {}",
        path.display(),
        config.server.host,
        config.server.port,
        config.all_accounts().len(),
        config.database.path,
        digest
    );
    Ok(())
}

async fn serve(path: &Path, args: &ServeArgs) -> Result<(), BootstrapError> {
    validate_root(args.as_root)?;
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    warn_without_accounts(&config);
    if args.verbose {
        let digest = config::content_digest(path)?;
        crate::server::run_with_digest(config, digest, Some(path.to_path_buf()))
            .await
            .map_err(server_error)
    } else {
        ensure_start_safe(path, &config).await?;
        let child = spawn_detached(path, args.log_file.as_deref(), args.quiet)?;
        let paths = RuntimePaths::resolve();
        let log = if args.quiet && args.log_file.is_none() {
            "/dev/null".to_owned()
        } else {
            args.log_file.as_deref().map_or_else(
                || paths.log_file.display().to_string(),
                |path| path.display().to_string(),
            )
        };
        println!(
            "Server spawned (PID {}).\n  Log: {}\n  PID file: {}",
            child.id(),
            log,
            paths.pid_file.display()
        );
        Ok(())
    }
}

fn server_error(error: crate::server::ServerError) -> BootstrapError {
    BootstrapError::Server {
        detail: error.to_string(),
    }
}

fn validate_root(as_root: bool) -> Result<(), BootstrapError> {
    #[cfg(unix)]
    if nix::unistd::geteuid().is_root() && !as_root {
        return Err(command_error(
            EXIT_VALIDATION,
            "refusing to run as root for personal deployment; pass --as-root for an intentional system install",
        ));
    }
    Ok(())
}

fn warn_without_accounts(config: &config::Config) {
    if config.all_accounts().is_empty() {
        eprintln!(
            "Warning: No provider accounts configured.\n  Use `eggpool connect` to add a provider, then restart."
        );
    }
}

async fn ensure_start_safe(_path: &Path, config: &config::Config) -> Result<(), BootstrapError> {
    let paths = RuntimePaths::prepare().map_err(path_error)?;
    let pid = process::read_pid(&paths.pid_file).map_err(process_error)?;
    if let Some(pid) = pid {
        if process::process_exists(pid) {
            return Err(command_error(
                EXIT_VALIDATION,
                format!("server is already running (PID {pid})"),
            ));
        }
        process::clear_stale_pid(&paths.pid_file, Some(pid)).map_err(process_error)?;
    }
    if process::probe_health(&config.server.host, config.server.port).await
        == process::HealthProbe::Healthy
    {
        return Err(command_error(
            EXIT_VALIDATION,
            format!(
                "another process is already serving {}:{}",
                config.server.host, config.server.port
            ),
        ));
    }
    Ok(())
}

fn process_error(error: process::ProcessError) -> BootstrapError {
    command_error(EXIT_VALIDATION, error.to_string())
}

fn path_error(error: crate::operations::paths::PathError) -> BootstrapError {
    process_error(process::ProcessError::Path(error))
}

fn open_log(path: &Path) -> Result<File, BootstrapError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            command_error(
                EXIT_VALIDATION,
                format!("cannot prepare log directory: {error}"),
            )
        })?;
    }
    let mut options = OpenOptions::new();
    options.create(true).append(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(path)
        .map_err(|error| command_error(EXIT_VALIDATION, format!("cannot open log file: {error}")))
}

fn spawn_detached(
    config_path: &Path,
    log_path: Option<&Path>,
    quiet: bool,
) -> Result<std::process::Child, BootstrapError> {
    let executable = std::env::current_exe().map_err(|error| {
        command_error(
            EXIT_VALIDATION,
            format!("cannot resolve eggpool executable: {error}"),
        )
    })?;
    let resolved_config = fs::canonicalize(config_path).map_err(|error| {
        command_error(
            EXIT_VALIDATION,
            format!("cannot resolve config path: {error}"),
        )
    })?;
    let paths = RuntimePaths::prepare().map_err(path_error)?;
    let explicit_log = log_path.map(PathBuf::from);
    let log_target = if quiet && explicit_log.is_none() {
        None
    } else {
        if explicit_log.is_none() {
            paths
                .ensure_state_dir()
                .map_err(|error| command_error(EXIT_VALIDATION, error.to_string()))?;
        }
        Some(explicit_log.unwrap_or(paths.log_file))
    };
    let (stdout, stderr) = if let Some(log_path) = log_target.as_deref() {
        let file = open_log(log_path)?;
        let duplicate = file.try_clone().map_err(|error| {
            command_error(
                EXIT_VALIDATION,
                format!("cannot duplicate log handle: {error}"),
            )
        })?;
        (Stdio::from(file), Stdio::from(duplicate))
    } else {
        (Stdio::null(), Stdio::null())
    };
    let mut command = OsCommand::new(executable);
    command
        .arg("--config")
        .arg(resolved_config)
        .arg("serve")
        .arg("--verbose")
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(stderr);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command
        .spawn()
        .map_err(|error| command_error(EXIT_VALIDATION, format!("failed to spawn server: {error}")))
}

async fn stop(path: &Path, timeout_seconds: f64) -> Result<(), BootstrapError> {
    let timeout = lifecycle_timeout(timeout_seconds)?;
    let paths = RuntimePaths::resolve();
    let Some(pid) = process::read_pid(&paths.pid_file).map_err(process_error)? else {
        println!("Server is not running (no PID file found).");
        return Ok(());
    };
    if !process::process_exists(pid) {
        process::clear_stale_pid(&paths.pid_file, Some(pid)).map_err(process_error)?;
        println!("Server is not running (stale PID file).");
        return Ok(());
    }
    let proof = identity_proof(path, &paths, pid).await;
    if !proof.proves_eggpool() {
        return Err(command_error(
            EXIT_VALIDATION,
            "refusing to signal PID without independent EggPool identity evidence",
        ));
    }
    println!("Stopping server (PID {pid})...");
    process::signal_term(pid, proof).map_err(process_error)?;
    if !process::wait_for_exit(pid, timeout).await {
        return Err(command_error(
            EXIT_VALIDATION,
            format!("server did not stop within {timeout_seconds}s"),
        ));
    }
    process::clear_pid_if_matches(&paths.pid_file, pid).map_err(process_error)?;
    println!("Server stopped.");
    Ok(())
}

async fn restart(path: &Path, timeout_seconds: f64) -> Result<(), BootstrapError> {
    let timeout = lifecycle_timeout(timeout_seconds)?;
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    let paths = RuntimePaths::resolve();
    if let Some(pid) = process::read_pid(&paths.pid_file).map_err(process_error)? {
        if process::process_exists(pid) {
            let proof = identity_proof(path, &paths, pid).await;
            if !proof.proves_eggpool() {
                return Err(command_error(
                    EXIT_VALIDATION,
                    "refusing to restart an unproven process associated with the PID file",
                ));
            }
            println!("Stopping server (PID {pid})...");
            process::signal_term(pid, proof).map_err(process_error)?;
            if !process::wait_for_exit(pid, timeout).await {
                return Err(command_error(
                    EXIT_VALIDATION,
                    format!(
                        "server did not stop within {timeout_seconds}s; replacement was not started"
                    ),
                ));
            }
            process::clear_pid_if_matches(&paths.pid_file, pid).map_err(process_error)?;
        } else {
            process::clear_stale_pid(&paths.pid_file, Some(pid)).map_err(process_error)?;
        }
    }
    if process::probe_health(&config.server.host, config.server.port).await
        == process::HealthProbe::Healthy
    {
        return Err(command_error(
            EXIT_VALIDATION,
            "replacement was not started because the configured listener is still healthy",
        ));
    }
    let child = spawn_detached(path, None, false)?;
    println!("Server started (PID {}).", child.id());
    Ok(())
}

async fn identity_proof(
    path: &Path,
    paths: &RuntimePaths,
    pid: i32,
) -> process::ProcessIdentityProof {
    let health = match config::Config::from_toml(path) {
        Ok(config) => process::probe_health(&config.server.host, config.server.port).await,
        Err(_) => process::HealthProbe::Unreachable,
    };
    let control = process::probe_control(&paths.control_socket).await;
    process::ProcessIdentityProof {
        pid_file_matches: process::read_pid(&paths.pid_file).ok().flatten() == Some(pid),
        health,
        control_socket_reachable: control == process::ControlProbe::Reachable,
    }
}

fn lifecycle_timeout(seconds: f64) -> Result<Duration, BootstrapError> {
    if !seconds.is_finite() || seconds <= 0.0 {
        return Err(command_error(
            EXIT_VALIDATION,
            "timeout must be finite and greater than zero",
        ));
    }
    Ok(Duration::from_secs_f64(seconds))
}

async fn rehash(path: &Path, json_output: bool) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    config.validate_account_credentials()?;
    let digest = config::content_digest(path)?;
    let response = process_control_client()
        .reload(Some(digest))
        .await
        .map_err(|error| render_control_error(error, json_output))?;
    let code = reload_exit_code(&response);
    if json_output {
        let value = json!({"ok": response.ok, "stage": response.stage, "exit_code": code, "generation": response.generation, "changed_sections": response.changed_sections, "warnings": response.warnings, "restart_required": response.restart_required, "retirement_pending": response.retirement_pending, "message": response.message});
        println!(
            "{}",
            serde_json::to_string_pretty(&value).expect("JSON rendering")
        );
    } else if response.ok {
        println!("\n{}", response.message);
        if !response.changed_sections.is_empty() {
            println!(
                "  Changed sections: {}",
                response.changed_sections.join(", ")
            );
        }
        if let Some(generation) = response.generation {
            println!("  Generation: {generation}");
        }
        if response.retirement_pending {
            println!(
                "  Old generation is draining; active requests will complete on their original configuration."
            );
        }
    } else {
        eprintln!("\n{}", response.message);
        if !response.restart_required.is_empty() {
            eprintln!("  Restart-required changes:");
            for field in &response.restart_required {
                eprintln!("    - {field}");
            }
        }
    }
    if code == 0 {
        Ok(())
    } else {
        Err(command_error(code, response.message))
    }
}

fn process_control_client() -> crate::operations::control::ControlClient {
    crate::operations::control::ControlClient::new(RuntimePaths::resolve().control_socket)
}

fn render_control_error(
    error: crate::operations::control::ControlClientError,
    json_output: bool,
) -> BootstrapError {
    let detail = error.to_string();
    if json_output {
        let stage = if matches!(
            error,
            crate::operations::control::ControlClientError::Timeout
        ) {
            "timeout"
        } else {
            "error"
        };
        println!("{}", serde_json::to_string_pretty(&json!({"ok": false, "stage": stage, "exit_code": EXIT_CONTROL_UNAVAILABLE, "generation": null, "changed_sections": [], "warnings": [], "restart_required": [], "retirement_pending": false, "message": detail})).expect("JSON rendering"));
    } else {
        eprintln!("\nControl socket unavailable: {detail}");
        eprintln!("Use `eggpool restart` for a disruptive configuration reload.");
    }
    command_error(EXIT_CONTROL_UNAVAILABLE, detail)
}

fn reload_exit_code(response: &crate::operations::control::ControlResponse) -> u8 {
    if response.ok {
        return 0;
    }
    if !response.restart_required.is_empty() {
        return EXIT_RESTART_REQUIRED;
    }
    if response
        .message
        .to_ascii_lowercase()
        .contains("digest mismatch")
    {
        return EXIT_DIGEST_MISMATCH;
    }
    match response.stage.as_str() {
        "reload_in_progress" => EXIT_RELOAD_BUSY,
        "preparation" | "reconciliation" | "commit" | "activation" => EXIT_PREPARATION_FAILED,
        _ => EXIT_VALIDATION,
    }
}

fn croncheck() {
    let paths = RuntimePaths::resolve();
    let healthy = process::read_pid(&paths.pid_file)
        .ok()
        .flatten()
        .is_some_and(process::process_exists);
    std::process::exit(if healthy { 0 } else { 1 });
}

async fn ensure_running(path: &Path) -> Result<(), BootstrapError> {
    let paths = RuntimePaths::prepare().map_err(path_error)?;
    if process::read_pid(&paths.pid_file)
        .map_err(process_error)?
        .is_some_and(process::process_exists)
    {
        return Ok(());
    }
    let _ = process::clear_stale_pid(&paths.pid_file, None).map_err(process_error)?;
    let guard = process::acquire_start_guard(&paths).map_err(process_error)?;
    if process::read_pid(&paths.pid_file)
        .map_err(process_error)?
        .is_some_and(process::process_exists)
    {
        drop(guard);
        return Ok(());
    }
    let config = config::Config::from_toml(path)?;
    if process::probe_health(&config.server.host, config.server.port).await
        == process::HealthProbe::Healthy
    {
        drop(guard);
        return Ok(());
    }
    let child = spawn_detached(path, None, false)?;
    let deadline = Instant::now() + WATCHDOG_START_TIMEOUT;
    loop {
        if process::read_pid(&paths.pid_file)
            .map_err(process_error)?
            .is_some_and(process::process_exists)
            || process::probe_health(&config.server.host, config.server.port).await
                == process::HealthProbe::Healthy
        {
            drop(guard);
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(command_error(
                EXIT_VALIDATION,
                format!(
                    "server did not confirm startup within 2s (child PID {})",
                    child.id()
                ),
            ));
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn runtime_status(path: &Path, json_output: bool) -> Result<(), BootstrapError> {
    let config = config::Config::from_toml(path)?;
    let value = fetch_runtime_status(&config)
        .await
        .map_err(|error| command_error(EXIT_VALIDATION, error))?;
    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&value).expect("JSON rendering")
        );
    } else {
        print_runtime_status(&value);
    }
    Ok(())
}

async fn fetch_runtime_status(config: &config::Config) -> Result<Value, String> {
    let host = match config.server.host.as_str() {
        "0.0.0.0" => "127.0.0.1",
        "::" => "::1",
        value => value,
    };
    let mut stream = timeout(
        STATUS_TIMEOUT,
        TcpStream::connect((host, config.server.port)),
    )
    .await
    .map_err(|_| "runtime-status connection timed out".to_owned())?
    .map_err(|_| "runtime-status server is not running".to_owned())?;
    let mut request =
        "GET /api/stats/runtime HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n".to_owned();
    if let Some(key) = config.resolved_server_api_key() {
        request.push_str(&format!("Authorization: Bearer {key}\r\n"));
    }
    request.push_str("\r\n");
    timeout(STATUS_TIMEOUT, stream.write_all(request.as_bytes()))
        .await
        .map_err(|_| "runtime-status request timed out".to_owned())?
        .map_err(|_| "runtime-status request failed".to_owned())?;
    let mut bytes = Vec::with_capacity(4096);
    let mut chunk = [0_u8; 8192];
    loop {
        let count = timeout(STATUS_TIMEOUT, stream.read(&mut chunk))
            .await
            .map_err(|_| "runtime-status response timed out".to_owned())?
            .map_err(|_| "runtime-status response failed".to_owned())?;
        if count == 0 {
            break;
        }
        if bytes.len().saturating_add(count) > MAX_STATUS_BODY_BYTES {
            return Err("runtime-status response body is too large".to_owned());
        }
        bytes.extend_from_slice(&chunk[..count]);
    }
    let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
        return Err("runtime-status response was malformed".to_owned());
    };
    let headers = &bytes[..header_end];
    let status = headers
        .split(|byte| *byte == b'\n')
        .next()
        .and_then(|line| line.split(|byte| *byte == b' ').nth(1))
        .and_then(|value| std::str::from_utf8(value).ok())
        .and_then(|value| value.trim().parse::<u16>().ok())
        .ok_or_else(|| "runtime-status response status was malformed".to_owned())?;
    if status != 200 {
        return Err(match status {
            401 | 403 => format!("runtime-status authentication failed (HTTP {status})"),
            404 => "runtime-status endpoint is unavailable".to_owned(),
            _ => format!("runtime-status server returned HTTP {status}"),
        });
    }
    serde_json::from_slice(&bytes[header_end + 4..])
        .map_err(|_| "runtime-status response body was malformed JSON".to_owned())
}

fn print_runtime_status(value: &Value) {
    let server = value.get("server").unwrap_or(&Value::Null);
    let memory = value.get("memory").unwrap_or(&Value::Null);
    let processes = value.get("processes").unwrap_or(&Value::Null);
    let db = value.get("db").unwrap_or(&Value::Null);
    let routing = value.get("routing_runtime").unwrap_or(&Value::Null);
    println!("=== EggPool Runtime Status ===\n");
    println!("  PID:            {}", display_value(server.get("pid")));
    println!(
        "  Uptime:         {}",
        format_duration(server.get("uptime_seconds"))
    );
    println!(
        "  Server Threads: {}",
        display_value(server.get("configured_server_threads"))
    );
    println!(
        "  Rust:           {}",
        display_value(server.get("rust_version"))
    );
    println!(
        "  RSS:            {}",
        format_bytes(memory.get("rss_bytes"))
    );
    println!(
        "  VMS:            {}",
        format_bytes(memory.get("vms_bytes"))
    );
    println!(
        "  Open FDs:       {}",
        display_value(memory.get("open_fd_count"))
    );
    println!(
        "  Threads:        {}",
        display_value(memory.get("thread_count"))
    );
    println!(
        "\n  EggPool Processes: {} (expected: {})",
        display_value(processes.get("eggpool_process_count")),
        display_value(processes.get("expected_worker_process_count"))
    );
    println!("\n  Background Tasks:");
    match value.get("background_tasks").and_then(Value::as_array) {
        Some(tasks) if !tasks.is_empty() => {
            for task in tasks {
                println!("    {}", display_value(task.get("name")));
            }
        }
        _ => println!("    (none)"),
    }
    println!("\n  DB Path:        {}", display_value(db.get("path")));
    println!(
        "  DB Size:        {}",
        format_bytes(db.get("file_size_bytes"))
    );
    println!(
        "  WAL Size:       {}",
        format_bytes(db.get("wal_size_bytes"))
    );
    println!(
        "\n  Pending Requests:   {}",
        display_value(routing.get("pending_count"))
    );
    println!(
        "  Active Reservations: {}",
        display_value(routing.get("active_reservations_count"))
    );
    println!(
        "  Reserved (μ$):      {}",
        display_value(routing.get("reserved_microdollars"))
    );
    let outbound = value.get("outbound_client").unwrap_or(&Value::Null);
    let provider_pool = value.get("provider_client_pool").unwrap_or(&Value::Null);
    println!("\n  Network:");
    println!(
        "    Outbound builds:   {}",
        display_value(outbound.get("build_count"))
    );
    println!(
        "    Outbound requests: {}",
        display_value(outbound.get("request_count"))
    );
    println!(
        "    Outbound errors:   {}",
        display_value(outbound.get("error_count"))
    );
    println!(
        "    Provider clients:  {}",
        display_value(provider_pool.get("build_count"))
    );
    if let Some(errors) = value.get("probe_errors").and_then(Value::as_array)
        && !errors.is_empty()
    {
        println!("\n  Probe Errors:");
        for error in errors {
            println!("    - {}", display_value(Some(error)));
        }
    }
}

fn display_value(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Number(value)) => value.to_string(),
        Some(Value::Bool(value)) => value.to_string(),
        Some(Value::Null) | None => "N/A".to_owned(),
        Some(value) => value.to_string(),
    }
}

fn format_duration(value: Option<&Value>) -> String {
    let Some(seconds) = value.and_then(Value::as_f64) else {
        return "N/A".to_owned();
    };
    let total = seconds.max(0.0) as u64;
    if total >= 3600 {
        format!("{}h {}m", total / 3600, (total % 3600) / 60)
    } else if total >= 60 {
        format!("{}m {}s", total / 60, total % 60)
    } else {
        format!("{total}s")
    }
}

fn format_bytes(value: Option<&Value>) -> String {
    let Some(bytes) = value.and_then(Value::as_u64) else {
        return "N/A".to_owned();
    };
    if bytes >= 1024 * 1024 * 1024 {
        format!("{:.2} GB", bytes as f64 / (1024.0 * 1024.0 * 1024.0))
    } else if bytes >= 1024 * 1024 {
        format!("{:.1} MB", bytes as f64 / (1024.0 * 1024.0))
    } else if bytes >= 1024 {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    } else {
        format!("{bytes} B")
    }
}
