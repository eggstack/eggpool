"""Account registry: loads accounts from config, manages runtime state."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from go_aggregator.accounts.state import AccountRuntimeState
from go_aggregator.errors import ConfigError

if TYPE_CHECKING:
    from go_aggregator.models.config import AppConfig

logger = logging.getLogger(__name__)


class AccountRegistry:
    """Manages account configurations and their runtime states."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._states: dict[str, AccountRuntimeState] = {}
        self._api_keys: dict[str, str] = {}
        self._initialize()

    def _initialize(self) -> None:
        """Load accounts from config and resolve API keys."""
        for acct_config in self._config.accounts:
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

            if acct_config.enabled:
                logger.info(
                    "Loaded account %r (weight=%.2f)",
                    acct_config.name,
                    acct_config.weight,
                )

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
        for acct in self._config.accounts:
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
