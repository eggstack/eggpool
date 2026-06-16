"""Secret and credential redaction helpers."""

from __future__ import annotations

import json
import re
from typing import Any, cast

REDACTED = "[REDACTED]"

# Authorization: Bearer <token>  /  Authorization: <scheme> <value>
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:[^\s,;\"'}]+(?:\s+[^\s,;\"'}]+)*)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-+/=]+)")

# OpenAI/Anthropic style API keys beginning with sk- (with optional
# suffix or separator).
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")

# password=..., secret=..., api_key=... assignments
_PASSWORD_RE = re.compile(r"(?i)(password\s*=\s*)([^\s,;\"'}]+)")
_SECRET_RE = re.compile(r"(?i)(\bsecret\s*=\s*)([^\s,;\"'}]+)")
_API_KEY_RE = re.compile(r"(?i)(api[_-]?key\s*=\s*)([^\s,;\"'}]+)")

# JSON "prompt": "..." and "completion": "..." fields
_PROMPT_FIELD_RE = re.compile(r'(?i)("prompt"\s*:\s*)"([^"\\]*(?:\\.[^"\\]*)*)"')
_COMPLETION_FIELD_RE = re.compile(
    r'(?i)("completion"\s*:\s*)"([^"\\]*(?:\\.[^"\\]*)*)"'
)

# https://user:pass@host/...
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)([^\s/:@\"'<>]+):([^\s/@\"'<>]+)@"
)

# Sensitive query parameters
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:key|token|api_key|access_token)=)([^&\s\"'<>]+)"
)

# JSON keys whose values must be redacted (case-insensitive comparison)
SENSITIVE_JSON_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "api-key",
        "password",
        "secret",
        "token",
        "access_token",
        "accesstoken",
        "access-token",
        "refresh_token",
        "refreshtoken",
        "refresh-token",
        "client_secret",
        "private_key",
    }
)

# User-content-bearing keys whose entire value must be replaced
USER_CONTENT_JSON_KEYS: frozenset[str] = frozenset(
    {
        "prompt",
        "completion",
        "input",
        "messages",
        "user_input",
    }
)

# JSON keys retained verbatim during structured sanitization
SAFE_JSON_KEYS: frozenset[str] = frozenset(
    {
        "type",
        "code",
        "status",
        "status_code",
        "error_type",
        "kind",
        "param",
        "message",
        "request_id",
        "trace_id",
    }
)

# Bounds for structured JSON sanitization. Larger inputs collapse to
# an empty list so no arbitrary provider detail can leak.
MAX_SANITIZE_DEPTH = 6
MAX_SANITIZE_ITEMS = 64
MAX_SANITIZE_BYTES = 8192
MAX_STRING_BYTES = 1024
MAX_KEY_BYTES = 64


def _truncate_string(value: str, limit: int = MAX_STRING_BYTES) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _truncate_key(key: Any) -> str:
    text = str(key)
    if len(text) <= MAX_KEY_BYTES:
        return text
    return text[:MAX_KEY_BYTES] + "..."


def _is_sensitive_key(key: Any) -> bool:
    return _truncate_key(key).lower() in SENSITIVE_JSON_KEYS


def _is_user_content_key(key: Any) -> bool:
    return _truncate_key(key).lower() in USER_CONTENT_JSON_KEYS


def sanitize_error_object(
    value: Any,
    *,
    depth: int = 0,
    item_budget: int = MAX_SANITIZE_ITEMS,
    byte_budget: int = MAX_SANITIZE_BYTES,
) -> Any:
    """Recursively sanitize a JSON-like value for safe persistence.

    Rules:
    - Sensitive keys (case-insensitive) have their values replaced
      with :data:`REDACTED`.
    - User-content keys (prompt, completion, input, messages, ...)
      have their entire value replaced with :data:`REDACTED`.
    - Safe keys (``type``, ``code``, bounded ``message``) are
      retained after string-level redaction.
    - Depth, item count, and serialized byte size are bounded so
      arbitrary provider detail cannot leak.
    - Non-string scalar values are stringified and redaction-applied.
    """
    if depth >= MAX_SANITIZE_DEPTH:
        return REDACTED
    if item_budget <= 0:
        return REDACTED
    if byte_budget <= 0:
        return REDACTED

    if isinstance(value, dict):
        if item_budget <= 0:
            return REDACTED
        result: dict[str, Any] = {}
        items_view = cast("dict[Any, Any]", value).items()
        for entry in items_view:
            key: Any = entry[0]
            item: Any = entry[1]
            if item_budget <= 0:
                break
            item_budget -= 1
            safe_key = _truncate_key(key)
            if _is_sensitive_key(key) or _is_user_content_key(key):
                result[safe_key] = REDACTED
                continue
            if isinstance(item, str):
                redacted_string = redact_error_detail(item)
                if redacted_string is not None:
                    result[safe_key] = _truncate_string(redacted_string)
                else:
                    result[safe_key] = None
                continue
            result[safe_key] = sanitize_error_object(
                item,
                depth=depth + 1,
                item_budget=item_budget,
                byte_budget=byte_budget,
            )
        return result

    if isinstance(value, list):
        if item_budget <= 0:
            return REDACTED
        result_list: list[Any] = []
        for entry in cast("list[Any]", value):
            item: Any = entry
            if item_budget <= 0:
                break
            item_budget -= 1
            result_list.append(
                sanitize_error_object(
                    item,
                    depth=depth + 1,
                    item_budget=item_budget,
                    byte_budget=byte_budget,
                )
            )
        return result_list

    if isinstance(value, str):
        redacted_string = redact_error_detail(value)
        if redacted_string is None:
            return None
        return _truncate_string(redacted_string)

    if value is None or isinstance(value, (bool, int, float)):
        return value

    text = str(value)
    redacted_text = redact_error_detail(text)
    if redacted_text is None:
        return None
    return _truncate_string(redacted_text)


def _try_parse_json(value: str) -> Any | None:
    """Attempt to parse ``value`` as JSON. Returns None on failure."""
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def redact_error_detail(value: str | None) -> str | None:
    """Replace secret-bearing fragments in an error detail string.

    The returned value is safe to persist to a database or write to
    logs. Matches are replaced with the ``[REDACTED]`` marker. ``None``
    and empty input are returned unchanged.

    When the input looks like a JSON object or array, the redactor
    first tries to parse it and apply :func:`sanitize_error_object`
    recursively, then re-serializes the sanitized result. Regex
    fallbacks are applied to non-JSON text and to scalar strings.
    """
    if value is None or value == "":
        return value

    parsed = _try_parse_json(value)
    if parsed is not None:
        sanitized = sanitize_error_object(parsed)
        try:
            return json.dumps(sanitized, ensure_ascii=False)
        except (TypeError, ValueError):
            # Fall through to regex-based redaction.
            pass

    redacted = value
    redacted = _AUTH_HEADER_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _BEARER_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _SK_KEY_RE.sub(REDACTED, redacted)
    redacted = _PASSWORD_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _SECRET_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _API_KEY_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _PROMPT_FIELD_RE.sub(r'\1"' + REDACTED + '"', redacted)
    redacted = _COMPLETION_FIELD_RE.sub(r'\1"' + REDACTED + '"', redacted)
    redacted = _URL_USERINFO_RE.sub(r"\g<scheme>" + REDACTED + "@", redacted)
    redacted = _SENSITIVE_QUERY_RE.sub(r"\1" + REDACTED, redacted)
    return redacted
