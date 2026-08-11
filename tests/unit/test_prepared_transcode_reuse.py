"""PreparedTranscodeValidityKey and transcode reuse tests.

Prepared transcode stores decoded translated payload as the primary
artifact; encoded bytes are optional cache output valid only for a
specific transform generation; selected-provider thinking normalization
can modify decoded output without parsing bytes; feature/policy changes
or selected-provider overrides invalidate reuse deterministically.
"""

from __future__ import annotations

from eggpool.request.coordinator import ProxyRequestContext
from eggpool.request.provider_bound_request import (
    PreparedTranscodeValidityKey,
    ProviderBoundRequest,
)
from eggpool.transcoder.prepared import PreparedTranscode


class TestPreparedTranscodeValidityKey:
    """Deterministic invalidation via compound key."""

    def test_equal_when_all_fields_match(self) -> None:
        k1 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc123",
            policy_generation=1,
            capability_generation=2,
        )
        k2 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc123",
            policy_generation=1,
            capability_generation=2,
        )
        assert k1 == k2
        assert hash(k1) == hash(k2)

    def test_not_equal_when_fingerprint_differs(self) -> None:
        k1 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
        )
        k2 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="def",
        )
        assert k1 != k2

    def test_not_equal_when_protocol_differs(self) -> None:
        k1 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
        )
        k2 = PreparedTranscodeValidityKey(
            client_protocol="anthropic",
            upstream_protocol="openai",
            features_fingerprint="abc",
        )
        assert k1 != k2

    def test_not_equal_when_policy_generation_differs(self) -> None:
        k1 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
            policy_generation=1,
        )
        k2 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
            policy_generation=2,
        )
        assert k1 != k2

    def test_not_equal_when_capability_generation_differs(self) -> None:
        k1 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
            capability_generation=1,
        )
        k2 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
            capability_generation=2,
        )
        assert k1 != k2

    def test_frozen(self) -> None:
        k = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
        )
        try:
            k.client_protocol = "anthropic"  # type: ignore[misc]
        except AttributeError:
            pass  # expected — frozen dataclass
        else:
            raise AssertionError("PreparedTranscodeValidityKey should be frozen")


class TestProviderBoundRequestTranscodeReuse:
    """ProviderBoundRequest carries transcode validity state."""

    def test_prepared_generation_adopts_without_rematerializing_or_alias_leak(
        self,
    ) -> None:
        messages = [{"role": "user", "content": "large"}]
        prepared_payload = {
            "messages": messages,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        prepared = PreparedTranscode(
            client_protocol="openai",
            upstream_protocol="anthropic",
            translated_payload=prepared_payload,
            translated_body=b"prepared-body",
            warnings=(),
            tool_token_padding=0,
            loss_policy_used="warn",
        )
        pbr = ProviderBoundRequest(
            client_bytes=b"client-body",
            client_payload={"messages": []},
            client_protocol="openai",
            model_id="gpt-4",
        )

        pbr.adopt_provider_payload(
            prepared.translated_payload,
            reason="prepared_transcode",
        )
        pbr.set_provider_bytes(prepared.translated_body)

        assert pbr.provider_payload["messages"] is messages
        assert pbr.provider_bytes is prepared.translated_body
        assert pbr.diagnostics.provider_encodes == 0

        assert pbr.mutate_top_level_mapping(
            "thinking",
            "budget_tokens",
            2048,
            reason="thinking_budget",
        )
        assert prepared_payload["thinking"] == {
            "type": "enabled",
            "budget_tokens": 1024,
        }
        assert pbr.provider_payload["messages"] is messages
        assert pbr.serialize_provider_payload() != prepared.translated_body
        assert pbr.diagnostics.provider_encodes == 1

    def test_release_drops_prepared_dispatch_references(self) -> None:
        prepared = PreparedTranscode(
            client_protocol="openai",
            upstream_protocol="anthropic",
            translated_payload={"messages": [{"role": "user"}]},
            translated_body=b"prepared-body",
            warnings=(),
            tool_token_padding=0,
            loss_policy_used="warn",
        )
        pbr = ProviderBoundRequest(
            client_bytes=b"client-body",
            client_payload={"messages": []},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.adopt_provider_payload(prepared.translated_payload, reason="prepared")
        pbr.set_provider_bytes(prepared.translated_body)
        pbr.serialize_provider_payload()
        context = ProxyRequestContext(
            request_id="request-1",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=b"client-body",
            incoming_headers={},
            prepared_transcode=prepared,
            provider_bound=pbr,
        )

        context.release_dispatch_buffers()

        assert context.prepared_transcode is None
        assert context.original_body == b""
        assert pbr.provider_bytes is None
        assert pbr.provider_payload == {}

    def test_payload_generation_bumps_on_mutation(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4"}',
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        assert pbr.payload_generation == 0
        pbr.set_provider_payload({"model": "claude-3"})
        assert pbr.payload_generation == 1

    def test_provider_bytes_invalidated_on_payload_change(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_bytes(b'{"model":"gpt-4"}')
        assert pbr.provider_bytes == b'{"model":"gpt-4"}'
        pbr.set_provider_payload({"model": "claude"})
        assert pbr.provider_bytes is None

    def test_no_mutation_preserves_alias(self) -> None:
        """When no transform mutates the payload, provider_payload
        aliases client_payload (zero-copy)."""
        payload = {"model": "gpt-4", "messages": []}
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=payload,
            client_protocol="openai",
            model_id="gpt-4",
        )
        assert pbr.provider_payload is pbr.client_payload
        assert pbr.mutated is False
        assert pbr.payload_generation == 0

    def test_multiple_mutations_bump_generation_monotonically(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "a"},
            client_protocol="openai",
            model_id="a",
        )
        for i in range(5):
            pbr.set_provider_payload({"model": f"model-{i}"})
            assert pbr.payload_generation == i + 1

    def test_set_provider_bytes_is_write_once(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_bytes(b"first")
        assert pbr.provider_bytes == b"first"
        pbr.set_provider_bytes(b"second")
        assert pbr.provider_bytes == b"second"
