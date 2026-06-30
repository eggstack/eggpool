"""Integration tests for structured outputs transcoding.

Tests end-to-end encode→decode pipelines for response_format translation
between OpenAI and Anthropic.
"""

from __future__ import annotations

import json

from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"structured_outputs": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


# ---------------------------------------------------------------------------
# OpenAI → Anthropic structured outputs (non-streaming round-trip)
# ---------------------------------------------------------------------------


class TestStructuredOutputsRoundTrip:
    def test_json_object_round_trip(self) -> None:
        """json_object response_format encodes to system prompt, then
        a JSON response decodes back correctly."""
        ctx = TranscodeContext(
            request_id="integ-struct-1",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        request = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Give me a person"}],
            "response_format": {"type": "json_object"},
        }
        upstream, warnings = transcoder.encode_request(
            request, ctx, features=_features()
        )
        assert "response_format" not in upstream
        system = upstream.get("system", "")
        assert "Respond with a valid JSON object" in system
        assert any(
            w.get("kind") == "response_format_to_system_prompt" for w in warnings
        )

        # Decode a JSON response
        response = {
            "id": "msg-abc",
            "content": [{"type": "text", "text": '{"name": "Alice"}'}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 5},
        }
        client_response, _ = transcoder.decode_response(response, ctx)
        assert (
            client_response["choices"][0]["message"]["content"] == '{"name": "Alice"}'
        )

    def test_json_schema_round_trip(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        ctx = TranscodeContext(
            request_id="integ-struct-2",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        request = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Give me data"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "person", "schema": schema, "strict": True},
            },
        }
        upstream, warnings = transcoder.encode_request(
            request, ctx, features=_features()
        )
        system = upstream.get("system", "")
        assert "Respond with a JSON object that matches this schema" in system
        assert json.dumps(schema) in system
        assert "Be precise; do not omit required fields" in system

    def test_disabled_structured_outputs_preserves_v1(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-struct-3",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        request = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Data"}],
            "response_format": {"type": "json_object"},
        }
        _, warnings = transcoder.encode_request(request, ctx, features=None)
        assert any(
            w.get("kind") == "dropped_field" and w.get("field") == "response_format"
            for w in warnings
        )

    def test_invalid_json_response_preserves_raw_text(self) -> None:
        """When upstream returns invalid JSON, raw text is preserved."""
        ctx = TranscodeContext(
            request_id="integ-struct-4",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        response = {
            "id": "msg-abc",
            "content": [{"type": "text", "text": "Sorry, I cannot do that."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        client_response, _ = transcoder.decode_response(response, ctx)
        assert (
            client_response["choices"][0]["message"]["content"]
            == "Sorry, I cannot do that."
        )

    def test_existing_system_preserved_with_response_format(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-struct-5",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        request = {
            "model": "claude-3",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Data"},
            ],
            "response_format": {"type": "json_object"},
        }
        upstream, _ = transcoder.encode_request(request, ctx, features=_features())
        system = upstream.get("system", "")
        assert "You are helpful." in system
        assert "Respond with a valid JSON object" in system
