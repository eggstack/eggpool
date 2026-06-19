"""Account registry: loads accounts from config, manages runtime state."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from eggpool.accounts.state import AccountRuntimeState
from eggpool.errors import ConfigError

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig

logger = logging.getLogger(__name__)


def account_config_rows(config: AppConfig) -> list[dict[str, Any]]:
    """Serialize configured accounts into rows for persistence.

    Returns a list of dicts with the exact fields consumed by
    :meth:`eggpool.db.repositories.AccountRepository.sync_from_config`.
    Keeping the shape in one place prevents the app lifespan and the
    ``models refresh`` CLI command from drifting out of sync.
    """
    rows: list[dict[str, Any]] = []
    for provider_id, provider in config.providers.items():
        for acct in provider.accounts:
            rows.append(
                {
                    "name": acct.name,
                    "api_key_env": acct.api_key_env,
                    "enabled": acct.enabled,
                    "weight": acct.weight,
                    "provider_id": provider_id,
                }
            )
    return rows


class AccountRegistry:
    """Manages account configurations and their runtime states."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._states: dict[str, AccountRuntimeState] = {}
        self._api_keys: dict[str, str] = {}
        self._account_providers: dict[str, str] = {}
        self._initialize()

    def _initialize(self) -> None:
        """Load accounts from config and resolve API keys."""
        for provider_id, provider_cfg in self._config.providers.items():
            for acct_config in provider_cfg.accounts:
                api_key = os.environ.get(acct_config.api_key_env, "")
                if acct_config.enabled and not api_key:
                    raise ConfigError(
                        f"Account {acct_config.name!r} is enabled but "
                        f"env var {acct_config.api_key_env!r} is not set"
                    )

                state = AccountRuntimeState(
                    name=acct_config.name,
                    enabled=acct_config.enabled,
                    weight=acct_config.weight,
                )
                self._states[acct_config.name] = state
                self._api_keys[acct_config.name] = api_key
                self._account_providers[acct_config.name] = provider_id

                if acct_config.enabled:
                    logger.info(
                        "Loaded account %r (weight=%.2f, provider=%r)",
                        acct_config.name,
                        acct_config.weight,
                        provider_id,
                    )

    def reload(self, config: AppConfig) -> None:
        """Reload account configurations from a new config."""
        self._config = config
        self._states.clear()
        self._api_keys.clear()
        self._account_providers.clear()
        self._initialize()

    def get_state(self, name: str) -> AccountRuntimeState | None:
        """Get runtime state for an account by name."""
        return self._states.get(name)

    def get_api_key(self, name: str) -> str | None:
        """Get the resolved API key for an account."""
        return self._api_keys.get(name)

    def get_all_states(self) -> list[AccountRuntimeState]:
        """Get all account runtime states."""
        return list(self._states.values())

    def get_enabled_states(self) -> list[AccountRuntimeState]:
        """Get runtime states for enabled accounts."""
        return [s for s in self._states.values() if s.enabled]

    def get_eligible_states(self) -> list[AccountRuntimeState]:
        """Get runtime states for eligible accounts."""
        return [s for s in self._states.values() if s.is_eligible()]

    def get_account_config(self, name: str):
        """Get the config for an account by name."""
        for acct in self._config.all_accounts():
            if acct.name == name:
                return acct
        return None

    def get_account_offsets(self, name: str) -> dict[str, int]:
        """Get quota offsets for an account."""
        acct = self.get_account_config(name)
        if acct is None:
            return {}
        return {
            "five_hour": acct.five_hour_offset_microdollars,
            "weekly": acct.weekly_offset_microdollars,
            "monthly": acct.monthly_offset_microdollars,
        }

    def get_provider_for_account(self, account_name: str) -> str | None:
        """Get the provider ID for an account."""
        return self._account_providers.get(account_name)

    def get_provider_protocols(self, provider_id: str) -> set[str]:
        """Get protocols configured for a provider."""
        provider = self._config.providers.get(provider_id)
        if provider is None:
            return set()
        return set(provider.protocols)

    def account_supports_protocol(self, account_name: str, protocol: str) -> bool:
        """Return whether an account's configured provider supports a protocol."""
        provider_id = self.get_provider_for_account(account_name)
        if provider_id is None:
            return False
        return protocol in self.get_provider_protocols(provider_id)

    def get_accounts_for_provider(self, provider_id: str) -> list[AccountRuntimeState]:
        """Get all account states belonging to a provider."""
        return [
            state
            for name, state in self._states.items()
            if self._account_providers.get(name) == provider_id
        ]

    def get_enabled_accounts_for_provider(
        self, provider_id: str
    ) -> list[AccountRuntimeState]:
        """Get enabled account states belonging to a provider."""
        return [
            state
            for name, state in self._states.items()
            if self._account_providers.get(name) == provider_id and state.enabled
        ]

    def get_provider_ids(self) -> list[str]:
        """Get all unique provider IDs."""
        return list(set(self._account_providers.values()))
