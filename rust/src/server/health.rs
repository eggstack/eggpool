use super::dashboard::{degraded, json_response};
use super::*;
use crate::operations::status as status_service;

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

/// Authenticated EggPool-specific integration profile (Plan 211).
///
/// Returns the portable `AgentIntegrationProfileV1` built from the same
/// conservative projection as local `configsetup`. Deterministic,
/// bounded, sanitized, and versioned. Performs no catalog refresh, upstream
/// request, or health mutation. Standard `/v1/models` is unchanged.
pub(super) async fn integration_profile(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => {
            return json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"detail": "integration profile unavailable"}),
            );
        }
    };
    let config = lease.generation().config().clone();
    let base_url = config
        .integrations
        .advertise_base_url
        .clone()
        .unwrap_or_else(|| format!("http://{}:{}/v1", config.server.host, config.server.port));
    let profile = match crate::operations::integrations::build_integration_profile(
        &config,
        &state.database,
        &base_url,
    )
    .await
    {
        Ok(profile) => profile,
        Err(_) => {
            return json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"detail": "integration profile unavailable"}),
            );
        }
    };
    let body = match profile.canonical_json() {
        Ok(body) => body,
        Err(_) => {
            return json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"detail": "integration profile unavailable"}),
            );
        }
    };
    if body.len() > eggpool_client_config::MAX_INTEGRATION_PROFILE_BYTES {
        return json_response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"detail": "integration profile unavailable"}),
        );
    }
    let etag = format!("\"{}\"", profile.revision);
    if let Some(if_none) = headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
    {
        let matched = if_none.split(',').any(|candidate| {
            let candidate = candidate.trim().trim_start_matches("W/").trim_matches('"');
            candidate == profile.revision
        });
        if matched {
            return (
                StatusCode::NOT_MODIFIED,
                [
                    (header::ETAG, etag.clone()),
                    (
                        header::CACHE_CONTROL,
                        "private, max-age=0, must-revalidate".to_owned(),
                    ),
                ],
            )
                .into_response();
        }
    }
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/json".to_owned()),
            (
                header::CACHE_CONTROL,
                "private, max-age=0, must-revalidate".to_owned(),
            ),
            (header::ETAG, etag),
        ],
        body,
    )
        .into_response()
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
    let snapshot = readiness_snapshot(&state).await;
    if snapshot.ready {
        json_response(StatusCode::OK, json!({"status": "ok"}))
    } else {
        degraded(snapshot.reason_code.as_deref().unwrap_or("not ready"))
    }
}

async fn readiness_snapshot(state: &AppState) -> status_service::ReadinessSnapshot {
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => {
            return status_service::evaluate_readiness(0, 0, false, 0, 0, true, false);
        }
    };
    let generation_config = lease.generation().config();
    let configured_accounts = generation_config.all_accounts().len();
    let enabled_accounts = match db::AccountRepository::new(&state.database)
        .list_enabled()
        .await
    {
        Ok(accounts) => accounts.len(),
        Err(_) => {
            return status_service::evaluate_readiness(
                configured_accounts,
                0,
                false,
                0,
                0,
                false,
                true,
            );
        }
    };
    let has_credentials = has_loaded_credentials(generation_config);
    let active_catalog_models = lease
        .generation()
        .inference()
        .router_handle()
        .catalog_model_count();
    let persisted_models = match db::ModelRepository::new(&state.database).list(None).await {
        Ok(models) => models.len(),
        Err(_) => {
            return status_service::evaluate_readiness(
                configured_accounts,
                enabled_accounts,
                has_credentials,
                active_catalog_models,
                0,
                false,
                true,
            );
        }
    };
    status_service::evaluate_readiness(
        configured_accounts,
        enabled_accounts,
        has_credentials,
        active_catalog_models,
        persisted_models,
        true,
        true,
    )
}

pub(super) async fn status_api(State(state): State<AppState>) -> Response {
    let snapshot = build_status_snapshot(&state).await;
    match serde_json::to_value(&snapshot) {
        Ok(value) => json_response(StatusCode::OK, value),
        Err(_) => degraded("status snapshot failed"),
    }
}

