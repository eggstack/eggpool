use super::*;

pub(super) async fn chat_completions(
    State(state): State<AppState>,
    Extension(lease): Extension<Arc<GenerationLease>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    handle_inference(state, ClientSurface::ChatCompletions, headers, body, lease).await
}

pub(super) async fn messages(
    State(state): State<AppState>,
    Extension(lease): Extension<Arc<GenerationLease>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    handle_inference(state, ClientSurface::Messages, headers, body, lease).await
}

pub(super) async fn responses(
    State(state): State<AppState>,
    Extension(lease): Extension<Arc<GenerationLease>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    handle_inference(state, ClientSurface::Responses, headers, body, lease).await
}

/// Historical remote-compaction endpoint (`POST /v1/responses/compact`).
///
/// A thin adapter only: bounded compact admission, one compact coordinator
/// operation, bounded compact result. No provider selection, summarization,
/// retry, or JSON shape reconstruction happens here. The endpoint is
/// finite-only and shares the stateless Responses contract; stateful
/// continuation remains rejected.
pub(super) async fn responses_compact(
    State(state): State<AppState>,
    Extension(lease): Extension<Arc<GenerationLease>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let session = headers
        .get("x-eggpool-route-session")
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let proxy_request_id = crate::coordinator::new_proxy_request_id();
    handle_finite_compact(state, headers, body, session, proxy_request_id, lease).await
}

pub(super) async fn handle_finite_compact(
    state: AppState,
    headers: HeaderMap,
    body: Bytes,
    session: Option<String>,
    proxy_request_id: String,
    lease: Arc<GenerationLease>,
) -> Response {
    let surface = ClientSurface::Responses;
    let incoming = filtered_incoming_headers(&headers);
    match crate::coordinator::execute_compact_finite(
        lease.generation().inference(),
        body,
        incoming,
        session,
        proxy_request_id.clone(),
    )
    .await
    {
        Ok((execution, _virtual)) => {
            execution.mark_started();
            let status = execution.response.status;
            let mut outgoing = HeaderMap::new();
            for (name, value) in &execution.response.headers {
                outgoing.insert(name.clone(), value.clone());
            }
            let body = execution.response.body.clone();
            if let Some(metrics) = state
                .process
                .as_ref()
                .map(ProcessRuntime::metrics_coalescer)
                && let Some(event) = execution.usage_metric_event()
            {
                let _ = metrics.record_usage_async(event).await;
            }
            match execution
                .complete(crate::coordinator::DownstreamResult::Delivered)
                .await
            {
                Ok(_) => (status, outgoing, body).into_response(),
                Err(_) => {
                    let detail = endpoint_error_body(surface, "Finalization failed");
                    error_body_response(StatusCode::SERVICE_UNAVAILABLE, surface, detail)
                }
            }
        }
        Err(error) => {
            let status = error.status();
            let detail = endpoint_error_body(surface, &error.to_string());
            error_body_response(status, surface, detail)
        }
    }
}

pub(super) async fn handle_inference(
    state: AppState,
    surface: ClientSurface,
    headers: HeaderMap,
    body: Bytes,
    lease: Arc<GenerationLease>,
) -> Response {
    let session = headers
        .get("x-eggpool-route-session")
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let proxy_request_id = crate::coordinator::new_proxy_request_id();
    let incoming = filtered_incoming_headers(&headers);
    match crate::coordinator::execute_endpoint(
        lease.generation().inference(),
        surface,
        body,
        incoming,
        session,
        proxy_request_id,
    )
    .await
    {
        Ok(crate::coordinator::EndpointExecution::Finite(execution, _)) => {
            finish_finite_execution(state, surface, *execution).await
        }
        Ok(crate::coordinator::EndpointExecution::Stream(execution, _)) => {
            finish_stream_execution(state, *execution, lease).await
        }
        Err(error) => {
            let status = error.status();
            let detail = endpoint_error_body(surface, &error.to_string());
            error_body_response(status, surface, detail)
        }
    }
}

