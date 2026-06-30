"""Tests for Phase 6.4 — Structured outputs transcoding."""

from __future__ import annotations

import json

from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures


def _make_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="test-structured",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"structured_outputs": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


class TestStructuredOutputsOpenAIToAnthropic:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()

    def test_json_object_appends_system_instruction(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Give me data"}],
            "response_format": {"type": "json_object"},
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        system = result.get("system", "")
        assert "Respond with a valid JSON object" in system
        assert any(
            w.get("kind") == "response_format_to_system_prompt" for w in warnings
        )

    def test_json_schema_appends_schema_instruction(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Give me data"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "person",
                    "schema": schema,
                    "strict": True,
                },
            },
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        system = result.get("system", "")
        assert "Respond with a JSON object that matches this schema" in system
        assert json.dumps(schema) in system
        assert "Be precise; do not omit required fields" in system
        assert any(
            w.get("kind") == "response_format_to_system_prompt" for w in warnings
        )

    def test_json_schema_non_strict(self) -> None:
        schema = {"type": "object"}
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Data"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "x", "schema": schema, "strict": False},
            },
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        system = result.get("system", "")
        assert "Be precise" not in system

    def test_disabled_drops_response_format(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Data"}],
            "response_format": {"type": "json_object"},
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=None
        )
        assert any(
            w.get("kind") == "dropped_field" and w.get("field") == "response_format"
            for w in warnings
        )

    def test_response_format_not_in_output(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Data"}],
            "response_format": {"type": "json_object"},
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert "response_format" not in result

    def test_existing_system_preserved(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Data"},
            ],
            "response_format": {"type": "json_object"},
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        system = result.get("system", "")
        assert "You are helpful." in system
        assert "Respond with a valid JSON object" in system
