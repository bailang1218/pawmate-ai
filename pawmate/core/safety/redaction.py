"""Redact credentials before tool results leave PawMate internals."""
from __future__ import annotations

import json
import re
from typing import Any


REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "access_token",
    "access-token",
    "refresh_token",
    "refresh-token",
    "token",
    "secret",
    "client_secret",
    "client-secret",
    "password",
    "passwd",
    "pwd",
    "authorization",
    "private_key",
    "private-key",
    "group_id",
    "group-id",
}

_SENSITIVE_KEY_PATTERN = "|".join(re.escape(k) for k in sorted(_SENSITIVE_KEYS, key=len, reverse=True))

_JSON_STRING_SECRET_RE = re.compile(
    rf'(?i)(["\'](?:{_SENSITIVE_KEY_PATTERN})["\']\s*:\s*)(["\'])(?:\\.|(?!\2).)*(\2)'
)
_ENV_SECRET_RE = re.compile(
    rf"(?i)\b({_SENSITIVE_KEY_PATTERN})\b\s*=\s*([^\s,;]+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SK_TOKEN_RE = re.compile(r"\b(?:sk|rk|pk|ak)-[A-Za-z0-9][A-Za-z0-9._-]{6,}\b")


def is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace(" ", "_")
    return normalized in _SENSITIVE_KEYS


def redact_sensitive_data(value: Any) -> Any:
    """Return a copy with credential-like values redacted."""
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) and _has_value(item)
            else redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Redact secrets in plain text or JSON text."""
    if not text:
        return text

    parsed = _try_parse_json(text)
    if parsed is not None:
        return json.dumps(redact_sensitive_data(parsed), ensure_ascii=False)

    redacted = _JSON_STRING_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(3)}", text)
    redacted = _ENV_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", redacted)
    redacted = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", redacted)
    redacted = _SK_TOKEN_RE.sub(REDACTED, redacted)
    return redacted


def redact_for_json(value: Any) -> str:
    return json.dumps(redact_sensitive_data(value), ensure_ascii=False)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _try_parse_json(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None
