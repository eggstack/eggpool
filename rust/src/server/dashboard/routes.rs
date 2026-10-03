use super::*;

pub(in crate::server) async fn overview(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    let period = match normalize_period(query.period.as_deref()) {
        Ok(period) => period,
        Err(response) => return *response,
    };
    let repository = db::UsageRollupRepository::new(&state.database);
    let summary = match repository.dashboard_summary_basic(period).await {
        Ok(summary) => summary,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let accounts = match db::AccountRepository::new(&state.database).list_all().await {
        Ok(accounts) => accounts,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let page_data = match db::DashboardRepository::new(&state.database)
        .load(period)
        .await
    {
        Ok(data) => data,
        Err(_) => return degraded("dashboard data unavailable"),
    };
    let theme_name = selected_theme(
        query
            .theme
            .as_deref()
            .unwrap_or(&state.server.dashboard_theme),
    );
    let show_disabled = query.show_disabled.as_deref() == Some("1");
    let health_snapshots = state
        .runtime
        .acquire()
        .await
        .ok()
        .map(|lease| {
            lease
                .generation()
                .inference()
                .router_handle()
                .health_snapshots()
        })
        .unwrap_or_default();
    let html = render_overview(
        &summary,
        OverviewPage {
            accounts: &accounts,
            page_data: &page_data,
            period,
            theme: theme_name,
            refresh_interval_s: state.server.dashboard_refresh_interval_s,
            show_disabled,
            health_snapshots: &health_snapshots,
        },
    );
    html_response(html)
}

pub(in crate::server) async fn accounts_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page_with_options(
        &state,
        "Accounts",
        "accounts",
        query.period,
        query.theme,
        query.show_disabled.as_deref() == Some("1"),
        ModelFilters::default(),
    )
    .await
}

pub(in crate::server) async fn models_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page_with_model_filters(
        &state,
        query.period,
        query.theme,
        ModelFilters {
            account: query.account,
            used: query.used,
            info_status: query.info_status,
            availability: query.availability,
            ..ModelFilters::default()
        },
    )
    .await
}

pub(in crate::server) async fn model_detail_page(
    State(state): State<AppState>,
    AxumPath(model_id): AxumPath<String>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    let model_id = model_id.trim_start_matches('/');
    let period = match normalize_period(query.period.as_deref()) {
        Ok(period) => period,
        Err(response) => return *response,
    };
    let theme = selected_theme(
        query
            .theme
            .as_deref()
            .unwrap_or(&state.server.dashboard_theme),
    );
    let model_info = crate::operations::operator::show_model_info(&state.database, model_id)
        .await
        .ok()
        .flatten();
    let observations =
        crate::operations::operator::list_compact_model_observations(&state.database, model_id)
            .await
            .unwrap_or_default();
    let body = render_model_detail(model_id, model_info.as_ref(), &observations);
    let title_model_id = model_info
        .as_ref()
        .and_then(|info| info["detail"]["display_name"].as_str())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or(model_id);
    dashboard_page_with_body(
        &state.server.dashboard_theme,
        &format!("Model: {title_model_id}"),
        "models",
        Some(period.to_owned()),
        Some(theme.to_owned()),
        body,
    )
}

pub(in crate::server) async fn latency_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Latency", "latency", query.period, query.theme).await
}

pub(in crate::server) async fn events_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page_with_options(
        &state,
        "Events",
        "events",
        query.period,
        query.theme,
        false,
        ModelFilters {
            event_type: query.event_type,
            ..ModelFilters::default()
        },
    )
    .await
}

pub(in crate::server) async fn timeseries_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(
        &state,
        "Timeseries",
        "timeseries",
        query.period,
        query.theme,
    )
    .await
}

pub(in crate::server) async fn bandwidth_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Bandwidth", "bandwidth", query.period, query.theme).await
}

pub(in crate::server) async fn pings_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Provider Pings", "pings", query.period, query.theme).await
}

pub(in crate::server) async fn reliability_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(
        &state,
        "Reliability",
        "reliability",
        query.period,
        query.theme,
    )
    .await
}

pub(in crate::server) async fn routing_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Routing", "routing", query.period, query.theme).await
}

pub(in crate::server) async fn traces_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page_with_options(
        &state,
        "Traces",
        "traces",
        query.period,
        query.theme,
        false,
        ModelFilters {
            trace_limit: query.limit,
            ..ModelFilters::default()
        },
    )
    .await
}

pub(in crate::server) async fn runtime_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Runtime", "runtime", query.period, query.theme).await
}

pub(in crate::server) async fn cache_page(
    State(state): State<AppState>,
    Query(query): Query<PeriodQuery>,
) -> Response {
    dashboard_data_page(&state, "Cache", "cache", query.period, query.theme).await
}

pub(in crate::server) async fn sync_accounts(
    config: &Config,
    database: &db::Database,
) -> Result<(), ServerError> {
    let accounts = config
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            provider
                .accounts
                .iter()
                .map(move |account| db::AccountConfig {
                    name: account.name.clone(),
                    api_key_env: account.api_key_env.clone(),
                    enabled: account.enabled,
                    weight: account.weight,
                    provider_id: provider_id.clone(),
                })
        })
        .collect();
    db::AccountRepository::new(database)
        .sync_from_config(accounts)
        .await
        .map(|_| ())
        .map_err(ServerError::Database)
}

