"""Request coordinator: central orchestration boundary for proxy lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

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
    QuotaExhaustedError,
    RateLimitError,
    UpstreamError,
)
from go_aggregator.health.health_manager import (
    FailureCategory,
    classify_failure_category,
)
from go_aggregator.proxy.client import filter_request_headers, filter_response_headers
from go_aggregator.proxy.sse_observer import IncrementalSSEObserver
from go_aggregator.proxy.usage import StreamUsageResult
from go_aggregator.request.attempt_finalizer import (
    AttemptFinalizationData,
    AttemptFinalizer,
)
from go_aggregator.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)
from go_aggregator.retry.classification import RetryCategory, RetryClassifier
from go_aggregator.security.redaction import redact_error_detail

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

# Default maximum retry attempts for pre-body failures
DEFAULT_MAX_RETRY_ATTEMPTS = 3


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
    client_metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    attempted_accounts: set[str] = field(default_factory=set[str])


@dataclass(frozen=True)
class SelectedAttempt:
    """Result of atomic pre-dispatch selection.

    Contains all data needed to execute an upstream request and finalize later.
    """

    proxy_request_id: str
    db_request_id: str
    attempt_id: int
    reservation_id: str
    account_id: int
    account_name: str
    api_key: str
    model_id: str
    estimated_tokens: int
    estimated_microdollars: int
    attempt_number: int


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
    - Create reservations and attempt records atomically before upstream dispatch
    - Open upstream connections
    - For non-streaming: read body, extract usage, calculate cost, finalize
    - For streaming: build streaming response with usage extraction
    - On error: finalize via RequestFinalizer, release reservation, update health
    - Pre-body failures retry on another account (excluding failed accounts)
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
        max_retry_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS,
        quota_exhausted_cooldown_seconds: float = 300.0,
        persist_error_detail: bool = False,
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
        self._max_retry_attempts = max_retry_attempts
        self._quota_exhausted_cooldown_seconds = quota_exhausted_cooldown_seconds
        self._persist_error_detail = persist_error_detail

        # Build the attempt finalizer with all dependencies
        self._attempt_finalizer = AttemptFinalizer(
            db=db,
            attempt_repo=attempt_repo or AttemptRepository(db),
            reservation_repo=reservation_repo or ReservationRepository(db),
            persist_error_detail=persist_error_detail,
        )

        # Build the finalizer with all dependencies
        self._finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo or RequestRepository(db),
            attempt_repo=attempt_repo or AttemptRepository(db),
            reservation_repo=reservation_repo or ReservationRepository(db),
            cost_calculator=cost_calculator,
            quota_estimator=quota_estimator,
            router=router,
            registry=registry,
            health_manager=health_manager,
            persist_error_detail=persist_error_detail,
        )

    async def execute(self, context: ProxyRequestContext) -> PreparedProxyResponse:
        """Execute a request through the full lifecycle.

        Returns a PreparedProxyResponse with either body (non-streaming)
        or stream_iterator (streaming). On retryable pre-body failures,
        retries on different accounts (excluding previously attempted ones).
        """
        # Section 10.5: Validate endpoint before durable selection.
        # Reject mismatched protocol endpoints before creating any
        # request, reservation, or attempt row.
        self._validate_endpoint(context)

        last_error: Exception | None = None
        last_upstream_response: tuple[int, dict[str, str], bytes] | None = None
        attempt_num = 0
        last_selected: SelectedAttempt | None = None

        for attempt_num in range(1, self._max_retry_attempts + 1):
            try:
                selected = await self._select_and_persist_attempt(context, attempt_num)
            except ModelUnavailableError as err:
                # Only overwrite last_error if we don't have an upstream error
                if last_error is None or not isinstance(
                    last_error, (_RetryableUpstreamError, _NonRetryableUpstreamError)
                ):
                    last_error = err
                # If a request row was created, finalize it as error so it
                # does not remain pending indefinitely.
                db_request_id = context.client_metadata.get("db_request_id")
                if db_request_id is not None and self._request_repo is not None:
                    async with self._db.transaction():
                        await self._request_repo.finalize_if_pending(
                            request_id=db_request_id,
                            status="error",
                            error_class=type(err).__name__,
                        )
                break
            except AuthenticationError as err:
                last_error = err
                logger.warning(
                    "Auth failure on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                # Auth failure on account selection - health already
                # updated by finalizer or selection. Retry with another
                # account if available.
                continue
            except (DatabaseError, Exception) as err:
                last_error = err
                logger.warning(
                    "Selection failed on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                break

            last_selected = selected
            try:
                return await self._execute_upstream(context, selected, attempt_num)
            except _RetryableUpstreamError as err:
                last_error = err
                # Track the last useful upstream response
                if err.upstream_response is not None:
                    last_upstream_response = err.upstream_response
                logger.warning(
                    "Retryable upstream error on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                # Finalize the failed attempt before retrying
                result = await self._attempt_finalizer.finalize_failed_attempt(
                    attempt_id=selected.attempt_id,
                    reservation_id=selected.reservation_id,
                    data=AttemptFinalizationData(
                        status_code=err.status_code,
                        error_class=err.error_class,
                        release_reason="attempt_retryable",
                    ),
                )
                # Clean up in-memory state when the attempt transitioned
                if result.attempt_transitioned:
                    if (
                        self._quota_estimator is not None
                        and result.reservation_released
                    ):
                        self._quota_estimator.remove_reservation(
                            selected.account_name,
                            selected.estimated_microdollars,
                        )
                    if result.reservation_released:
                        self._router.decrement_active_request_count(
                            selected.account_name
                        )
                # Apply health transitions only when the attempt transitioned
                if result.attempt_transitioned:
                    self._apply_health_transition(
                        selected.account_name, err, context.model_id
                    )
                if attempt_num >= self._max_retry_attempts:
                    break
                continue
            except _NonRetryableUpstreamError as err:
                last_error = err
                # Track the upstream response for pass-through
                if err.upstream_response is not None:
                    last_upstream_response = err.upstream_response
                logger.warning(
                    "Non-retryable upstream error on attempt %d for %s: %s",
                    attempt_num,
                    context.request_id,
                    err,
                )
                break

        # All retries exhausted or non-retryable error
        actual_attempts = attempt_num if last_selected is not None else 0
        return await self._handle_exhausted(
            context,
            last_error,
            last_upstream_response,
            actual_attempts,
            last_selected,
        )

    async def _select_and_persist_attempt(
        self,
        context: ProxyRequestContext,
        attempt_number: int,
    ) -> SelectedAttempt:
        """Atomically select an account, create request/reservation/attempt.

        All database writes happen inside a single transaction under the
        select lock. Nothing is committed until the transaction completes.
        """
        if (
            self._request_repo is None
            or self._reservation_repo is None
            or self._attempt_repo is None
        ):
            raise DatabaseError("Cannot persist: database repositories unavailable")

        async with self._select_lock, self._db.transaction():
            # 1. Get eligible account names excluding attempted ones
            eligible_account_names = self._router.get_eligible_account_names(
                context.model_id,
                exclude_accounts=context.attempted_accounts
                if context.attempted_accounts
                else None,
            )

            if not eligible_account_names:
                raise ModelUnavailableError(
                    f"No accounts available for model {context.model_id!r}"
                )

            # 2. Calculate projected request tokens once
            estimated_tokens = self._estimate_request_tokens(context)

            # 3. Build per-account estimate map for scoring
            request_estimates: dict[str, int] = {}
            if self._quota_estimator is not None:
                for acct_name in eligible_account_names:
                    request_estimates[acct_name] = self._quota_estimator.estimate_cost(
                        acct_name, context.model_id, estimated_tokens
                    )

            # 4. Select account using projected estimates
            selected_state = self._router.select_account(
                context.model_id,
                context.request_id,
                request_estimates=request_estimates,
                exclude_accounts=context.attempted_accounts,
            )
            if selected_state is None:
                raise ModelUnavailableError(
                    f"No accounts available for model {context.model_id!r}"
                )

            account_name = selected_state.name
            api_key = self._registry.get_api_key(account_name)
            if not api_key or not api_key.strip():
                raise AuthenticationError(
                    f"API key not available for account {account_name!r}"
                )

            # 5. Resolve account ID
            account_repo = AccountRepository(self._db)
            account_id = await account_repo.get_id_by_name(account_name)
            if account_id is None:
                raise DatabaseError(f"Account {account_name!r} not found in database")

            # 6. Create pending request if first attempt
            if "db_request_id" not in context.client_metadata:
                db_request_id = await self._request_repo.create_pending(
                    request_id=context.request_id,
                    model_id=context.model_id,
                    protocol=context.protocol,
                    streamed=context.streaming,
                    account_id=account_id,
                    started_at=context.started_at,
                )
                context.client_metadata["db_request_id"] = db_request_id
            db_request_id = context.client_metadata["db_request_id"]

            # 7. Use the exact estimate for the selected account
            estimated_microdollars = request_estimates.get(account_name, 0)
            if estimated_microdollars == 0 and self._quota_estimator is not None:
                estimated_microdollars = self._quota_estimator.estimate_cost(
                    account_name, context.model_id, estimated_tokens
                )

            # 8. Create reservation
            reservation_id = await self._reservation_repo.create(
                request_id=db_request_id,
                account_id=account_id,
                model_id=context.model_id,
                estimated_tokens=estimated_tokens,
                estimated_microdollars=estimated_microdollars,
            )

            # 9. Create attempt row
            attempt_id = await self._attempt_repo.create(
                request_id=db_request_id,
                attempt_number=attempt_number,
                account_id=account_id,
            )

            # 10. Update request with account and reserved amount
            await self._request_repo.update_after_selection(
                request_id=db_request_id,
                account_id=account_id,
                reserved_microdollars=estimated_microdollars,
            )

            # 11. Increment runtime active count
            self._router.increment_active_request_count(account_name)

            # 12. Add exact reserved amount to in-memory cache
            if self._quota_estimator is not None:
                self._quota_estimator.add_reservation(
                    account_name, estimated_microdollars
                )

            # Record the account under the same select lock so a
            # concurrent caller observing the same context cannot
            # race on attempted_accounts before this attempt is
            # fully persisted and committed.
            context.attempted_accounts.add(account_name)
            context.client_metadata["account_name"] = account_name

        # Transaction and select lock released here. All rows are
        # now durable and the attempted set is consistent.

        return SelectedAttempt(
            proxy_request_id=context.request_id,
            db_request_id=db_request_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            account_id=account_id,
            account_name=account_name,
            api_key=api_key,
            model_id=context.model_id,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=estimated_microdollars,
            attempt_number=attempt_number,
        )

    async def _execute_upstream(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        attempt_num: int,
    ) -> PreparedProxyResponse:
        """Execute the upstream HTTP call using the selected attempt."""
        try:
            if context.streaming:
                return await self._execute_streaming(context, selected, attempt_num)
            else:
                return await self._execute_non_streaming(context, selected, attempt_num)
        except asyncio.CancelledError:
            # Client cancellation after selection - finalize the attempt
            elapsed_ms = int((time.time() - context.started_at) * 1000)
            await self._finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.CLIENT_CANCELLED,
                    error_class="CancelledError",
                    upstream_latency_ms=elapsed_ms,
                ),
            )
            raise

    async def _execute_non_streaming(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        attempt_num: int,
    ) -> PreparedProxyResponse:
        """Execute a non-streaming request."""
        headers = filter_request_headers(
            dict(context.incoming_headers), selected.api_key
        )
        upstream_path = self._get_upstream_path(context.protocol)

        try:
            response = await self._client.request(
                "POST",
                upstream_path,
                headers=headers,
                content=context.original_body,
                timeout=self._client.timeout.read,
            )
        except httpx.ConnectError as err:
            raise _RetryableUpstreamError(
                f"Connection failed: {err}",
                status_code=None,
                error_class="ConnectError",
            ) from err
        except httpx.TimeoutException as err:
            raise _RetryableUpstreamError(
                f"Timeout: {err}",
                status_code=504,
                error_class="TimeoutException",
            ) from err
        except Exception as err:
            raise _RetryableUpstreamError(
                f"Upstream error: {err}",
                status_code=None,
                error_class=type(err).__name__,
            ) from err

        # Check for upstream errors before consuming body
        try:
            if response.status_code >= 400:
                resp_headers = filter_response_headers(response.headers)
                resp_body = response.content

                # Check if this is retryable
                error = self._classify_upstream_error(
                    response.status_code, resp_headers, body=resp_body
                )
                if error is not None:
                    # Retryable error - raise for retry
                    raise _RetryableUpstreamError(
                        str(error),
                        status_code=response.status_code,
                        error_class=type(error).__name__,
                        retry_after=getattr(error, "retry_after", None),
                        upstream_response=(
                            response.status_code,
                            resp_headers,
                            resp_body,
                        ),
                    ) from error

                # Non-retryable client error (400, 404) - finalize and pass through
                await self._finalize_non_retryable(
                    context, selected, response.status_code, resp_headers, resp_body
                )
                elapsed_ms = int((time.time() - context.started_at) * 1000)
                return PreparedProxyResponse(
                    status_code=response.status_code,
                    headers=resp_headers,
                    body=resp_body,
                    request_id=context.request_id,
                    account_name=selected.account_name,
                    latency_ms=elapsed_ms,
                    attempt_count=attempt_num,
                )

            # Success path
            body = response.content
            resp_headers = filter_response_headers(response.headers)
            elapsed_ms = int((time.time() - context.started_at) * 1000)

            usage = self._extract_non_stream_usage(context.protocol, body)
            upstream_req_id = resp_headers.get("x-request-id")

            # Finalize via RequestFinalizer
            await self._finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=response.status_code,
                    input_tokens=usage.input_tokens if usage else 0,
                    output_tokens=usage.output_tokens if usage else 0,
                    cache_read_tokens=usage.cache_read_tokens if usage else 0,
                    cache_write_tokens=usage.cache_creation_tokens if usage else 0,
                    reasoning_tokens=usage.reasoning_tokens if usage else 0,
                    thinking_characters=usage.thinking_characters if usage else 0,
                    first_byte_ms=0,
                    upstream_latency_ms=elapsed_ms,
                    bytes_emitted=len(body),
                    upstream_request_id=upstream_req_id,
                ),
            )

            resp_headers["x-proxy-request-id"] = context.request_id
            resp_headers["x-proxy-attempt-count"] = str(attempt_num)
            return PreparedProxyResponse(
                status_code=response.status_code,
                headers=resp_headers,
                body=body,
                request_id=context.request_id,
                account_name=selected.account_name,
                usage=usage,
                latency_ms=elapsed_ms,
                attempt_count=attempt_num,
            )
        finally:
            await response.aclose()

    async def _execute_streaming(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        attempt_num: int,
    ) -> PreparedProxyResponse:
        """Execute a streaming request."""
        headers = filter_request_headers(
            dict(context.incoming_headers), selected.api_key
        )
        upstream_path = self._get_upstream_path(context.protocol)

        # Inject stream_options.include_usage for OpenAI
        body_to_send = context.original_body
        if context.protocol == "openai":
            payload_obj: object
            try:
                payload_obj = json.loads(context.original_body)
            except (json.JSONDecodeError, ValueError):
                pass
            else:
                if isinstance(payload_obj, dict):
                    payload = cast("dict[str, Any]", payload_obj)
                    stream_opts_value: Any = payload.get("stream_options")
                    stream_opts: dict[str, Any]
                    if isinstance(stream_opts_value, dict):
                        stream_opts = cast("dict[str, Any]", stream_opts_value)
                    else:
                        stream_opts = {}
                    stream_opts["include_usage"] = True
                    payload["stream_options"] = stream_opts
                    body_to_send = json.dumps(payload).encode()

        request = self._client.build_request(
            "POST",
            upstream_path,
            headers=headers,
            content=body_to_send,
        )

        try:
            response = await self._client.send(request, stream=True)
        except httpx.ConnectError as err:
            raise _RetryableUpstreamError(
                f"Connection failed: {err}",
                status_code=None,
                error_class="ConnectError",
            ) from err
        except httpx.TimeoutException as err:
            raise _RetryableUpstreamError(
                f"Timeout: {err}",
                status_code=504,
                error_class="TimeoutException",
            ) from err
        except Exception as err:
            raise _RetryableUpstreamError(
                f"Upstream error: {err}",
                status_code=None,
                error_class=type(err).__name__,
            ) from err

        # Check upstream status before creating downstream response
        try:
            if response.status_code >= 400:
                await response.aread()
                resp_headers = filter_response_headers(response.headers)
                resp_body = response.content

                error = self._classify_upstream_error(
                    response.status_code, resp_headers, body=resp_body
                )
                if error is not None:
                    raise _RetryableUpstreamError(
                        str(error),
                        status_code=response.status_code,
                        error_class=type(error).__name__,
                        retry_after=getattr(error, "retry_after", None),
                        upstream_response=(
                            response.status_code,
                            resp_headers,
                            resp_body,
                        ),
                    ) from error

                # Non-retryable client error - finalize and raise for pass-through
                await self._finalize_non_retryable(
                    context, selected, response.status_code, resp_headers, resp_body
                )
                raise _NonRetryableUpstreamError(
                    f"Upstream returned {response.status_code}",
                    status_code=response.status_code,
                    upstream_response=(
                        response.status_code,
                        resp_headers,
                        resp_body,
                    ),
                )
        finally:
            if response.status_code >= 400:
                await response.aclose()

        # Build the response headers
        resp_headers = filter_response_headers(response.headers, streaming=True)
        resp_headers["x-proxy-request-id"] = context.request_id
        resp_headers["x-proxy-attempt-count"] = str(attempt_num)

        # Build streaming generator
        stream_iter = self._build_stream_generator(
            context=context,
            upstream_response=response,
            selected=selected,
            resp_headers=resp_headers,
        )

        return PreparedProxyResponse(
            status_code=response.status_code,
            headers=resp_headers,
            stream_iterator=stream_iter,
            request_id=context.request_id,
            account_name=selected.account_name,
            latency_ms=int((time.time() - context.started_at) * 1000),
            attempt_count=attempt_num,
        )

    def _build_stream_generator(
        self,
        context: ProxyRequestContext,
        upstream_response: httpx.Response,
        selected: SelectedAttempt,
        resp_headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """Build an async generator that streams upstream bytes downstream,
        extracts usage via IncrementalSSEObserver, and finalizes the request
        on completion."""
        observer = IncrementalSSEObserver(context.protocol)
        bytes_emitted = 0
        first_byte_ms = 0.0
        started = time.time()
        finalizer = self._finalizer
        persist_error_detail = self._persist_error_detail

        async def _stream() -> AsyncIterator[bytes]:
            nonlocal bytes_emitted, first_byte_ms
            try:
                async for chunk in upstream_response.aiter_bytes():
                    if first_byte_ms == 0.0:
                        first_byte_ms = (time.time() - started) * 1000

                    observer.observe(chunk)
                    bytes_emitted = observer.bytes_emitted

                    yield chunk

                # Stream completed - flush observer to process any remaining data
                observer.flush()
                usage_result = observer.usage

                # Finalize via RequestFinalizer
                await finalizer.finalize(
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.COMPLETED,
                        status_code=upstream_response.status_code,
                        input_tokens=usage_result.input_tokens,
                        output_tokens=usage_result.output_tokens,
                        cache_read_tokens=usage_result.cache_read_tokens,
                        cache_write_tokens=usage_result.cache_creation_tokens,
                        reasoning_tokens=usage_result.reasoning_tokens,
                        thinking_characters=usage_result.thinking_characters,
                        first_byte_ms=int(first_byte_ms) if first_byte_ms > 0 else None,
                        upstream_latency_ms=int((time.time() - started) * 1000),
                        bytes_emitted=bytes_emitted,
                        upstream_request_id=resp_headers.get("x-request-id"),
                    ),
                )

            except asyncio.CancelledError:
                # Client cancellation - finalize but don't penalize health
                observer.flush()
                usage_result = observer.usage
                await finalizer.finalize(
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.CLIENT_CANCELLED,
                        first_byte_ms=int(first_byte_ms) if first_byte_ms > 0 else None,
                        upstream_latency_ms=int((time.time() - started) * 1000),
                        bytes_emitted=bytes_emitted,
                        input_tokens=usage_result.input_tokens,
                        output_tokens=usage_result.output_tokens,
                        cache_read_tokens=usage_result.cache_read_tokens,
                        cache_write_tokens=usage_result.cache_creation_tokens,
                        reasoning_tokens=usage_result.reasoning_tokens,
                        thinking_characters=usage_result.thinking_characters,
                    ),
                )
                raise
            except Exception as exc:
                # Midstream error - finalize, no retry
                observer.flush()
                usage_result = observer.usage
                # Default is fail-closed: do not persist arbitrary
                # provider error detail. When explicitly enabled, the
                # strengthened redactor is applied and bounded.
                error_detail_value: str | None = None
                if persist_error_detail:
                    redacted = redact_error_detail(str(exc))
                    error_detail_value = (
                        redacted[:2048] if redacted is not None else None
                    )
                await finalizer.finalize(
                    selected,
                    FinalizationData(
                        outcome=FinalizationOutcome.MIDSTREAM_ERROR,
                        first_byte_ms=int(first_byte_ms) if first_byte_ms > 0 else None,
                        upstream_latency_ms=int((time.time() - started) * 1000),
                        bytes_emitted=bytes_emitted,
                        input_tokens=usage_result.input_tokens,
                        output_tokens=usage_result.output_tokens,
                        cache_read_tokens=usage_result.cache_read_tokens,
                        cache_write_tokens=usage_result.cache_creation_tokens,
                        reasoning_tokens=usage_result.reasoning_tokens,
                        thinking_characters=usage_result.thinking_characters,
                        error_class=type(exc).__name__,
                        error_detail=error_detail_value,
                    ),
                )
                raise
            finally:
                try:
                    await upstream_response.aclose()
                except Exception:
                    logger.debug("Error closing upstream response", exc_info=True)

        return _stream()

    def _extract_non_stream_usage(
        self, protocol: str, body: bytes
    ) -> StreamUsageResult | None:
        """Extract usage from a non-streaming response body."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Non-streaming upstream response body is not valid JSON; "
                "usage will not be extracted (body_len=%d)",
                len(body),
            )
            return None

        if protocol == "anthropic":
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
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> UpstreamError | None:
        """Classify an upstream error status code into an exception.

        Returns None for non-retryable client errors (400, non-model-specific 404)
        where the response body should be passed through as-is.
        """
        error = self._classifier.classify(status_code, headers, body=body)

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
        if error.category == RetryCategory.MODEL_UNAVAILABLE:
            return ModelUnavailableError(error.message, status_code=status_code)
        if error.category == RetryCategory.BAD_REQUEST:
            return None
        if error.category in (RetryCategory.TEMPORARY, RetryCategory.TRANSIENT):
            return UpstreamError(error.message, status_code=status_code)

        return None

    def _get_upstream_path(self, protocol: str) -> str:
        """Get the upstream path for a protocol."""
        if protocol == "anthropic":
            return "/messages"
        return "/chat/completions"

    def _apply_health_transition(
        self,
        account_name: str,
        err: _RetryableUpstreamError,
        model_id: str,
    ) -> None:
        """Apply health transitions for a failed account."""
        if self._health_manager is None:
            return

        category = classify_failure_category(err.error_class, err.status_code)
        if category == FailureCategory.AUTHENTICATION_FAILED:
            self._health_manager.record_failure(
                account_name, model_id=model_id, reason="authentication_failed"
            )
        elif category == FailureCategory.RATE_LIMITED:
            retry_after = err.retry_after or 60.0
            self._health_manager.record_rate_limit(account_name, retry_after)
        elif category == FailureCategory.QUOTA_EXHAUSTED:
            self._health_manager.record_quota_exhausted(
                account_name,
                self._quota_exhausted_cooldown_seconds,
            )
        elif category == FailureCategory.MODEL_UNAVAILABLE:
            self._health_manager.disable_model(account_name, model_id)
            self._catalog.cache.mark_model_unavailable(account_name, model_id)
        else:
            self._health_manager.record_failure(
                account_name, model_id=model_id, reason=category.value
            )

        # Also update runtime state with normalized category
        state = self._registry.get_state(account_name)
        if state is not None:
            state.record_failure(
                category.value,
                cooldown_seconds=self._quota_exhausted_cooldown_seconds,
                rate_limit_retry_after=err.retry_after,
            )

    async def _finalize_non_retryable(
        self,
        context: ProxyRequestContext,
        selected: SelectedAttempt,
        status_code: int,
        resp_headers: dict[str, str],
        resp_body: bytes,
    ) -> None:
        """Finalize a non-retryable client error (4xx)."""
        elapsed_ms = int((time.time() - context.started_at) * 1000)
        await self._finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.CLIENT_ERROR,
                status_code=status_code,
                upstream_latency_ms=elapsed_ms,
                bytes_emitted=len(resp_body),
                upstream_request_id=resp_headers.get("x-request-id"),
            ),
        )

    async def _handle_exhausted(
        self,
        context: ProxyRequestContext,
        last_error: Exception | None,
        last_upstream_response: tuple[int, dict[str, str], bytes] | None,
        attempt_num: int,
        last_selected: SelectedAttempt | None = None,
    ) -> PreparedProxyResponse:
        """Handle exhausted retries or non-retryable errors.

        Uses last_selected for finalization instead of reconstructing from DB.
        Preserves the last upstream response when available.
        """
        elapsed_ms = int((time.time() - context.started_at) * 1000)

        # Finalize the request if we have a selected attempt
        if last_selected is not None:
            # Determine outcome based on error type
            outcome = FinalizationOutcome.UPSTREAM_ERROR
            status_code = None
            error_class = None
            error_detail: str | None = None

            if last_upstream_response is not None:
                status_code = last_upstream_response[0]
            if last_error is not None:
                error_class = type(last_error).__name__
                # Default is fail-closed: do not persist arbitrary
                # provider error detail. When explicitly enabled, the
                # strengthened redactor is applied and bounded.
                if self._persist_error_detail:
                    redacted_detail = redact_error_detail(str(last_error))
                    error_detail = (
                        redacted_detail[:2048] if redacted_detail is not None else None
                    )
                if isinstance(last_error, _NonRetryableUpstreamError):
                    outcome = FinalizationOutcome.CLIENT_ERROR
                elif isinstance(last_error, _RetryableUpstreamError):
                    outcome = FinalizationOutcome.UPSTREAM_ERROR

            await self._finalizer.finalize(
                last_selected,
                FinalizationData(
                    outcome=outcome,
                    status_code=status_code,
                    error_class=error_class,
                    error_detail=error_detail,
                    upstream_latency_ms=elapsed_ms,
                    health_already_applied=True,
                ),
            )
        elif context.client_metadata.get("db_request_id") is not None:
            # No selected attempt but request exists - just mark as interrupted
            db_request_id = context.client_metadata["db_request_id"]
            # Default is fail-closed: do not persist arbitrary provider
            # error detail. When explicitly enabled, the strengthened
            # redactor is applied and bounded.
            if self._persist_error_detail and last_error is not None:
                redacted_detail = redact_error_detail(str(last_error))
                detail_value: str = (
                    redacted_detail[:2048]
                    if redacted_detail is not None
                    else "No attempts dispatched"
                )
            elif self._persist_error_detail:
                detail_value = "No attempts dispatched"
            else:
                detail_value = "No attempts dispatched"
            async with self._db.transaction():
                await self._db.execute_write(
                    "UPDATE requests SET status = 'error', "
                    "completed_at = CURRENT_TIMESTAMP, "
                    "error_class = ?, error_detail = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (
                        type(last_error).__name__ if last_error else "exhausted",
                        detail_value,
                        db_request_id,
                    ),
                )

        # Use last upstream response if available
        if last_upstream_response is not None:
            status, headers, body = last_upstream_response
            headers["x-proxy-request-id"] = context.request_id
            headers["x-proxy-attempt-count"] = str(attempt_num)
            return PreparedProxyResponse(
                status_code=status,
                headers=headers,
                body=body,
                request_id=context.request_id,
                account_name=context.client_metadata.get("account_name", ""),
                latency_ms=elapsed_ms,
                attempt_count=attempt_num,
            )

        # Generate a protocol-compatible proxy error envelope
        status_code = self._error_status_code(last_error)
        error_msg = str(last_error or "Request failed")
        if context.protocol == "anthropic":
            error_body = json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": error_msg,
                    },
                }
            ).encode()
        else:
            error_body = json.dumps(
                {
                    "error": {
                        "message": error_msg,
                        "type": "server_error",
                        "code": status_code,
                    }
                }
            ).encode()
        return PreparedProxyResponse(
            status_code=status_code,
            headers={
                "content-type": "application/json",
                "x-proxy-request-id": context.request_id,
                "x-proxy-attempt-count": str(attempt_num),
            },
            body=error_body,
            request_id=context.request_id,
            account_name=context.client_metadata.get("account_name", ""),
            latency_ms=elapsed_ms,
            attempt_count=attempt_num,
        )

    def _validate_endpoint(self, context: ProxyRequestContext) -> None:
        """Validate that the endpoint matches the model's protocol.

        Raises ProtocolMismatchError (which callers render as 400) when
        the wrong endpoint is used for a known model.
        """
        model_info = self._catalog.cache.get_model(context.model_id)
        if model_info is None:
            # Unknown model - handled later by _select_and_persist_attempt
            return

        model_protocol = model_info.get("protocol")
        if not model_protocol:
            # Unresolved protocol - fail closed
            raise ModelUnavailableError(
                f"Model {context.model_id!r} has unresolved protocol"
            )

        from go_aggregator.catalog.protocols import ModelProtocolResolver

        resolver = ModelProtocolResolver()
        resolver.validate_endpoint(model_protocol, context.protocol, context.model_id)

    @staticmethod
    def _estimate_request_tokens(context: ProxyRequestContext) -> int:
        """Estimate token count from the incoming request body.

        Uses a conservative heuristic: max(1_000, len(body) // 3),
        capped at 128_000 to avoid pathological reservations.
        """
        body = context.original_body
        if not body:
            return 1_000
        estimated = max(1_000, len(body) // 3)
        return min(estimated, 128_000)

    @staticmethod
    def _error_status_code(err: Exception | None) -> int:
        """Map an exception to an HTTP status code."""
        if err is None:
            return 500
        if isinstance(err, UpstreamError) and err.status_code is not None:
            return err.status_code
        if isinstance(err, _RetryableUpstreamError):
            if err.status_code is not None:
                return err.status_code
            return 502
        if isinstance(err, _NonRetryableUpstreamError):
            if err.status_code is not None:
                return err.status_code
            return 502
        if isinstance(err, AuthenticationError):
            return 502
        if isinstance(err, RateLimitError):
            return 429
        if isinstance(err, QuotaExhaustedError):
            return 503
        if isinstance(err, ModelUnavailableError):
            return 404
        return 502


class _RetryableUpstreamError(Exception):
    """An upstream error that can be retried on another account."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        error_class: str | None = None,
        retry_after: float | None = None,
        upstream_response: tuple[int, dict[str, str], bytes] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_class = error_class
        self.retry_after = retry_after
        self.upstream_response = upstream_response


class _NonRetryableUpstreamError(Exception):
    """An upstream error that should not be retried."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        upstream_response: tuple[int, dict[str, str], bytes] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_response = upstream_response