async fn build_status_snapshot(state: &AppState) -> status_service::ProxyStatusSnapshot {
    use std::collections::{BTreeMap, BTreeSet};
    use std::time::{SystemTime, UNIX_EPOCH};

    let version = crate::version::PACKAGE_VERSION.to_owned();
    let now_epoch_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => {
            return status_service::ProxyStatusSnapshot {
                schema_version: status_service::STATUS_SCHEMA_VERSION,
                observed_at: status_service::observed_at_now(),
                proxy: status_service::ProxyHealthSummary {
                    status: status_service::ProxyStatus::Unready,
                    ready: false,
                    available: true,
                    version,
                    base_url: String::new(),
                    uptime_seconds: Some(state.server.started_at.elapsed().as_secs_f64()),
                    model_count: 0,
                    routable_accounts: 0,
                    enabled_accounts: 0,
                    reason_code: Some("runtime unavailable".to_owned()),
                },
                providers: Vec::new(),
                runtime: status_service::RuntimeHealthSummary {
                    generation: None,
                    digest_prefix: String::new(),
                    reload: "unknown".to_owned(),
                    tasks: "unknown".to_owned(),
                    db: "unknown".to_owned(),
                    retiring: 0,
                },
            };
        }
    };
    let generation = lease.generation();
    let config = generation.config();
    let router = generation.inference().router_handle();
    let base_url = format!("http://{}:{}", config.server.host, config.server.port);
    let stale_after = status_service::stale_after_secs(config.models.stale_after_s);

    let health_manager = router.health_manager();
    let health_now = health_manager.as_ref().map(|manager| manager.now());
    let snapshots: BTreeMap<String, crate::health::AccountHealthSnapshot> = router
        .health_snapshots()
        .into_iter()
        .map(|snapshot| (snapshot.account_name.clone(), snapshot))
        .collect();
    let catalog_snapshot = router.catalog_snapshot();
    let catalog_counts =
        status_service::catalog_counts_by_provider(&catalog_snapshot.provider_model_keys);
    let total_models = router.catalog_model_count();

    // One bounded DB round-trip for the latest ping per account pair plus
    // the counts needed for the shared readiness decision.
    let ping_result = db::PingRepository::new(&state.database)
        .latest_grouped()
        .await;
    let ping_ok = ping_result.is_ok();
    let ping_map = ping_result.unwrap_or_default();
    let latest_per_provider: BTreeMap<String, db::Ping> = {
        let mut grouped: BTreeMap<String, Vec<db::Ping>> = BTreeMap::new();
        for ping in ping_map.into_values() {
            grouped
                .entry(ping.provider_id.clone())
                .or_default()
                .push(ping);
        }
        grouped
            .into_iter()
            .map(|(provider, mut rows)| {
                rows.sort_by(|left, right| left.probed_at.cmp(&right.probed_at));
                let latest = rows.pop().expect("grouped ping has one row");
                (provider, latest)
            })
            .collect()
    };
    let enabled_result = db::AccountRepository::new(&state.database)
        .list_enabled()
        .await;
    let enabled_ok = enabled_result.is_ok();
    let enabled_db_accounts = enabled_result.map(|rows| rows.len()).unwrap_or(0);
    let persisted_result = db::ModelRepository::new(&state.database).list(None).await;
    let persisted_ok = persisted_result.is_ok();
    let persisted_models = persisted_result.map(|rows| rows.len()).unwrap_or(0);
    let database_ok = ping_ok && enabled_ok && persisted_ok;

    // Active-generation identity: every configured provider appears exactly
    // once, even when all of its accounts are disabled.
    let mut provider_ids: BTreeSet<String> = config.providers.keys().cloned().collect();
    for identity in router.all_account_identities() {
        provider_ids.insert(identity.provider_id.clone());
    }
    let identities: BTreeMap<String, crate::accounts::AccountIdentity> = router
        .all_account_identities()
        .into_iter()
        .map(|identity| (identity.account_name.clone(), identity))
        .collect();

    let mut providers = Vec::new();
    for provider_id in provider_ids {
        let configured_accounts: Vec<&crate::config::AccountConfig> = config
            .providers
            .get(&provider_id)
            .map(|provider| provider.accounts.iter().collect())
            .unwrap_or_default();
        let mut accounts = Vec::new();
        for configured in configured_accounts {
            let identity = identities.get(&configured.name);
            let enabled = identity.is_some_and(|item| item.enabled) || configured.enabled;
            // Identity is authoritative when present; static config plus
            // environment resolution is the fallback for offline parity.
            let has_creds = identity.map_or_else(
                || {
                    configured
                        .api_key
                        .as_ref()
                        .is_some_and(|key| !key.trim().is_empty())
                        || (!configured.api_key_env.is_empty()
                            && std::env::var(&configured.api_key_env)
                                .is_ok_and(|key| !key.trim().is_empty()))
                },
                |item| item.has_usable_credentials,
            );
            let snapshot = snapshots.get(&configured.name);
            let now = health_now.unwrap_or(0.0);
            let circuit_open =
                snapshot
                    .zip(health_manager.as_ref())
                    .is_some_and(|(snapshot, _)| {
                        snapshot.circuit.state == crate::health::CircuitState::Open
                    });
            // Routability reuses the live health gate; unknown accounts stay
            // eligible for routing but never count as verified.
            let routable = if enabled && has_creds {
                match (snapshot, health_manager.as_ref()) {
                    (Some(_), Some(manager)) => {
                        manager.is_account_healthy_read_only(&configured.name)
                    }
                    (None, _) => true,
                    (Some(_), None) => true,
                }
            } else {
                false
            };
            let mut input = status_service::account_input_from_snapshot(
                &configured.name,
                &provider_id,
                enabled,
                has_creds,
                snapshot,
                now,
                circuit_open,
            );
            input.routable = routable && input.routable;
            if snapshot.is_none() {
                input.in_backoff = false;
            }
            accounts.push(input);
        }
        let ping = latest_per_provider
            .get(&provider_id)
            .map(|ping| status_service::ping_evidence_for_provider(Some(ping), now_epoch_secs))
            .unwrap_or(status_service::PingEvidence::never());
        providers.push(status_service::aggregate_provider(
            &status_service::ProviderStatusInput {
                provider_id: provider_id.clone(),
                accounts,
                ping,
                catalog_model_count: catalog_counts.get(&provider_id).copied(),
                stale_after_secs: stale_after,
            },
        ));
    }
    status_service::sort_providers(&mut providers);

    let readiness = status_service::evaluate_readiness(
        config.all_accounts().len(),
        enabled_db_accounts,
        has_loaded_credentials(config),
        total_models,
        persisted_models,
        database_ok,
        true,
    );
    let diagnostics = state
        .process
        .as_ref()
        .map(|process| process.diagnostics(&state.runtime));
    let task_degraded = diagnostics.as_ref().is_some_and(|snapshot| {
        snapshot.tasks.iter().any(|task| {
            matches!(
                task.last_outcome.as_deref(),
                Some("error" | "timeout" | "panic_or_join_failure")
            )
        })
    });
    let reload_active = diagnostics
        .as_ref()
        .is_some_and(|snapshot| snapshot.reload.in_progress);
    let retiring_abnormal = diagnostics.as_ref().is_some_and(|snapshot| {
        snapshot
            .retiring_generations
            .iter()
            .any(|retiring| retiring.failed_close)
    });
    let retiring_count = diagnostics
        .as_ref()
        .map(|snapshot| snapshot.retiring_generations.len())
        .unwrap_or(0);
    let (proxy_status, ready, reason) = status_service::aggregate_proxy(
        &readiness,
        &providers,
        task_degraded,
        reload_active,
        retiring_abnormal,
    );
    let routable_accounts = providers.iter().map(|item| item.routable_accounts).sum();
    let enabled_accounts = providers.iter().map(|item| item.enabled_accounts).sum();
    let generation_id = generation.generation_id();
    let digest: String = generation.content_digest().chars().take(12).collect();
    let reload_state = diagnostics.as_ref().map_or("unknown", |snapshot| {
        if snapshot.reload.in_progress {
            "active"
        } else {
            "idle"
        }
    });
    let tasks_summary = diagnostics
        .as_ref()
        .map_or("unknown".to_owned(), |snapshot| {
            let total = snapshot.tasks.len();
            let running = snapshot.tasks.iter().filter(|task| task.running).count();
            format!("{running}/{total}")
        });
    status_service::ProxyStatusSnapshot {
        schema_version: status_service::STATUS_SCHEMA_VERSION,
        observed_at: status_service::observed_at_now(),
        proxy: status_service::ProxyHealthSummary {
            status: proxy_status,
            ready,
            available: true,
            version,
            base_url,
            uptime_seconds: Some(state.server.started_at.elapsed().as_secs_f64()),
            model_count: total_models,
            routable_accounts,
            enabled_accounts,
            reason_code: reason,
        },
        providers,
        runtime: status_service::RuntimeHealthSummary {
            generation: Some(generation_id),
            digest_prefix: digest,
            reload: reload_state.to_owned(),
            tasks: tasks_summary,
            db: if database_ok {
                "ok".to_owned()
            } else {
                "degraded".to_owned()
            },
            retiring: retiring_count,
        },
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
