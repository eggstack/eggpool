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

pub(super) async fn handle_inference(
    state: AppState,
    surface: ClientSurface,
    headers: HeaderMap,
    body: Bytes,
    lease: Arc<GenerationLease>,
) -> Response {
    // Peek the stream flag without consuming the body: finite and streaming
    // coordinators own their full lifecycle and must not be mixed.
    let is_stream = serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|value| value.get("stream").cloned())
        .is_some_and(|value| value == serde_json::Value::Bool(true));
    // A present-but-non-boolean stream flag is a 400 (Python parity).
    let stream_shape_valid = serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|value| value.get("stream").cloned())
        .is_none_or(|value| value.is_null() || value.is_boolean());
    if !stream_shape_valid {
        let detail = endpoint_error_body(surface, "Invalid stream value: must be a boolean");
        return error_body_response(StatusCode::BAD_REQUEST, surface, detail);
    }
    let session = headers
        .get("x-eggpool-route-session")
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let proxy_request_id = crate::coordinator::new_proxy_request_id();
    if is_stream {
        handle_stream_inference(
            state,
            surface,
            headers,
            body,
            session,
            proxy_request_id,
            lease,
        )
        .await
    } else {
        handle_finite_inference(
            state,
            surface,
            headers,
            body,
            session,
            proxy_request_id,
            lease,
        )
        .await
    }
}

pub(super) async fn handle_finite_inference(
    state: AppState,
    surface: ClientSurface,
    headers: HeaderMap,
    body: Bytes,
    session: Option<String>,
    proxy_request_id: String,
    lease: Arc<GenerationLease>,
) -> Response {
    let incoming = filtered_incoming_headers(&headers);
    match crate::coordinator::execute_finite(
        lease.generation().inference(),
        surface,
        body,
        incoming,
        session,
        proxy_request_id.clone(),
    )
    .await
    {
        Ok((execution, _virtual)) => {
            // Mark response start immediately before sending response start,
            // then converge retained C006 ownership after the finite body
            // write. The Axum body write happens after this return; a handler
            // task cancellation before return drops the execution and
            // converges as interrupted without replay.
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

pub(super) async fn handle_stream_inference(
    state: AppState,
    surface: ClientSurface,
    headers: HeaderMap,
    body: Bytes,
    session: Option<String>,
    proxy_request_id: String,
    lease: Arc<GenerationLease>,
) -> Response {
    let incoming = filtered_incoming_headers(&headers);
    let execution = match crate::coordinator::execute_stream(
        lease.generation().inference(),
        surface,
        body,
        incoming,
        session,
        proxy_request_id.clone(),
    )
    .await
    {
        Ok((execution, _virtual)) => execution,
        Err(error) => {
            let status = error.status();
            let detail = endpoint_error_body(surface, &error.to_string());
            return error_body_response(status, surface, detail);
        }
    };
    // Pre-handoff terminal error envelope (e.g. exhaustion without a live
    // stream): finite JSON body with the coordinator's status/headers.
    if let Some(error_body) = execution.error_body.clone() {
        let status = execution.headers.status;
        let mut outgoing = HeaderMap::new();
        for (name, value) in &execution.headers.headers {
            outgoing.insert(name.clone(), value.clone());
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
        outgoing.insert(name.clone(), value.clone());
    }
    // Drive the incremental body without buffering the complete stream:
    // each pulled chunk is forwarded as one Axum frame. Terminal ownership
    // is stored on clean or failed terminal; failures never retry.
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
                        // Downstream disconnected: drop converges as
                        // cancelled without replay.
                        drop(execution);
                        break;
                    }
                }
                None => {
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
                Some(Err(_)) => {
                    // Failed terminal after handoff: end the stream without
                    // replay; durable status already converges as error.
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
    let body = axum::body::Body::from_stream(stream);
    (status, outgoing, body).into_response()
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
    let mut outgoing = HeaderMap::new();
    for (name, value) in headers {
        let lower = name.as_str().to_ascii_lowercase();
        if matches!(
            lower.as_str(),
            "authorization"
                | "proxy-authorization"
                | "x-api-key"
                | "host"
                | "content-length"
                | "x-eggpool-route-session"
                | "connection"
                | "transfer-encoding"
                | "upgrade"
                | "keep-alive"
        ) {
            continue;
        }
        outgoing.insert(name.clone(), value.clone());
    }
    outgoing
}