pub(super) async fn dashboard_data_page(
    state: &AppState,
    title: &str,
    active_nav: &str,
    period: Option<String>,
    theme: Option<String>,
) -> Response {
    dashboard_data_page_with_options(
        state,
        title,
        active_nav,
        period,
        theme,
        false,
        ModelFilters::default(),
    )
    .await
}

pub(super) async fn dashboard_data_page_with_options(
    state: &AppState,
    title: &str,
    active_nav: &str,
    period: Option<String>,
    theme: Option<String>,
    show_disabled: bool,
    model_filters: ModelFilters,
) -> Response {
    let period = match normalize_period(period.as_deref()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let theme = selected_theme(theme.as_deref().unwrap_or(&state.server.dashboard_theme));
    let data = match db::DashboardRepository::new(&state.database)
        .load(period)
        .await
    {
        Ok(data) => data,
        Err(error) => {
            eprintln!("dashboard data read failed: {error}");
            return degraded("dashboard data unavailable");
        }
    };
    let summary = match db::UsageRollupRepository::new(&state.database)
        .dashboard_summary_basic(period)
        .await
    {
        Ok(summary) => summary,
        Err(error) => {
            eprintln!("dashboard summary read failed: {error}");
            return degraded("dashboard data unavailable");
        }
    };
    let observability = if matches!(active_nav, "runtime" | "cache") {
        match db::DashboardRepository::new(&state.database)
            .observability_stats(period)
            .await
        {
            Ok(value) => value,
            Err(_) => return degraded("dashboard data unavailable"),
        }
    } else {
        Value::Null
    };
    let runtime_diagnostics = if active_nav == "runtime" {
        state
            .process
            .as_ref()
            .map(|process| process.diagnostics(&state.runtime))
    } else {
        None
    };
    let model_info = if active_nav == "models" {
        crate::operations::operator::list_model_info(&state.database, None)
            .await
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let health_snapshots = if active_nav == "accounts" {
        state
            .runtime
            .acquire()
            .await
            .ok()
            .map(|lease| {
                lease
                    .generation()
                    .inference()
                    .router_handle()
                    .health_snapshots()
            })
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let provider_priorities = if active_nav == "models" {
        state
            .runtime
            .acquire()
            .await
            .ok()
            .map(|lease| {
                lease
                    .generation()
                    .config()
                    .providers
                    .iter()
                    .map(|(provider_id, provider)| (provider_id.clone(), provider.routing_priority))
                    .collect::<std::collections::BTreeMap<_, _>>()
            })
            .unwrap_or_default()
    } else {
        std::collections::BTreeMap::new()
    };
    let timeseries_projection = if active_nav == "timeseries" {
        match db::DashboardRepository::new(&state.database)
            .grouped_timeseries_json(
                period.to_owned(),
                "hour".to_owned(),
                "provider_model".to_owned(),
                None,
                None,
            )
            .await
        {
            Ok((rows, _from_rollups)) if rows.is_empty() => Some(json!({
                "bucket": "hour",
                "group_by": "provider_model",
                "metric": "requests",
                "limit": 12,
                "source": "empty",
                "degraded_reason": "rollup_empty",
                "buckets": [],
                "series": [],
                "points": [],
                "bucket_totals": [],
            })),
            Ok((rows, from_rollups)) => Some(grouped_timeseries_projection(
                &rows,
                "hour",
                "provider_model",
                12,
                from_rollups,
            )),
            Err(error) => {
                eprintln!("dashboard timeseries read failed: {error}");
                return degraded("dashboard data unavailable");
            }
        }
    } else {
        None
    };
    let routing_trace = if active_nav == "routing" {
        state
            .runtime
            .acquire()
            .await
            .ok()
            .map(|lease| {
                let config = &lease.generation().config().routing.trace;
                RoutingTraceSnapshot {
                    mode: config.mode.clone(),
                    sample_rate: config.sample_rate,
                    status: if config.mode == "off" {
                        "Off".to_owned()
                    } else {
                        "Unavailable".to_owned()
                    },
                    queue_capacity: if config.mode == "off" {
                        0
                    } else {
                        u64::from(config.queue_capacity)
                    },
                    ..RoutingTraceSnapshot::default()
                }
            })
            .unwrap_or_default()
    } else {
        RoutingTraceSnapshot::default()
    };
    let mut body = render_dashboard_page_body(
        title,
        active_nav,
        period,
        theme,
        &data,
        &summary,
        &observability,
        runtime_diagnostics.as_ref(),
        state.server.started_at.elapsed(),
        &model_info,
        show_disabled,
        &model_filters,
        &health_snapshots,
        &provider_priorities,
        timeseries_projection.as_ref(),
        &routing_trace,
    );
    if active_nav == "timeseries" {
        body = body.replace(
            "class=\"period-selector\"",
            "class=\"period-selector timeseries-period-selector\"",
        );
    }
    dashboard_page_with_body(
        &state.server.dashboard_theme,
        title,
        active_nav,
        Some(period.to_owned()),
        Some(theme.to_owned()),
        body,
    )
}

pub(super) async fn dashboard_data_page_with_model_filters(
    state: &AppState,
    period: Option<String>,
    theme: Option<String>,
    model_filters: ModelFilters,
) -> Response {
    dashboard_data_page_with_options(
        state,
        "Models",
        "models",
        period,
        theme,
        false,
        model_filters,
    )
    .await
}
