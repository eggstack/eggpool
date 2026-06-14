"""In-memory model catalog cache."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ModelCatalogCache:
    """In-memory cache of the model catalog."""

    def __init__(self) -> None:
        # model_id -> model info dict
        self._models: dict[str, dict[str, Any]] = {}
        # model_id -> set of account names that support it
        self._account_support: dict[str, set[str]] = {}
        self._last_refresh: float = 0.0

    def update_from_account(
        self,
        account_name: str,
        models: list[dict[str, Any]],
    ) -> None:
        """Update cache with models from a specific account."""
        now = time.time()
        for model in models:
            model_id = model["model_id"]
            if model_id not in self._models:
                self._models[model_id] = {
                    "model_id": model_id,
                    "display_name": model.get("display_name"),
                    "protocol": model.get("protocol"),
                    "protocol_source": model.get("protocol_source"),
                    "capabilities": model.get("capabilities", {}),
                    "source_metadata": model.get("source_metadata", {}),
                    "first_seen_at": now,
                    "last_seen_at": now,
                }
            else:
                self._models[model_id]["last_seen_at"] = now
                # Update protocol if more specific
                if model.get("protocol"):
                    self._models[model_id]["protocol"] = model["protocol"]
                # Update source if provided
                if model.get("protocol_source"):
                    self._models[model_id]["protocol_source"] = model["protocol_source"]

            if model_id not in self._account_support:
                self._account_support[model_id] = set()
            self._account_support[model_id].add(account_name)

        self._last_refresh = now

    def mark_account_models_unavailable(self, account_name: str) -> None:
        """Mark all models as unavailable for an account."""
        for _model_id, accounts in self._account_support.items():
            accounts.discard(account_name)

    def mark_model_unavailable(self, account_name: str, model_id: str) -> None:
        """Mark a specific model as unavailable for an account."""
        if model_id in self._account_support:
            self._account_support[model_id].discard(account_name)

    def get_models_for_exposure(
        self,
        expose_mode: str,
        eligible_account_names: set[str],
    ) -> list[dict[str, Any]]:
        """Get models to expose based on the configured mode.

        Excludes models with unresolved protocol (None) since they
        cannot be routed to any endpoint.
        """
        result = []

        for model_id, model_info in self._models.items():
            # Fail-closed: do not expose unresolved models
            if not model_info.get("protocol"):
                continue

            accounts_supporting = self._account_support.get(model_id, set())
            visible_accounts = accounts_supporting & eligible_account_names

            should_include = (
                (expose_mode == "union" and visible_accounts)
                or (
                    expose_mode == "intersection"
                    and eligible_account_names
                    and visible_accounts == eligible_account_names
                )
                or (expose_mode == "healthy_union" and visible_accounts)
            )

            if should_include:
                model_info_copy = dict(model_info)
                model_info_copy["available_accounts"] = sorted(visible_accounts)
                result.append(model_info_copy)

        return sorted(result, key=lambda m: m["model_id"])

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        """Get a specific model from the cache."""
        return self._models.get(model_id)

    def get_supporting_accounts(self, model_id: str) -> set[str]:
        """Get set of account names that support a model."""
        return self._account_support.get(model_id, set()).copy()

    def is_model_available(self, model_id: str, eligible_accounts: set[str]) -> bool:
        """Check if a model is available from any eligible account."""
        supporting = self._account_support.get(model_id, set())
        return bool(supporting & eligible_accounts)

    @property
    def last_refresh(self) -> float:
        return self._last_refresh

    @property
    def model_count(self) -> int:
        return len(self._models)

    def is_stale(self, max_age_s: float) -> bool:
        """Check if the cache is older than max_age_s."""
        if self._last_refresh == 0:
            return True
        return (time.time() - self._last_refresh) > max_age_s

    def load_model(
        self,
        model_id: str,
        display_name: str | None,
        protocol: str,
        capabilities: dict[str, Any],
        source_metadata: dict[str, Any],
    ) -> None:
        """Load a model from database into cache."""
        self._models[model_id] = {
            "model_id": model_id,
            "display_name": display_name,
            "protocol": protocol,
            "capabilities": capabilities,
            "source_metadata": source_metadata,
            "first_seen_at": 0,
            "last_seen_at": 0,
        }

    def add_account_support(self, model_id: str, account_name: str) -> None:
        """Add account support for a model."""
        if model_id not in self._account_support:
            self._account_support[model_id] = set()
        self._account_support[model_id].add(account_name)

    def has_model(self, model_id: str) -> bool:
        """Check if model exists in cache."""
        return model_id in self._models

    def get_all_models(self) -> dict[str, dict[str, Any]]:
        """Get all models in cache."""
        return dict(self._models)

    def get_supporting_accounts_for_model(self, model_id: str) -> set[str]:
        """Get supporting accounts for a model."""
        return self._account_support.get(model_id, set()).copy()
