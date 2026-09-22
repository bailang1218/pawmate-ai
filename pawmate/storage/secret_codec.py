"""Protect secret configuration fields at rest with Windows DPAPI."""
from __future__ import annotations

import base64
import os

SECRET_KEYS = {"api_key", "apikey", "auth_token", "access_token", "refresh_token", "password", "secret"}
PREFIX = "dpapi:"


def _protect(value: str) -> str:
    if not value or value.startswith(PREFIX) or os.name != "nt":
        return value
    import win32crypt

    blob = win32crypt.CryptProtectData(value.encode("utf-8"), "PawMate", None, None, None, 0)
    return PREFIX + base64.b64encode(blob).decode("ascii")


def _unprotect(value: str) -> str:
    if not value.startswith(PREFIX):
        return value
    if os.name != "nt":
        return ""
    try:
        import win32crypt

        blob = base64.b64decode(value[len(PREFIX):], validate=True)
        return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return ""


def protect_config(value, key: str = ""):
    if isinstance(value, dict):
        return {item_key: protect_config(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [protect_config(item) for item in value]
    if key.lower() in SECRET_KEYS and isinstance(value, str):
        return _protect(value)
    return value


def unprotect_config(value, key: str = ""):
    if isinstance(value, dict):
        return {item_key: unprotect_config(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [unprotect_config(item) for item in value]
    if key.lower() in SECRET_KEYS and isinstance(value, str):
        return _unprotect(value)
    return value
