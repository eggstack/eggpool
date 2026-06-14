"""Request coordinator: central orchestration boundary for proxy lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from go_aggregator.db.repositories import (
    AccountRepository,
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from go_aggregator.errors import (
    AuthenticationError,
    DatabaseError,
    ModelUnavailableError,
    ProxyError,
    QuotaExhaustedError,
    RateLimitError,
    UpstreamError,
)
from go_aggregator.proxy.client import filter_request_headers, filter_response_headers
from go_aggregator.proxy.usage import (
    AnthropicStreamUsageExtractor,
    OpenAIStreamUsageExtractor,
    StreamUsageResult,
)
from go_aggregator.retry.classification import RetryCategory, RetryClassifier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.catalog.pricing import CostCalculator
    from go_aggregator.catalog.service import CatalogService
    from go_aggregator.db.connection import Database
    from go_aggregator.health.health_manager import HealthManager
    from go_aggregator.quota.estimation import QuotaEstimator
    from go_aggregator.routing.router import Router

logger = logging.getLogger(__name__)

# Maximum number of retry attempts for pre-body failures
MAX_RETRY_ATTEMPTS = 3


@dataclass
class ProxyRequestContext:
    """Input context for a proxy request."""

    request_id: str
    protocol: str  # 'openai' or 'anthropic'
    model_id: str
    streaming: bool
    original_body: bytes
    incoming_headers: dict[str, str]
    started_at: float = field(default_factory=time.time)
    client_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreparedProxyResponse:
    """Result of executing a proxy request through the coordinator."""

    status_code: int
    headers: dict[str, str]
    body: bytes | None = None  # for non-streaming
    stream_iterator: AsyncIterator[bytes] | None = None  # for streaming
    request_id: str = ""
    account_name: str = ""
    usage: StreamUsageResult | None = None
    latency_ms: int = 0
    attempt_count: int = 1


class RequestCoordinator:
    """Orchestrates the full proxy request lifecycle.

    Responsibilities:
    - Create pending request records
    - Select accounts via router
    - Create reservations
    - Create attempt records
    - Open upstream connections
    - For non-streaming: read body, extract usage, calculate cost, finalize
    - For streaming: build streaming response with usage extraction
    - On error: finalize request, release reservation, update health
    - try/finally ensures cleanup
    - Pre-body failures can retry on another account
    """

    def __init__(
        self,
        registry: AccountRegistry,
        catalog: CatalogService,
        router: Router,
        db: Database,
        httpx_client: httpx.AsyncClient,
        request_repo: RequestRepository | None = None,
        reservation_repo: ReservationRepository | None = None,
        attempt_repo: AttemptRepository | None = None,
        usage_window_repo: UsageWindowRepository | None = None,
        health_manager: HealthManager | None = None,
        cost_calculator: CostCalculator | None = None,
        quota_estimator: QuotaEstimator | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._router = router
        self._db = db
        self._client = httpx_client
        self._request_repo = request_repo
        self._reservation_repo = reservation_repo
        self._attempt_repo = attempt_repo
        self._usage_window_repo = usage_window_repo
        self._health_manager = health_manager
        self._cost_calculator = cost_calculator
        self._quota_estimator = quota_estimator
        self._classifier = RetryClassifier()
        self._select_lock = asyncio.Lock()

    async def execute(self, context: ProxyRequestContext) -> PreparedProxyResponse:
        """Execute a request through the full lifecycle.

        Returns a PreparedProxyResponse with either body (non-streaming)
        or stream_iterator (streaming). On retryable pre-body failures,
        retries up to MAX_RETRY_ATTEMPTS on different accounts.
        """
        last_error: Exception | None = None
        attempt_num = 0
        for attempt_num in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                return await self._attempt_request(context, attempt_num)
            except AuthenticationError as err:
                last_error = err
                logger.warning(
                    "Auth failure on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                acct_name = context.client_metadata.get("account_name", "")
                if self._health_manager and acct_name:
                    self._health_manager.record_failure(
                        acct_name, reason="authentication_failed"
                    )
                self._update_account_state(acct_name, "authentication")
                break  # Don't retry auth failures
            except RateLimitError as err:
                last_error = err
                logger.warning(
                    "Rate limited on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                acct_name = context.client_metadata.get("account_name", "")
                if self._health_manager and acct_name:
                    self._health_manager.record_rate_limit(
                        acct_name, err.retry_after or 60.0
                    )
                # Track failure without cooldown - retry is allowed
                state = self._registry.get_state(acct_name)
                if state:
                    state.consecutive_failures += 1
                    state.last_failure_at = time.time()
                if attempt_num >= MAX_RETRY_ATTEMPTS:
                    break
            except (QuotaExhaustedError, ModelUnavailableError) as err:
                last_error = err
                logger.warning(
                    "Non-retryable upstream error on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                if isinstance(err, ModelUnavailableError) and self._health_manager:
                    self._health_manager.disable_model(
                        context.client_metadata.get("account_name", ""),
                        context.model_id,
                    )
                self._update_account_state(
                    context.client_metadata.get("account_name", ""),
                    "quota_exhausted",
                )
                break
            except httpx.ConnectError as err:
                last_error = ProxyError(f"Connection failed: {err}")
                logger.warning(
                    "Connection error on attempt %d for %s",
                    attempt_num,
                    context.request_id,
                )
                acct_name = context.client_metadata.get("account_name", "")
                if self._health_manager and acct_name:
                    self._health_manager.record_failure(acct_name)
                # Track failure without cooldown - retry is allowed
                state = self._registry.get_state(acct_name)
                if state:
                    state.consecutive_failures += 1
                    state.last_failure_at = time.time()
                if attempt_num >= MAX_RETRY_ATTEMPTS:
                    break
            except httpx.TimeoutException as err:
                last_error = UpstreamError(f"Timeout: {err}", status_code=504)
                logger.warning(
                    "Timeout on attempt %d for %s",
                    attempt_num,
                    context.request_id,
                )
                acct_name = context.client_metadata.get("account_name", "")
                if self._health_manager and acct_name:
                    self._health_manager.record_failure(acct_name)
                # Track failure without cooldown - retry is allowed
                state = self._registry.get_state(acct_name)
                if state:
                    state.consecutive_failures += 1
                    state.last_failure_at = time.time()
                if attempt_num >= MAX_RETRY_ATTEMPTS:
                    break
            except UpstreamError as err:
                last_error = err
                logger.warning(
                    "Upstream error on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                acct_name = context.client_metadata.get("account_name", "")
                if self._health_manager and acct_name:
                    self._health_manager.record_failure(acct_name)
                # Track failure without cooldown - 5xx errors are transient
                state = self._registry.get_state(acct_name)
                if state:
                    state.consecutive_failures += 1
                    state.last_failure_at = time.time()
                if attempt_num >= MAX_RETRY_ATTEMPTS:
                    break

        await self._finalize_request(context, "error")
        elapsed_ms = int((time.time() - context.started_at) * 1000)
        return PreparedProxyResponse(
            status_code=self._error_status_code(last_error),
            headers={"content-type": "application/json"},
            body=json.dumps({"error": str(last_error or "Request failed")}).encode(),
            request_id=context.request_id,
            latency_ms=elapsed_ms,
            attempt_count=attempt_num if "attempt_num" in dir() else 1,
        )

    def _update_account_state(
        self, account_name: str, error_class: str | None = None
    ) -> None:
        """Update AccountRuntimeState for health transitions."""
        state = self._registry.get_state(account_name)
        if state is None:
            return
        if error_class is None:
            state.record_success()
        else:
            state.record_failure(error_class)

    async def _attempt_request(
        self, context: ProxyRequestContext, attempt_num: int
    ) -> PreparedProxyResponse:
        """Execute a single attempt with account selection and upstream call."""
        async with self._select_lock:
            selected_state = self._router.select_account(
                context.model_id, context.request_id
            )
            if selected_state is None:
                raise ModelUnavailableError(
                    f"No accounts available for model {context.model_id!r}"
                )

            account_name = selected_state.name
            api_key = self._registry.get_api_key(account_name)
            if not api_key:
                raise AuthenticationError(
                    f"API key not available for account {account_name!r}"
                )

            # Store account_name for health tracking
            context.client_metadata["account_name"] = account_name

            # Create pending request record (only on first attempt)
            if "db_request_id" not in context.client_metadata:
                db_request_id = await self._ensure_request_record(context)
                context.client_metadata["db_request_id"] = db_request_id
            db_request_id = context.client_metadata["db_request_id"]

            # Create reservation (inside lock for atomicity)
            reservation_id = await self._create_reservation(
                context, account_name, db_request_id
            )
            # Track reservation in estimator for scoring
            if reservation_id and self._quota_estimator is not None:
                account_repo = AccountRepository(self._db)
                account_id = await account_repo.get_id_by_name(account_name)
                if account_id is not None:
                    estimated_microdollars = self._quota_estimator.estimate_cost(
                        account_name, context.model_id, 1000
                    )
                    self._quota_estimator.add_reservation(
                        account_name, estimated_microdollars
                    )

        # Create attempt record
        attempt_id = await self._create_attempt(
            context, attempt_num, account_name, db_request_id
        )

        try:
            if context.streaming:
                return await self._execute_streaming(
                    context, account_name, api_key, attempt_id
                )
            else:
                return await self._execute_non_streaming(
                    context, account_name, api_key, attempt_id
                )
        except Exception:
            # Release reservation on error
            if reservation_id and self._reservation_repo:
                await self._reservation_repo.release(reservation_id, "error")
            # Remove from estimator tracking
            if self._quota_estimator is not None:
                estimated = self._quota_estimator.estimate_cost(
                    account_name, context.model_id, 1000
                )
                self._quota_estimator.remove_reservation(account_name, estimated)
            raise

    async def _execute_non_streaming(
        self,
        context: ProxyRequestContext,
        account_name: str,
        api_key: str,
        attempt_id: int,
    ) -> PreparedProxyResponse:
        """Execute a non-streaming request."""
        headers = filter_request_headers(dict(context.incoming_headers), api_key)
        upstream_path = self._get_upstream_path(context.protocol)

        try:
            response = await self._client.request(
                "POST",
                upstream_path,
                headers=headers,
                content=context.original_body,
                timeout=300.0,
            )
        except Exception as err:
            await self._update_attempt(
                attempt_id,
                error_class=type(err).__name__,
                error_detail=str(err),
            )
            raise

        # Check for upstream errors before consuming body
        if response.status_code >= 400:
            resp_headers = filter_response_headers(response.headers)
            resp_headers["x-proxy-request-id"] = context.request_id
            error = self._classify_upstream_error(response.status_code, resp_headers)
            if error is not None:
                await self._update_attempt(
                    attempt_id,
                    status_code=response.status_code,
                    error_class=type(error).__name__,
                    error_detail=str(error),
                )
                raise error
            # Non-retryable client error (400, 404, etc.) - pass through
            await self._update_attempt(
                attempt_id,
                status_code=response.status_code,
            )
            elapsed_ms = int((time.time() - context.started_at) * 1000)
            return PreparedProxyResponse(
                status_code=response.status_code,
                headers=resp_headers,
                body=response.content,
                request_id=context.request_id,
                account_name=account_name,
                latency_ms=elapsed_ms,
                attempt_count=1,
            )

        # Read the response body
        body = response.content
        resp_headers = filter_response_headers(response.headers)
        elapsed_ms = int((time.time() - context.started_at) * 1000)

        # Extract usage
        usage = self._extract_non_stream_usage(context.protocol, body)

        # Record usage with quota estimator
        if usage and usage.input_tokens + usage.output_tokens > 0:
            total_tokens = usage.input_tokens + usage.output_tokens
            self._router.record_usage(account_name, total_tokens, 0)

        # Update attempt
        upstream_req_id = resp_headers.get("x-request-id")
        await self._update_attempt(
            attempt_id,
            status_code=response.status_code,
            upstream_request_id=upstream_req_id,
            bytes_emitted=len(body),
        )

        # Finalize request
        status = "completed" if response.status_code < 400 else "error"
        await self._finalize_request(
            context,
            status,
            status_code=response.status_code,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
        )

        # Record health success for completed requests
        if self._health_manager and status == "completed":
            self._health_manager.record_success(account_name, context.model_id)
        if status == "completed":
            self._update_account_state(account_name)

        # Remove reservation from estimator tracking
        if self._quota_estimator is not None:
            estimated = self._quota_estimator.estimate_cost(
                account_name, context.model_id, 1000
            )
            self._quota_estimator.remove_reservation(account_name, estimated)

        resp_headers["x-proxy-request-id"] = context.request_id
        return PreparedProxyResponse(
            status_code=response.status_code,
            headers=resp_headers,
            body=body,
            request_id=context.request_id,
            account_name=account_name,
            usage=usage,
            latency_ms=elapsed_ms,
            attempt_count=1,
        )

    async def _execute_streaming(
        self,
        context: ProxyRequestContext,
        account_name: str,
        api_key: str,
        attempt_id: int,
    ) -> PreparedProxyResponse:
        """Execute a streaming request. Opens upstream, checks status, builds
        streaming response that extracts usage during relay."""
        headers = filter_request_headers(dict(context.incoming_headers), api_key)
        upstream_path = self._get_upstream_path(context.protocol)

        # Inject stream_options.include_usage for OpenAI
        body_to_send = context.original_body
        if context.protocol == "openai":
            try:
                payload = json.loads(context.original_body)
                stream_opts = payload.get("stream_options", {})
                stream_opts["include_usage"] = True
                payload["stream_options"] = stream_opts
                body_to_send = json.dumps(payload).encode()
            except (json.JSONDecodeError, ValueError):
                pass

        # Use httpx low-level streaming API
        request = self._client.build_request(
            "POST",
            upstream_path,
            headers=headers,
            content=body_to_send,
        )
        response = await self._client.send(request, stream=True)

        # Check upstream status before creating downstream response
        if response.status_code >= 400:
            await response.aread()
            resp_headers = dict(response.headers)
            error = self._classify_upstream_error(response.status_code, resp_headers)
            if error is not None:
                await self._update_attempt(
                    attempt_id,
                    status_code=response.status_code,
                    error_class=type(error).__name__,
                    error_detail=str(error),
                )
                raise error
            # Non-retryable client error (400, 404, etc.) - raise
            # with status code so it can be passed through
            await self._update_attempt(
                attempt_id,
                status_code=response.status_code,
            )
            raise UpstreamError(
                f"Upstream returned {response.status_code}",
                status_code=response.status_code,
            )

        # Build the response headers
        resp_headers = filter_response_headers(response.headers)
        resp_headers["x-proxy-request-id"] = context.request_id

        # Build streaming generator
        stream_iter = self._build_stream_generator(
            context=context,
            upstream_response=response,
            account_name=account_name,
            attempt_id=attempt_id,
            resp_headers=resp_headers,
        )

        return PreparedProxyResponse(
            status_code=200,
            headers=resp_headers,
            stream_iterator=stream_iter,
            request_id=context.request_id,
            account_name=account_name,
            latency_ms=int((time.time() - context.started_at) * 1000),
            attempt_count=1,
        )

    def _build_stream_generator(
        self,
        context: ProxyRequestContext,
        upstream_response: httpx.Response,
        account_name: str,
        attempt_id: int,
        resp_headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """Build an async generator that streams upstream bytes downstream,
        extracts usage, and finalizes the request on completion."""
        # Select the appropriate usage extractor
        if context.protocol == "anthropic":
            extractor = AnthropicStreamUsageExtractor()
        else:
            extractor = OpenAIStreamUsageExtractor()

        usage_result = StreamUsageResult()
        bytes_emitted = 0
        first_byte_ms = 0.0
        started = time.time()

        async def _stream() -> AsyncIterator[bytes]:
            nonlocal bytes_emitted, first_byte_ms
            try:
                async for chunk in upstream_response.aiter_bytes():
                    if first_byte_ms == 0.0:
                        first_byte_ms = (time.time() - started) * 1000

                    bytes_emitted += len(chunk)

                    # Try to extract usage from the chunk text
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                        for line in text.split("\n"):
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str.strip() == "[DONE]":
                                    continue
                                try:
                                    data = json.loads(data_str)
                                    usage = extractor.extract(data)
                                    if usage:
                                        _merge_usage(usage_result, usage)
                                except (json.JSONDecodeError, ValueError):
                                    pass
                    except Exception:
                        pass

                    yield chunk

                # Stream completed successfully
                int((time.time() - context.started_at) * 1000)

                # Record usage
                total_tokens = usage_result.input_tokens + usage_result.output_tokens
                if total_tokens > 0:
                    self._router.record_usage(account_name, total_tokens, 0)

                # Update attempt
                upstream_req_id = resp_headers.get("x-request-id")
                await self._update_attempt(
                    attempt_id,
                    status_code=200,
                    upstream_request_id=upstream_req_id,
                    bytes_emitted=bytes_emitted,
                )

                # Finalize request
                await self._finalize_request(
                    context,
                    "completed",
                    status_code=200,
                    input_tokens=usage_result.input_tokens,
                    output_tokens=usage_result.output_tokens,
                    thinking_characters=usage_result.thinking_characters,
                )

                # Record health success
                if self._health_manager:
                    self._health_manager.record_success(account_name, context.model_id)

                # Remove reservation from estimator tracking
                if self._quota_estimator is not None:
                    estimated = self._quota_estimator.estimate_cost(
                        account_name, context.model_id, 1000
                    )
                    self._quota_estimator.remove_reservation(account_name, estimated)

            except Exception as exc:
                # Finalize on error
                await self._update_attempt(
                    attempt_id,
                    error_class=type(exc).__name__,
                    error_detail=str(exc),
                    bytes_emitted=bytes_emitted,
                )
                await self._finalize_request(
                    context,
                    "error",
                    error_class=type(exc).__name__,
                    error_detail=str(exc),
                )
                if self._health_manager:
                    self._health_manager.record_failure(account_name, context.model_id)
                # Remove reservation from estimator tracking
                if self._quota_estimator is not None:
                    estimated = self._quota_estimator.estimate_cost(
                        account_name, context.model_id, 1000
                    )
                    self._quota_estimator.remove_reservation(account_name, estimated)
                raise
            finally:
                # Always close upstream response
                await upstream_response.aclose()

        return _stream()

    def _extract_non_stream_usage(
        self, protocol: str, body: bytes
    ) -> StreamUsageResult | None:
        """Extract usage from a non-streaming response body."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

        if protocol == "anthropic":
            # Anthropic puts usage in top-level "usage"
            usage = data.get("usage")
            if not usage:
                return None
            return StreamUsageResult(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                is_complete=True,
            )
        else:
            # OpenAI puts usage in top-level "usage"
            usage = data.get("usage")
            if not usage:
                return None
            return StreamUsageResult(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cache_read_tokens=usage.get("prompt_tokens_details", {}).get(
                    "cached_tokens", 0
                ),
                reasoning_tokens=usage.get("completion_tokens_details", {}).get(
                    "reasoning_tokens", 0
                ),
                is_complete=True,
            )

    def _classify_upstream_error(
        self, status_code: int, headers: dict[str, str]
    ) -> UpstreamError | None:
        """Classify an upstream error status code into an exception.

        Returns None for non-retryable client errors (400, 404) where the
        response body should be passed through as-is.
        """
        error = self._classifier.classify(status_code, headers)

        if error.category == RetryCategory.AUTH_FAILURE:
            return AuthenticationError(error.message, status_code=status_code)
        if error.category == RetryCategory.QUOTA_EXCEEDED:
            if status_code == 429:
                retry_after = error.retry_after
                return RateLimitError(
                    error.message,
                    status_code=status_code,
                    retry_after=retry_after if retry_after is not None else 60.0,
                )
            return QuotaExhaustedError(error.message, status_code=status_code)
        if error.category == RetryCategory.BAD_REQUEST:
            # Client errors - return None so the response body is
            # passed through as-is with its original status code
            return None
        if error.category in (RetryCategory.TEMPORARY, RetryCategory.TRANSIENT):
            return UpstreamError(error.message, status_code=status_code)

        return None

    def _get_upstream_path(self, protocol: str) -> str:
        """Get the upstream path for a protocol."""
        if protocol == "anthropic":
            return "/messages"
        return "/chat/completions"

    async def _ensure_request_record(self, context: ProxyRequestContext) -> str:
        """Create a pending request record in the database.

        Returns the database row ID.
        """
        if self._request_repo is None:
            raise DatabaseError("Cannot persist request: database unavailable")
        account_name = context.client_metadata.get("account_name")
        account_id = None
        if account_name:
            acct_repo = AccountRepository(self._db)
            account_id = await acct_repo.get_id_by_name(account_name)

        return await self._request_repo.create_pending(
            request_id=context.request_id,
            model_id=context.model_id,
            protocol=context.protocol,
            streamed=context.streaming,
            account_id=account_id,
            started_at=context.started_at,
        )

    async def _create_reservation(
        self,
        context: ProxyRequestContext,
        account_name: str,
        db_request_id: str,
    ) -> str | None:
        """Create a reservation for the selected account."""
        if self._reservation_repo is None:
            raise DatabaseError("Cannot create reservation: database unavailable")
        account_repo = AccountRepository(self._db)
        account_id = await account_repo.get_id_by_name(account_name)
        if account_id is None:
            return None

        # Estimate cost for the reservation
        estimated_tokens = 1000
        estimated_microdollars = 0
        if self._quota_estimator is not None:
            estimated_microdollars = self._quota_estimator.estimate_cost(
                account_name, context.model_id, estimated_tokens
            )

        return await self._reservation_repo.create(
            request_id=db_request_id,
            account_id=account_id,
            model_id=context.model_id,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=estimated_microdollars,
        )

    async def _create_attempt(
        self,
        context: ProxyRequestContext,
        attempt_num: int,
        account_name: str,
        db_request_id: str,
    ) -> int:
        """Create an attempt record."""
        if self._attempt_repo is None:
            raise DatabaseError("Cannot create attempt: database unavailable")
        account_repo = AccountRepository(self._db)
        account_id = await account_repo.get_id_by_name(account_name)
        if account_id is None:
            return 0
        return await self._attempt_repo.create(
            request_id=db_request_id,
            attempt_number=attempt_num,
            account_id=account_id,
        )

    async def _update_attempt(
        self,
        attempt_id: int,
        status_code: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        upstream_request_id: str | None = None,
        bytes_emitted: int = 0,
    ) -> None:
        """Update an attempt record."""
        if self._attempt_repo is None or attempt_id == 0:
            return
        await self._attempt_repo.update(
            attempt_id=attempt_id,
            status_code=status_code,
            error_class=error_class,
            error_detail=error_detail,
            upstream_request_id=upstream_request_id,
            bytes_emitted=bytes_emitted,
        )

    async def _finalize_request(
        self,
        context: ProxyRequestContext,
        status: str,
        status_code: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_characters: int = 0,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Finalize the request record and release reservations."""
        elapsed_ms = int((time.time() - context.started_at) * 1000)

        # Calculate cost if calculator available
        cost_microdollars = 0
        exactness = "unknown"
        if self._cost_calculator is not None and (
            input_tokens > 0 or output_tokens > 0
        ):
            cost_microdollars, exactness = await self._cost_calculator.calculate_cost(
                context.model_id, input_tokens, output_tokens
            )

        # For interrupted/error requests with no usage, use reservation
        # estimate as conservative cost
        if (
            cost_microdollars == 0
            and exactness == "unknown"
            and status != "completed"
            and self._quota_estimator is not None
        ):
            account_name = context.client_metadata.get("account_name", "")
            if account_name:
                estimated = self._quota_estimator.estimate_cost(
                    account_name, context.model_id, 1000
                )
                if estimated > 0:
                    cost_microdollars = estimated
                    exactness = "estimated"

        # Use the DB row ID for updates
        db_request_id = context.client_metadata.get("db_request_id", context.request_id)

        if self._request_repo:
            await self._request_repo.update_after_completion(
                request_id=db_request_id,
                status=status,
                status_code=status_code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microdollars=cost_microdollars,
                exactness=exactness,
                upstream_latency_ms=elapsed_ms,
                error_class=error_class,
                error_detail=error_detail,
                thinking_characters=thinking_characters,
            )

        if self._reservation_repo:
            await self._reservation_repo.release_for_request(
                request_id=db_request_id,
                reason=status,
            )

        await self._db.connection.commit()

    @staticmethod
    def _error_status_code(err: Exception | None) -> int:
        """Map an exception to an HTTP status code."""
        if err is None:
            return 500
        # Use upstream status code if available
        if isinstance(err, UpstreamError) and err.status_code is not None:
            return err.status_code
        if isinstance(err, AuthenticationError):
            return 502
        if isinstance(err, RateLimitError):
            return 429
        if isinstance(err, QuotaExhaustedError):
            return 503
        if isinstance(err, ModelUnavailableError):
            return 404
        return 502


def _merge_usage(target: StreamUsageResult, incoming: StreamUsageResult) -> None:
    """Merge incoming usage into target."""
    target.input_tokens += incoming.input_tokens
    target.output_tokens += incoming.output_tokens
    target.cache_read_tokens += incoming.cache_read_tokens
    target.cache_creation_tokens += incoming.cache_creation_tokens
    target.reasoning_tokens += incoming.reasoning_tokens
    target.thinking_characters += incoming.thinking_characters
    if incoming.is_complete:
        target.is_complete = True
