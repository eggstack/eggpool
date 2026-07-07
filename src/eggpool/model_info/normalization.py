"""Deterministic normalization primitives for model-info identity matching.

All functions are pure, side-effect-free, and stdlib-only. They operate on
raw strings and return new strings — inputs are never mutated, preserving
the original for display and diagnostics.

Design goals:
- Deterministic: same input always produces the same output.
- Conservative: collapsing only happens when a vendor token exactly repeats
  the next token after casefolding.
- Auditable: every normalization step is explicit and traceable.
"""

from __future__ import annotations

import re
import unicodedata

# Separator pattern: space, hyphen, underscore, colon, dot, slash.
_SEP_RE = re.compile(r"[-_:. /]+")

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def normalize_model_key(value: str) -> str:
    """Deterministic comparison key for any model identifier.

    Rules (in order):
    1. Unicode normalize with NFKC
    2. .casefold() (not .lower())
    3. Strip leading/trailing whitespace
    4. Strip leading ``<vendor>:`` / ``<vendor> -`` decoration when the
       vendor token exactly matches the next token (handles
       ``MiniMax: MiniMax M3`` -> ``MiniMax M3`` -> ``minimaxm3``)
    5. Replace separators (- _ : . / space) with nothing
    6. Remove all non-alphanumeric characters
    7. Collapse runs of whitespace between tokens before stripping
       (so the duplicate-token collapse in step 4 still works)
    """
    # Step 1: Unicode NFKC normalization
    value = unicodedata.normalize("NFKC", value)
    # Step 2: casefold
    value = value.casefold()
    # Step 3: strip leading/trailing whitespace
    value = value.strip()
    if not value:
        return ""
    # Step 7 (before step 4): collapse runs of whitespace so token
    # comparison works correctly.
    value = re.sub(r"\s+", " ", value)
    # Step 4: collapse duplicate vendor prefix (must happen before
    # separator removal so we can split on the first separator).
    value = collapse_duplicate_vendor(value)
    # Step 5: replace separators with nothing
    value = _SEP_RE.sub("", value)
    # Step 6: remove all non-alphanumeric characters
    value = _NON_ALNUM_RE.sub("", value)
    return value


def split_source_id(value: str) -> tuple[str | None, str]:
    """Split slash-delimited source IDs into (namespace_vendor_or_None, model_segment).

    Examples:
        "minimax/minimax-m3"        -> ("minimax", "minimax-m3")
        "anthropic/claude-sonnet-4.5" -> ("anthropic", "claude-sonnet-4.5")
        "minimax-m3"                -> (None, "minimax-m3")
        "opencode-go/minimax-m3"    -> ("opencode-go", "minimax-m3")
    """
    parts = value.split("/", 1)
    if len(parts) == 2:
        return (parts[0], parts[1])
    return (None, value)


def normalize_vendor_key(value: str | None) -> str | None:
    """Return a deterministic vendor comparison key.

    Uses normalize_model_key; returns None when input is None or empty.
    """
    if value is None:
        return None
    normalized = normalize_model_key(value)
    return normalized if normalized else None


def tokenize_model_key(value: str) -> tuple[str, ...]:
    """Tokenize a model identifier into a tuple of lowercase tokens.

    Splits on the same separators as normalize_model_key (space, hyphen,
    underscore, colon, dot, slash). Filters empty tokens. Preserves
    ordering.

    Example: "claude-sonnet-4.5" -> ("claude", "sonnet", "4", "5")
             "MiniMax-M3" -> ("minimax", "m3") after casefold
    """
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    raw = _SEP_RE.split(value)
    return tuple(t for t in raw if t)


def strip_provider_namespace(value: str, known_providers: set[str]) -> str:
    """If value is slash-delimited AND the prefix is in known_providers,
    return the model segment after the slash; otherwise return value.

    This is for cases like "opencode-go/minimax-m3" -> "minimax-m3"
    (when opencode-go is a known provider). The provider namespace is
    NOT a vendor namespace for aggregator providers.
    """
    parts = value.split("/", 1)
    if len(parts) == 2 and parts[0] in known_providers:
        return parts[1]
    return value


def collapse_duplicate_vendor(value: str) -> str:
    """If value starts with a leading vendor token that exactly repeats
    the next token (case-insensitively after stripping the trailing
    separator and whitespace), drop the leading repeat.

    Example: "MiniMax: MiniMax M3" -> "MiniMax M3"
             "MiniMax - MiniMax M3" -> "MiniMax M3"
             "minimax: minimax m3" -> "minimax m3"
             "minimax-m3" -> "minimax-m3" (unchanged)
    """
    # Split on the first separator to isolate the leading token.
    # Only trigger collapse for separators used in display-name decoration
    # (colon, dash, space, dot, underscore) — NOT slash, which indicates
    # a source namespace like "minimax/minimax-m3".
    match = re.match(r"^([^\s:_.\-/]+)\s*([:_.\- ])\s*", value)
    if not match:
        return value
    prefix = match.group(1)
    rest = value[match.end() :]
    rest_lower = rest.casefold()
    prefix_lower = prefix.casefold()
    if rest_lower.startswith(prefix_lower):
        return rest
    return value
