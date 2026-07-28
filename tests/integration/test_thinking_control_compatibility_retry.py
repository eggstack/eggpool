"""Thinking-control compatibility retry deferral tests.

The ``allow_compatibility_retry`` configuration field exists but defaults
to ``False``.  No retry logic is wired into the adaptation pipeline.

This file verifies the deferral contract:

- The configuration field defaults to disabled.
- Strict policy disables retry regardless of config.
- When the config is disabled, no retry signature is set on adaptation
  results.
"""

from __future__ import annotations

import pytest

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
    ThinkingRequestIntent,
)
from eggpool.errors import CapabilityError
from eggpool.transcoder.provider_adaptation import (
    ProviderControlPolicy,
    adapt_thinking_controls,
)

pytestmark = [pytest.mark.integration, pytest.mark.request_path]


def _capability_fixed() -> ThinkingCapability:
    return ThinkingCapability(
        status="supported",
        control_contract=ThinkingControlContract(
            mode="fixed",
            source="manual_override",
        ),
    )


def _intent_effort() -> ThinkingRequestIntent:
    return ThinkingRequestIntent(
        requested_effort="high",
        request_fields=("reasoning_effort",),
        has_historical_reasoning_content=False,
        client_requests_new_reasoning=True,
        client_protocol="openai",
    )


class TestCompatibilityRetryDeferred:
    """Verify that compatibility retry is disabled by default."""

    def test_default_policy_disables_retry(self) -> None:
        """ProviderControlPolicy defaults to allow_compatibility_retry=False."""
        policy = ProviderControlPolicy()
        assert policy.allow_compatibility_retry is False

    def test_no_retry_signature_when_disabled(self) -> None:
        """Adaptation results have no retry_signature when retry is disabled."""
        cap = _capability_fixed()
        intent = _intent_effort()

        # warn_drop policy: effort is dropped, not rejected.
        result = adapt_thinking_controls(
            payload={"model": "test", "reasoning_effort": "high"},
            client_protocol="openai",
            model_id="test-model",
            provider_id="test-provider",
            capability=cap,
            intent=intent,
            policy=ProviderControlPolicy(
                unsupported_control="warn_drop",
                allow_compatibility_retry=False,
            ),
        )
        assert result.retry_signature is None

    def test_strict_policy_overrides_retry_config(self) -> None:
        """Even if retry were enabled, strict policy rejects locally."""
        cap = _capability_fixed()
        intent = _intent_effort()

        # Even with allow_compatibility_retry=True, reject policy raises.
        with pytest.raises(CapabilityError):
            adapt_thinking_controls(
                payload={"model": "test", "reasoning_effort": "high"},
                client_protocol="openai",
                model_id="test-model",
                provider_id="test-provider",
                capability=cap,
                intent=intent,
                policy=ProviderControlPolicy(
                    unsupported_control="reject",
                    allow_compatibility_retry=True,
                ),
            )
