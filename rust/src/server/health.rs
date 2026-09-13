use super::dashboard::{degraded, json_response};
use super::*;

pub(super) async fn healthz() -> Response {
    json_response(StatusCode::OK, json!({"status": "ok"}))
}

pub(super) async fn models_api(State(state): State<AppState>) -> Response {
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return degraded("runtime unavailable"),
    };
    let data = lease
        .generation()
        .inference()
        .catalog_model_ids()
        .into_iter()
        .map(|model_id| {
            json!({
                "id": model_id,
                "object": "model",
                "owned_by": "eggpool",
                "name": model_id,
            })
        })
        .collect::<Vec<_>>();
    json_response(StatusCode::OK, json!({"object": "list", "data": data}))
}

pub(super) async fn runtime_status(State(state): State<AppState>) -> Response {
    let diagnostics = state
        .process
        .as_ref()
        .map(|process| process.diagnostics(&state.runtime));
    let db_path = &state.server.database_path;
    let (file_size_bytes, wal_size_bytes) = if db_path == ":memory:" {
        (None, None)
    } else {
        let file_size = std::fs::metadata(db_path)
            .ok()
            .map(|metadata| metadata.len());
        let wal_size = std::fs::metadata(format!("{db_path}-wal"))
            .ok()
            .map(|metadata| metadata.len());
        (file_size, wal_size)
    };
    let tasks = diagnostics
        .as_ref()
        .map(|snapshot| {
            snapshot
                .tasks
                .iter()
                .map(|task| {
                    json!({
                        "name": task.name,
                        "running": task.running,
                        "enabled": task.enabled,
                        "tick_count": task.tick_count,
                        "last_outcome": task.last_outcome,
                        "in_tick": task.in_tick,
                    })
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let active_jobs = diagnostics
        .as_ref()
        .map(|snapshot| snapshot.active_generation.finalization_active_jobs)
        .unwrap_or_default();
    let runtime_manager = diagnostics
        .as_ref()
        .and_then(|snapshot| serde_json::to_value(snapshot).ok());
    json_response(
        StatusCode::OK,
        json!({
            "server": {
                "pid": std::process::id(),
                "ppid": parent_pid(),
                "uptime_seconds": state.server.started_at.elapsed().as_secs_f64(),
                "configured_server_threads": state.server.configured_server_threads,
                "python_version": serde_json::Value::Null,
                "rust_version": env!("CARGO_PKG_VERSION"),
            },
            "memory": {
                "rss_bytes": serde_json::Value::Null,
                "vms_bytes": serde_json::Value::Null,
                "open_fd_count": serde_json::Value::Null,
                "thread_count": 1,
            },
            "processes": {
                "eggpool_process_count": 1,
                "expected_worker_process_count": 1,
                "process_count_warning": false,
            },
            "background_tasks": tasks,
            "db": {
                "path": db_path,
                "is_memory_db": db_path == ":memory:",
                "file_size_bytes": file_size_bytes,
                "wal_size_bytes": wal_size_bytes,
            },
            "routing_runtime": {
                "active_requests_total": 0,
                "active_requests_by_account": serde_json::Value::Null,
                "pending_count": 0,
                "oldest_pending_age_seconds": serde_json::Value::Null,
                "active_reservations_count": active_jobs,
                "reserved_microdollars": 0,
                "health_states_by_account": serde_json::Value::Null,
                "active_backoff_count": 0,
            },
            "outbound_client": {
                "build_count": 0,
                "request_count": 0,
                "error_count": 0,
                "has_client": false,
            },
            "provider_client_pool": {
                "build_count": 0,
                "providers": {},
            },
            "runtime_manager": runtime_manager,
            "probe_errors": [],
        }),
    )
}

pub(super) async fn update_status(State(state): State<AppState>) -> Response {
    let snapshot = state
        .process
        .as_ref()
        .map(|process| process.update_checker().snapshot())
        .unwrap_or_default();
    json_response(
        StatusCode::OK,
        serde_json::to_value(snapshot).unwrap_or_else(|_| json!({})),
    )
}

pub(super) fn parent_pid() -> serde_json::Value {
    #[cfg(unix)]
    {
        json!(nix::unistd::getppid().as_raw())
    }
    #[cfg(not(unix))]
    {
        serde_json::Value::Null
    }
}

pub(super) async fn readyz(State(state): State<AppState>) -> Response {
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return degraded("runtime unavailable"),
    };
    let generation_config = lease.generation().config();
    let accounts = match db::AccountRepository::new(&state.database)
        .list_enabled()
        .await
    {
        Ok(accounts) => accounts,
        Err(_) => return degraded("database not writable"),
    };
    if generation_config.all_accounts().is_empty() {
        return degraded("no accounts configured");
    }
    if accounts.is_empty() {
        return degraded("no enabled accounts");
    }
    if !has_loaded_credentials(generation_config) {
        return degraded("no loaded credentials");
    }
    let active_catalog_has_models = lease
        .generation()
        .inference()
        .router_handle()
        .catalog_model_count()
        > 0;
    match db::ModelRepository::new(&state.database).list(None).await {
        Ok(models) if active_catalog_has_models && !models.is_empty() => {
            json_response(StatusCode::OK, json!({"status": "ok"}))
        }
        Ok(_) => degraded("no usable model catalog"),
        Err(_) => degraded("database not writable"),
    }
}

pub(super) fn has_loaded_credentials(config: &Config) -> bool {
    config.providers.values().any(|provider| {
        let provider_is_anonymous = provider.auth.mode == "none"
            && provider
                .wire_surfaces
                .values()
                .all(|surface| surface.auth.as_ref().is_none_or(|auth| auth.mode == "none"));
        provider.accounts.iter().any(|account| {
            account.enabled
                && (provider_is_anonymous
                    || account.api_key.as_ref().is_some_and(|key| !key.is_empty())
                    || std::env::var(&account.api_key_env).is_ok_and(|key| !key.is_empty()))
        })
    })
}