async fn finish_finite_execution(
    state: AppState,
    surface: ClientSurface,
    execution: crate::coordinator::FiniteExecution,
) -> Response {
    execution.mark_started();
    let status = execution.response.status;
    let mut outgoing = HeaderMap::new();
    for (name, value) in &execution.response.headers {
        outgoing.append(name.clone(), value.clone());
    }
    let body = execution.response.body.clone();
    if let Some(metrics) = state
        .process
        .as_ref()
        .map(ProcessRuntime::metrics_coalescer)
        && let Some(event) = execution.usage_metric_event()
    {
        let _ = metrics.record_usage_async(event).await;
    }
    match execution
        .complete(crate::coordinator::DownstreamResult::Delivered)
        .await
    {
        Ok(_) => (status, outgoing, body).into_response(),
        Err(_) => {
            let detail = endpoint_error_body(surface, "Finalization failed");
            error_body_response(StatusCode::SERVICE_UNAVAILABLE, surface, detail)
        }
    }
}

async fn finish_stream_execution(
    state: AppState,
    execution: crate::coordinator::StreamingExecution,
    lease: Arc<GenerationLease>,
) -> Response {
    if let Some(error_body) = execution.error_body.clone() {
        let status = execution.headers.status;
        let mut outgoing = HeaderMap::new();
        for (name, value) in &execution.headers.headers {
            outgoing.append(name.clone(), value.clone());
        }
        execution.mark_started();
        if let Some(metrics) = state
            .process
            .as_ref()
            .map(ProcessRuntime::metrics_coalescer)
            && let Some(event) = execution.usage_metric_event()
        {
            let _ = metrics.record_usage_async(event).await;
        }
        let _ = execution
            .complete(crate::coordinator::DownstreamResult::Delivered)
            .await;
        return (status, outgoing, error_body).into_response();
    }
    let status = execution.headers.status;
    let mut outgoing = HeaderMap::new();
    for (name, value) in &execution.headers.headers {
        outgoing.append(name.clone(), value.clone());
    }
    let (sender, receiver) = tokio::sync::mpsc::channel::<Result<Bytes, std::io::Error>>(32);
    let metrics = state
        .process
        .as_ref()
        .map(ProcessRuntime::metrics_coalescer);
    state.body_tasks.spawn(async move {
        let _lease = lease;
        let mut execution = execution;
        execution.mark_started();
        loop {
            match execution.next_chunk().await {
                Some(Ok(chunk)) => {
                    if sender.send(Ok(chunk)).await.is_err() {
                        drop(execution);
                        break;
                    }
                }
                None | Some(Err(_)) => {
                    if let Some(metrics) = &metrics
                        && let Some(event) = execution.usage_metric_event()
                    {
                        let _ = metrics.record_usage_async(event).await;
                    }
                    let _ = execution
                        .complete(crate::coordinator::DownstreamResult::Delivered)
                        .await;
                    break;
                }
            }
        }
    });
    let stream = tokio_stream::wrappers::ReceiverStream::new(receiver);
    (status, outgoing, axum::body::Body::from_stream(stream)).into_response()
}

pub(super) fn error_body_response(
    status: StatusCode,
    surface: ClientSurface,
    detail: Vec<u8>,
) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    let _ = surface;
    (status, headers, Bytes::from(detail)).into_response()
}

/// Incoming headers forwarded toward provider selection, minus credentials
/// and hop-by-hop framing. The session header is hashed, never forwarded.
pub(super) fn filtered_incoming_headers(headers: &HeaderMap) -> HeaderMap {
    const BLOCKED: [&str; 10] = [
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "host",
        "content-length",
        "x-eggpool-route-session",
        "connection",
        "transfer-encoding",
        "upgrade",
        "keep-alive",
    ];
    let mut outgoing = HeaderMap::new();
    for (name, value) in headers {
        if BLOCKED.contains(&name.as_str()) {
            continue;
        }
        outgoing.insert(name.clone(), value.clone());
    }
    outgoing
}
