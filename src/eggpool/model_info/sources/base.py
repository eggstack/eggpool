"""Base protocol for model-info observation sources."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from eggpool.model_info.types import SourceModelRecord


@runtime_checkable
class ModelInfoSource(Protocol):
    """Protocol for sources that provide model metadata observations."""

    name: str

    @property
    def priority(self) -> int: ...

    async def fetch_all(self) -> list[SourceModelRecord]: ...

    async def fetch_one(
        self, model_id: str, *, provider_id: str | None = None
    ) -> SourceModelRecord | None: ...
