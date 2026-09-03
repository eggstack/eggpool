"""Semantic model-router selection through the ordinary concrete lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from eggpool.model_router.prompt import (
    compile_repair_prompt,
    compile_selector_prompt,
    parse_route_id,
)
from eggpool.request.internal_dispatch import prepare_internal_concrete_request
from eggpool.routing.provider import parse_model_provider

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from eggpool.model_router.registry import CompiledModelRoute, CompiledModelRouter
    from eggpool.request.coordinator import PreparedProxyResponse, RequestCoordinator
    from eggpool.wire.ir import CanonicalSurface

logger = logging.getLogger(__name__)

FallbackReason = Literal["timeout", "unavailable", "invalid_output", "repair_failed"]


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """A safe semantic route decision returned by :class:`ModelRouterSelector`."""

    virtual_model: str
    route_id: str
    route_label: str
    concrete_model: str
    source: Literal["selector", "default"]
    selector_attempts: int
    selector_latency_ms: float | None
    fallback_reason: FallbackReason | None = None
    repair_attempted: bool = False
    repair_succeeded: bool = False


class ModelRouterSelector:
    """Resolve one compiled router using bounded internal selector calls."""

    def __init__(
        self,
        coordinator: RequestCoordinator,
        *,
        known_provider_ids: Collection[str] | None = None,
        max_response_bytes: int = 16 * 1024,
    ) -> None:
        self._coordinator = coordinator
        if known_provider_ids is None:
            known_provider_ids = cast(
                "Collection[str]",
                getattr(coordinator, "known_provider_ids", ()),
            )
        self._known_provider_ids = tuple(known_provider_ids)
        self._max_response_bytes = max_response_bytes

    async def select(
        self,
        router: CompiledModelRouter,
        payload: Mapping[str, Any],
        *,
        client_surface: CanonicalSurface = "chat_completions",
        protocol: str | None = None,
    ) -> ModelSelection:
        """Return a selector route or the configured default route.

        The timeout covers the initial request and the optional repair. A
        cancellation from the parent task is deliberately not converted into
        a default decision; an internal timeout is converted only after the
        coordinator's cancellation cleanup has completed.
        """
        started = time.monotonic()
        attempts = 0
        fallback_reason: FallbackReason | None = None
        repair_attempted = False
        repair_succeeded = False
        try:
            async with asyncio.timeout(router.selector_timeout_s):
                prompt = compile_selector_prompt(
                    router,
                    payload,
                    client_surface=client_surface,
                    protocol=protocol,
                )
                attempts = 1
                result = await self._execute_selector(router, prompt.payload)
                route_id: str | None = None
                if not 200 <= result.status_code < 300:
                    fallback_reason = "unavailable"
                else:
                    route_id = parse_route_id(
                        result.body,
                        router,
                        max_response_bytes=self._max_response_bytes,
                    )
                if (
                    route_id is None
                    and router.repair_attempts
                    and fallback_reason is None
                ):
                    repair_attempted = True
                    repair = compile_repair_prompt(router, prompt)
                    attempts = 2
                    result = await self._execute_selector(router, repair)
                    if not 200 <= result.status_code < 300:
                        fallback_reason = "unavailable"
                    else:
                        route_id = parse_route_id(
                            result.body,
                            router,
                            max_response_bytes=self._max_response_bytes,
                        )
                        if route_id is not None:
                            repair_succeeded = True
                        else:
                            fallback_reason = "repair_failed"
                elif route_id is None and fallback_reason is None:
                    fallback_reason = "invalid_output"
                if route_id is not None:
                    route = router.route_by_id[route_id]
                    return self._selection(
                        router,
                        route,
                        source="selector",
                        attempts=attempts,
                        started=started,
                        fallback_reason=None,
                        repair_attempted=repair_attempted,
                        repair_succeeded=repair_succeeded,
                    )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            fallback_reason = "timeout"
        except Exception as exc:
            # Diagnostics intentionally contain only a type, never prompt,
            # response, provider, credential, or request content.
            logger.debug(
                "model-router selector fell back: virtual_model=%s "
                "error_type=%s attempts=%d",
                router.virtual_model,
                type(exc).__name__,
                attempts,
            )
            fallback_reason = "unavailable"
        route = self._default_route(router)
        return self._selection(
            router,
            route,
            source="default",
            attempts=attempts,
            started=started,
            fallback_reason=fallback_reason or "unavailable",
            repair_attempted=repair_attempted,
            repair_succeeded=repair_succeeded,
        )

    async def resolve(
        self,
        router: CompiledModelRouter,
        payload: Mapping[str, Any],
        *,
        client_surface: CanonicalSurface = "chat_completions",
        protocol: str | None = None,
    ) -> ModelSelection:
        """Compatibility verb for callers treating selection as resolution."""
        return await self.select(
            router,
            payload,
            client_surface=client_surface,
            protocol=protocol,
        )

    async def _execute_selector(
        self,
        router: CompiledModelRouter,
        payload: Mapping[str, Any],
    ) -> PreparedProxyResponse:
        selector_model, _provider_id = parse_model_provider(
            router.selector_model,
            self._known_provider_ids,
        )
        context = prepare_internal_concrete_request(
            payload,
            model_id=selector_model,
            known_provider_ids=self._known_provider_ids,
            request_id=str(uuid.uuid4()),
        )
        return await self._coordinator.execute(context)

    @staticmethod
    def _default_route(router: CompiledModelRouter) -> CompiledModelRoute:
        for route in router.routes:
            if route.model == router.default_model:
                return route
        # Structural configuration validation makes this unreachable. Keep a
        # typed guard here so a malformed hand-built compiled object cannot
        # inject an arbitrary model into a caller.
        raise ValueError("compiled router default_model has no route")

    @staticmethod
    def _selection(
        router: CompiledModelRouter,
        route: CompiledModelRoute,
        *,
        source: Literal["selector", "default"],
        attempts: int,
        started: float,
        fallback_reason: FallbackReason | None,
        repair_attempted: bool,
        repair_succeeded: bool,
    ) -> ModelSelection:
        return ModelSelection(
            virtual_model=router.virtual_model,
            route_id=route.route_id,
            route_label=route.label,
            concrete_model=route.model,
            source=source,
            selector_attempts=attempts,
            selector_latency_ms=(time.monotonic() - started) * 1000.0,
            fallback_reason=fallback_reason,
            repair_attempted=repair_attempted,
            repair_succeeded=repair_succeeded,
        )


__all__ = ["ModelRouterSelector", "ModelSelection"]
