"""In-memory model catalog cache."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from eggpool.catalog.limits import EffectiveModelLimits, conservative_limits
from eggpool.constants import DEPRECATED_MODEL_ID
from eggpool.routing.provider import parse_model_provider

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig

logger = logging.getLogger(__name__)


def parse_model_id(
    model_id: str, known_providers: set[str] | None = None
) -> tuple[str, str | None]:
    """Parse a model ID that may contain a provider suffix.

    Returns (base_model_id, provider_id) where provider_id is None
    when no suffix is present or the suffix does not match a known
    provider.
    """
    return parse_model_provider(model_id, known_providers)


class ModelCatalogCache:
    """In-memory cache of the model catalog."""

    def __init__(self) -> None:
        # model_id -> model info dict (global, first-seen wins for metadata)
        self._models: dict[str, dict[str, Any]] = {}
        # (model_id, provider_id) -> provider-specific model info
        self._provider_models: dict[tuple[str, str], dict[str, Any]] = {}
        # model_id -> set of account names that support it
        self._account_support: dict[str, set[str]] = {}
        # account_name -> provider_id
        self._account_providers: dict[str, str] = {}
        self._last_refresh: float = 0.0
        # Optional reference to the app config, used to look up
        # provider-level protocol lists for transcoding decisions.
        self._config: AppConfig | None = None
        # Per-account last successful refresh timestamp
        self._account_last_refresh: dict[str, float] = {}
        # Per-account set of (model_id, provider_id) keys the account
        # currently advertises.  Used by ``update_from_account`` to
        # drop stale ``_provider_models`` rows when an upstream removes
        # a model from a single account, so the in-memory cache can
        # converge with the live catalog.
        self._account_provider_keys: dict[str, set[tuple[str, str]]] = {}

    def set_config(self, config: AppConfig) -> None:
        """Set the application config reference for provider lookups."""
        self._config = config

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

        # Drop any per-provider rows this account used to advertise
        # but the new response no longer includes.  A row survives when
        # at least one other account on the same provider still
        # publishes it; otherwise it is removed so the in-memory cache
        # converges with the live catalog.
        new_keys: set[tuple[str, str]] = set()
        for model in models:
            new_keys.add((model["model_id"], provider_id))
        prior_keys = self._account_provider_keys.get(account_name, set())
        if prior_keys - new_keys:
            surviving: set[tuple[str, str]] = set()
            for other_acct, other_keys in self._account_provider_keys.items():
                if other_acct == account_name:
                    continue
                surviving |= other_keys
            for stale_key in prior_keys - new_keys:
                if stale_key not in surviving:
                    self._provider_models.pop(stale_key, None)
        self._account_provider_keys[account_name] = new_keys

        for model in models:
            model_id = model["model_id"]
            provider_key = (model_id, provider_id)

            # Store per-provider metadata (provider-specific protocol,
            # display_name, capabilities, source_metadata).
            model_info: dict[str, Any] = {
                "model_id": model_id,
                "display_name": model.get("display_name"),
                "protocol": model.get("protocol"),
                "protocol_source": model.get("protocol_source"),
                "capabilities": model.get("capabilities", {}),
                "source_metadata": model.get("source_metadata", {}),
                "first_seen_at": now,
                "last_seen_at": now,
                "discovered_limits": model.get("discovered_limits", {}),
                "effective_limits": model.get("effective_limits", {}),
            }
            self._preserve_static_fields(provider_key, model_info)
            self._provider_models[provider_key] = dict(model_info)

            # Global entry: only set on first encounter; never overwrite
            # metadata from an earlier provider for the same model_id.
            if model_id not in self._models:
                self._models[model_id] = model_info
            else:
                global_info = self._models[model_id]
                if not global_info.get("protocol") and model_info.get("protocol"):
                    model_info["first_seen_at"] = global_info.get("first_seen_at", now)
                    self._models[model_id] = model_info
                else:
                    global_info["last_seen_at"] = now

            if model_id not in self._account_support:
                self._account_support[model_id] = set()
            self._account_support[model_id].add(account_name)

        self._last_refresh = now
        self._account_last_refresh[account_name] = now

    @staticmethod
    def _effective_limits_from_info(
        model_info: dict[str, Any] | None,
    ) -> EffectiveModelLimits | None:
        """Read typed effective limits from a model metadata entry."""
        if model_info is None:
            return None
        raw_limits_value = model_info.get("effective_limits")
        if not isinstance(raw_limits_value, dict) or not raw_limits_value:
            return None
        raw_limits = cast("dict[str, Any]", raw_limits_value)
        return EffectiveModelLimits(
            context_tokens=raw_limits.get("context_tokens"),
            input_tokens=raw_limits.get("input_tokens"),
            output_tokens=raw_limits.get("output_tokens"),
            enforce=raw_limits.get("enforce", True),
            context_source=raw_limits.get("context_source"),
            input_source=raw_limits.get("input_source"),
            output_source=raw_limits.get("output_source"),
        )

    def _preserve_static_fields(
        self,
        provider_key: tuple[str, str],
        model_info: dict[str, Any],
    ) -> None:
        """Preserve static_config-sourced fields against a live merge.

        When a live row arrives for a model that already has a
        ``static_config``-sourced provider entry, explicit static
        protocol and capability fields win over a live row that did not
        bring its own resolution. The merge mutates *model_info* in
        place so callers can write the merged value into the cache.
        """
        existing = self._provider_models.get(provider_key)
        if existing is None:
            return
        if existing.get("protocol_source") != "static_config":
            return
        new_protocol_source = model_info.get("protocol_source")
        if new_protocol_source in {"config", "static_config"}:
            return
        model_info["protocol"] = existing.get("protocol")
        model_info["protocol_source"] = existing.get("protocol_source")
        existing_caps_raw = existing.get("capabilities", {})
        new_caps_raw = model_info.get("capabilities", {})
        if not isinstance(existing_caps_raw, dict) or not isinstance(
            new_caps_raw, dict
        ):
            return
        existing_caps = cast("dict[str, Any]", existing_caps_raw)
        new_caps = cast("dict[str, Any]", new_caps_raw)
        if (
            existing_caps.get("supports_tools") is not None
            and new_caps.get("supports_tools") is None
        ):
            new_caps["supports_tools"] = existing_caps["supports_tools"]
        if (
            existing_caps.get("supports_vision") is not None
            and new_caps.get("supports_vision") is None
        ):
            new_caps["supports_vision"] = existing_caps["supports_vision"]
        model_info["capabilities"] = new_caps

    def _visible_provider_ids(
        self,
        visible_accounts: set[str],
    ) -> list[str]:
        """Return deterministic provider IDs for the visible accounts."""
        provider_ids = {
            provider_id
            for account_name in visible_accounts
            if (provider_id := self._account_providers.get(account_name)) is not None
        }
        return sorted(provider_ids)

    @staticmethod
    def _is_visible(
        expose_mode: str,
        visible_accounts: set[str],
        eligible_accounts: set[str],
    ) -> bool:
        """Return whether account support satisfies the exposure policy."""
        if expose_mode == "intersection":
            return bool(eligible_accounts) and visible_accounts == eligible_accounts
        return expose_mode in {"union", "healthy_union"} and bool(visible_accounts)

    @staticmethod
    def _copy_exposed_model(
        model_info: dict[str, Any],
        *,
        model_id: str,
        available_accounts: set[str],
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        """Copy a cache entry into the serialized exposure format."""
        model_copy = dict(model_info)
        model_copy["model_id"] = (
            f"{model_id}/{provider_id}" if provider_id is not None else model_id
        )
        if provider_id is not None:
            model_copy["base_model_id"] = model_id
            model_copy["provider_id"] = provider_id
        model_copy["available_accounts"] = sorted(available_accounts)
        return model_copy

    def _select_unsuffixed_model_info(
        self,
        model_id: str,
        provider_ids: list[str],
    ) -> dict[str, Any] | None:
        """Pick the metadata entry to expose for an unsuffixed model ID."""
        found_provider_entry = False
        for provider_id in provider_ids:
            provider_info = self._provider_models.get((model_id, provider_id))
            if provider_info is None:
                continue
            found_provider_entry = True
            if provider_info.get("protocol"):
                return provider_info

        if not found_provider_entry:
            global_info = self._models.get(model_id)
            if global_info is not None and global_info.get("protocol"):
                return global_info
        return None

    def _select_provider_model_info(
        self,
        model_id: str,
        provider_id: str,
    ) -> dict[str, Any] | None:
        """Pick the metadata entry to expose for a provider-suffixed model."""
        provider_info = self._provider_models.get((model_id, provider_id))
        if provider_info is not None:
            if not provider_info.get("protocol"):
                return None
            return provider_info

        global_info = self._models.get(model_id)
        if global_info is not None and global_info.get("protocol"):
            return global_info
        return None

    def _merged_effective_limits(
        self,
        model_id: str,
        provider_ids: list[str],
    ) -> dict[str, Any] | None:
        """Merge per-provider effective limits across visible providers."""
        limits = self._merged_effective_limits_value(model_id, provider_ids)
        return None if limits is None else limits.as_dict()

    def _merged_effective_limits_value(
        self,
        model_id: str,
        provider_ids: list[str],
    ) -> EffectiveModelLimits | None:
        """Return typed conservative limits across visible providers."""
        all_limits: list[EffectiveModelLimits] = []
        for provider_id in provider_ids:
            provider_info = self._provider_models.get((model_id, provider_id))
            limits = self._effective_limits_from_info(provider_info)
            if limits is not None:
                all_limits.append(limits)

        if not all_limits:
            return None
        return conservative_limits(all_limits)

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
        cannot be routed to any endpoint.  When multiple providers
        advertise the same model, uses per-provider metadata.  The
        ``__deprecated__`` placeholder is also excluded; it only
        exists in the durable layer for relinking historical usage.
        """
        result: list[dict[str, Any]] = []

        for model_id, _model_info in self._models.items():
            if model_id == DEPRECATED_MODEL_ID:
                continue
            accounts_supporting = self._account_support.get(model_id, set())
            visible_accounts = accounts_supporting & eligible_account_names

            if not self._is_visible(
                expose_mode,
                visible_accounts,
                eligible_account_names,
            ):
                continue

            provider_ids = self._visible_provider_ids(visible_accounts)
            best_info = self._select_unsuffixed_model_info(model_id, provider_ids)
            if best_info is None:
                continue

            model_info_copy = self._copy_exposed_model(
                best_info,
                model_id=model_id,
                available_accounts=visible_accounts,
            )
            # Track contributing providers so /v1/models can surface
            # routing priorities and provider list for collapsed entries.
            model_info_copy["providers"] = list(provider_ids)

            merged_limits = self._merged_effective_limits(model_id, provider_ids)
            if merged_limits is not None:
                model_info_copy["effective_limits"] = merged_limits

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
        model ID like 'model-id/provider-id'.  Uses per-provider metadata
        so models shared across providers retain independent protocol and
        capability information.
        """
        # Build provider -> eligible accounts mapping
        provider_accounts: dict[str, set[str]] = {}
        for account_name in eligible_account_names:
            pid = self._account_providers.get(account_name)
            if pid:
                provider_accounts.setdefault(pid, set()).add(account_name)

        result: list[dict[str, Any]] = []
        for model_id in self._models:
            if model_id == DEPRECATED_MODEL_ID:
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

                if not self._is_visible(
                    expose_mode,
                    visible,
                    eligible_in_provider,
                ):
                    continue

                # Use per-provider metadata for this (model_id, provider_id)
                # when available. If the provider-specific entry is
                # unresolved, keep it hidden rather than borrowing another
                # provider's protocol from the global cache entry.
                model_info = self._select_provider_model_info(model_id, pid)
                if model_info is None:
                    continue

                result.append(
                    self._copy_exposed_model(
                        model_info,
                        model_id=model_id,
                        available_accounts=visible,
                        provider_id=pid,
                    )
                )

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

    def get_model_for_provider(
        self, model_id: str, provider_id: str | None
    ) -> dict[str, Any] | None:
        """Get model info for a specific provider.

        Returns per-provider metadata when available, falling back to
        the global entry.
        """
        if provider_id is not None:
            pinfo = self._provider_models.get((model_id, provider_id))
            if pinfo is not None:
                return pinfo
        return self._models.get(model_id)

    def get_effective_limits(
        self,
        model_id: str,
        provider_id: str | None,
    ) -> EffectiveModelLimits | None:
        """Return request limits for a model and optional provider.

        Provider-suffixed requests use that provider's limits. Unsuffixed
        requests use the conservative merge across every provider that
        currently supports the model, matching the limits exposed by the
        model catalog.
        """
        if provider_id is not None:
            return self._effective_limits_from_info(
                self._provider_models.get((model_id, provider_id))
            )

        supporting_accounts = self._account_support.get(model_id, set())
        provider_ids = self._visible_provider_ids(supporting_accounts)
        merged = self._merged_effective_limits_value(model_id, provider_ids)
        if merged is not None:
            return merged

        return self._effective_limits_from_info(self._models.get(model_id))

    def get_provider_model_entry(
        self,
        model_id: str,
        provider_id: str,
    ) -> dict[str, Any] | None:
        """Return exact provider metadata without a global fallback."""
        return self._provider_models.get((model_id, provider_id))

    def get_model_for_account(
        self, model_id: str, account_name: str
    ) -> dict[str, Any] | None:
        """Get model info using the account's provider-specific metadata."""
        return self.get_model_for_provider(
            model_id,
            self._account_providers.get(account_name),
        )

    def is_account_model_available(
        self,
        account_name: str,
        model_id: str,
        *,
        max_age_s: float | None = None,
        protocol: str | None = None,
    ) -> bool:
        """Return whether an account can route a model.

        Availability requires account support and a resolved protocol in
        that account's provider-specific model metadata.  When
        ``protocol`` is supplied, the resolved protocol must match the
        requested endpoint protocol.
        """
        supporting = (
            self.get_fresh_supporting_accounts(model_id, max_age_s)
            if max_age_s is not None
            else self.get_supporting_accounts(model_id)
        )
        if account_name not in supporting:
            return False

        model_info = self.get_model_for_account(model_id, account_name)
        resolved_protocol = model_info.get("protocol") if model_info else None
        if not resolved_protocol:
            return False
        return protocol is None or resolved_protocol == protocol

    def get_model_protocols(
        self,
        model_id: str,
        *,
        account_names: set[str] | None = None,
        provider_id: str | None = None,
    ) -> set[str]:
        """Get resolved protocols available for a model across accounts."""
        supporting = self.get_supporting_accounts(model_id)
        if account_names is not None:
            supporting &= account_names

        protocols: set[str] = set()
        for account_name in supporting:
            account_provider = self._account_providers.get(account_name)
            if provider_id is not None and account_provider != provider_id:
                continue
            model_info = self.get_model_for_provider(model_id, account_provider)
            resolved_protocol = model_info.get("protocol") if model_info else None
            if resolved_protocol:
                protocols.add(str(resolved_protocol))
        return protocols

    def get_supporting_accounts(self, model_id: str) -> set[str]:
        """Get set of account names that support a model."""
        return self._account_support.get(model_id, set()).copy()

    def get_transcodable_protocols(
        self,
        model_id: str,
        *,
        client_protocol: str,
    ) -> set[str]:
        """Return the set of protocols a model can reach, minus the client protocol.

        Uses the union of all account provider.protocols to determine
        which protocols can reach this model.
        """
        supporting = self.get_supporting_accounts(model_id)
        protocols: set[str] = set()
        for account_name in supporting:
            provider_id = self._account_providers.get(account_name)
            if provider_id is None:
                continue
            if self._config is not None:
                provider = self._config.providers.get(provider_id)
                if provider is not None:
                    for proto in provider.protocols:
                        protocols.add(proto)
        protocols.discard(client_protocol)
        return protocols

    def is_model_available(self, model_id: str, eligible_accounts: set[str]) -> bool:
        """Check if a model is available from any eligible account."""
        if model_id not in self._models:
            return False
        supporting = self._account_support.get(model_id, set())
        visible = supporting & eligible_accounts
        return any(
            self.is_account_model_available(account_name, model_id)
            for account_name in visible
        )

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

        Prefer provider-specific metadata.  The global model entry may have
        been refreshed by a different provider offering the same model ID,
        which must not make this account's catalog data appear newer.
        """
        for model_id, accounts in self._account_support.items():
            for account_name in accounts:
                provider_id = self._account_providers.get(account_name)
                model_info = (
                    self._provider_models.get((model_id, provider_id))
                    if provider_id is not None
                    else None
                )
                if model_info is None:
                    model_info = self._models.get(model_id)
                if model_info is None:
                    continue
                last_seen = model_info.get("last_seen_at", 0.0)
                if not isinstance(last_seen, (int, float)) or last_seen <= 0:
                    continue
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

    def get_provider_model_entries(
        self,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Get all per-provider model entries.

        Returns a dict keyed by ``(model_id, provider_id)`` with
        provider-specific model info dicts as values.
        """
        return dict(self._provider_models)

    def set_provider_model_entry(
        self,
        model_id: str,
        provider_id: str,
        model_info: dict[str, Any],
    ) -> None:
        """Set a per-provider model entry."""
        self._provider_models[(model_id, provider_id)] = model_info

    def prune_unused(self) -> int:
        """Drop cache entries no longer referenced by any account or provider.

        A model is removed from ``_models`` only when both:

        * its ``_account_support`` set is empty (no live account can route
          the model), and
        * no ``_provider_models`` row exists for any provider.

        Returns the number of models removed. The returned count is
        intended for log diagnostics; tests assert on it to prove that
        withdraw-from-every-account actually clears the in-memory state.
        """
        referenced: set[str] = set()
        for model_id in self._account_support:
            if self._account_support[model_id]:
                referenced.add(model_id)
        for model_id, _provider_id in self._provider_models:
            referenced.add(model_id)

        stale = [model_id for model_id in self._models if model_id not in referenced]
        for model_id in stale:
            del self._models[model_id]
            self._account_support.pop(model_id, None)
        return len(stale)
