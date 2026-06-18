"""In-memory model catalog cache."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def parse_model_id(model_id: str) -> tuple[str, str | None]:
    """Parse a model ID that may contain a provider suffix.

    Returns (base_model_id, provider_id) where provider_id is None
    when no suffix is present.
    """
    if "/" in model_id:
        base, provider = model_id.rsplit("/", 1)
        return base, provider
    return model_id, None


class ModelCatalogCache:
    """In-memory cache of the model catalog."""

    def __init__(self) -> None:
        # model_id -> model info dict
        self._models: dict[str, dict[str, Any]] = {}
        # model_id -> set of account names that support it
        self._account_support: dict[str, set[str]] = {}
        # account_name -> provider_id
        self._account_providers: dict[str, str] = {}
        self._last_refresh: float = 0.0
        # Per-account last successful refresh timestamp
        self._account_last_refresh: dict[str, float] = {}

    def update_from_account(
        self,
        account_name: str,
        provider_id: str,
        models: list[dict[str, Any]],
    ) -> None:
        """Update cache with models from a specific account."""
        self._account_providers[account_name] = provider_id
        now = time.time()
        # A refresh response is authoritative for this account. Clear
        # prior support first so models withdrawn upstream stop being
        # exposed or routed for this account while other accounts'
        # support remains intact.
        self.mark_account_models_unavailable(account_name)
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
                self._models[model_id]["display_name"] = model.get("display_name")
                # Always update protocol to reflect current upstream state;
                # an empty protocol means unresolved and should clear any
                # previously resolved value.
                self._models[model_id]["protocol"] = model.get("protocol")
                # Update source if provided
                if model.get("protocol_source"):
                    self._models[model_id]["protocol_source"] = model["protocol_source"]
                self._models[model_id]["capabilities"] = model.get("capabilities", {})
                self._models[model_id]["source_metadata"] = model.get(
                    "source_metadata", {}
                )

            if model_id not in self._account_support:
                self._account_support[model_id] = set()
            self._account_support[model_id].add(account_name)

        self._last_refresh = now
        self._account_last_refresh[account_name] = now

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
        result: list[dict[str, Any]] = []

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

    def get_provider_suffixed_models(
        self,
        expose_mode: str,
        eligible_account_names: set[str],
    ) -> list[dict[str, Any]]:
        """Get models with provider-suffixed IDs for client exposure.

        For each (model_id, provider_id) pair where at least one account
        from that provider supports the model, generate a client-facing
        model ID like 'model-id/provider-id'.
        """
        # Build provider -> eligible accounts mapping
        provider_accounts: dict[str, set[str]] = {}
        for account_name in eligible_account_names:
            pid = self._account_providers.get(account_name)
            if pid:
                provider_accounts.setdefault(pid, set()).add(account_name)

        result: list[dict[str, Any]] = []
        for model_id, model_info in self._models.items():
            if not model_info.get("protocol"):
                continue

            accounts_supporting = self._account_support.get(model_id, set())

            # Group supporting accounts by provider
            provider_support: dict[str, set[str]] = {}
            for acct in accounts_supporting:
                pid = self._account_providers.get(acct)
                if pid:
                    provider_support.setdefault(pid, set()).add(acct)

            for pid, eligible_in_provider in provider_accounts.items():
                supporting_in_provider = provider_support.get(pid, set())
                visible = supporting_in_provider & eligible_in_provider

                should_include = (
                    (expose_mode == "union" and visible)
                    or (
                        expose_mode == "intersection"
                        and eligible_in_provider
                        and visible == eligible_in_provider
                    )
                    or (expose_mode == "healthy_union" and visible)
                )

                if should_include:
                    suffixed_id = f"{model_id}/{pid}"
                    model_copy = dict(model_info)
                    model_copy["model_id"] = suffixed_id
                    model_copy["base_model_id"] = model_id
                    model_copy["provider_id"] = pid
                    model_copy["available_accounts"] = sorted(visible)
                    result.append(model_copy)

        return sorted(result, key=lambda m: m["model_id"])

    def set_account_provider(self, account_name: str, provider_id: str) -> None:
        """Record which provider an account belongs to."""
        self._account_providers[account_name] = provider_id

    def get_provider_for_account(self, account_name: str) -> str | None:
        """Get the provider ID for an account."""
        return self._account_providers.get(account_name)

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        """Get a specific model from the cache."""
        return self._models.get(model_id)

    def get_supporting_accounts(self, model_id: str) -> set[str]:
        """Get set of account names that support a model."""
        return self._account_support.get(model_id, set()).copy()

    def is_model_available(self, model_id: str, eligible_accounts: set[str]) -> bool:
        """Check if a model is available from any eligible account."""
        model_info = self._models.get(model_id)
        if model_info is None or not model_info.get("protocol"):
            return False
        supporting = self._account_support.get(model_id, set())
        return bool(supporting & eligible_accounts)

    @property
    def last_refresh(self) -> float:
        return self._last_refresh

    def hydrate_refresh_age(self) -> None:
        """Set _last_refresh to the newest last_seen_at across loaded models.

        Called after loading cached models from the database so that
        staleness checks reflect the actual age of the cached data
        rather than always reporting fresh-on-startup.
        """
        if not self._models:
            return
        newest = max(info.get("last_seen_at", 0.0) for info in self._models.values())
        if newest > self._last_refresh:
            self._last_refresh = newest

    def hydrate_account_refresh_ages(self) -> None:
        """Set per-account refresh timestamps from loaded model data.

        Called after loading cached account-model relationships so that
        ``is_account_stale()`` does not reject every supporting account
        that lacks a prior in-memory refresh timestamp.
        """
        for model_id, accounts in self._account_support.items():
            model_info = self._models.get(model_id)
            if model_info is None:
                continue
            last_seen = model_info.get("last_seen_at", 0.0)
            if last_seen <= 0:
                continue
            for account_name in accounts:
                existing = self._account_last_refresh.get(account_name, 0.0)
                if last_seen > existing:
                    self._account_last_refresh[account_name] = last_seen

    @property
    def model_count(self) -> int:
        return len(self._models)

    def is_stale(self, max_age_s: float) -> bool:
        """Check if the cache is older than max_age_s."""
        if self._last_refresh == 0:
            return True
        return (time.time() - self._last_refresh) > max_age_s

    def is_account_stale(self, account_name: str, max_age_s: float) -> bool:
        """Check if an account's catalog data is older than max_age_s."""
        last = self._account_last_refresh.get(account_name)
        if last is None or last == 0:
            return True
        return (time.time() - last) > max_age_s

    def get_fresh_supporting_accounts(
        self, model_id: str, max_age_s: float
    ) -> set[str]:
        """Get supporting accounts for a model that refreshed within max_age_s."""
        supporting = self._account_support.get(model_id, set())
        return {
            name for name in supporting if not self.is_account_stale(name, max_age_s)
        }

    def load_model(
        self,
        model_id: str,
        display_name: str | None,
        protocol: str,
        capabilities: dict[str, Any],
        source_metadata: dict[str, Any],
        protocol_source: str | None = None,
        first_seen_at: float = 0.0,
        last_seen_at: float = 0.0,
    ) -> None:
        """Load a model from database into cache.

        When the model already exists, merge with the existing entry so
        fields such as ``first_seen_at`` and any per-account resolved
        ``protocol_source`` are not lost on a refresh cycle. New
        non-empty values from the database take precedence over the
        in-memory entry.
        """
        existing = self._models.get(model_id)
        if existing is None:
            self._models[model_id] = {
                "model_id": model_id,
                "display_name": display_name,
                "protocol": protocol,
                "protocol_source": protocol_source,
                "capabilities": capabilities,
                "source_metadata": source_metadata,
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
            }
            return

        # Merge: keep first_seen_at from the existing entry unless the
        # caller provides a non-zero value. Prefer DB-supplied values
        # for fields that the caller populated.
        merged = dict(existing)
        if display_name is not None:
            merged["display_name"] = display_name
        if protocol:
            merged["protocol"] = protocol
        if protocol_source:
            merged["protocol_source"] = protocol_source
        if capabilities:
            merged["capabilities"] = capabilities
        if source_metadata:
            merged["source_metadata"] = source_metadata
        if first_seen_at > 0:
            merged["first_seen_at"] = first_seen_at
        if last_seen_at > 0:
            merged["last_seen_at"] = last_seen_at
        self._models[model_id] = merged

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
