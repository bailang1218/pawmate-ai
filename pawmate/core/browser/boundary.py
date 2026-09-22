"""Browser automation boundary metadata."""
from __future__ import annotations

from enum import Enum
from typing import Any


class BrowserActionType(str, Enum):
    NAVIGATE = "navigate"
    READ = "read"
    EXTRACT = "extract"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    PRESS = "press"
    SELECT = "select"
    SUBMIT = "submit"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    COORDINATE_CLICK = "coordinate_click"


class BrowserRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_SUBMIT_WORDS = {
    "submit",
    "send",
    "post",
    "publish",
    "confirm",
    "checkout",
    "pay",
    "purchase",
    "buy",
    "place order",
    "approve",
    "unsubscribe",
    "delete",
    "remove",
    "transfer",
    "发送",
    "提交",
    "发布",
    "删除",
    "付款",
    "支付",
    "购买",
    "下单",
    "转账",
    "清空",
}


def classify_browser_action(
    action: str,
    *,
    intent: str = "",
    text: str = "",
    key: str = "",
    x: int | None = None,
    y: int | None = None,
) -> tuple[BrowserActionType, BrowserRisk]:
    action_norm = str(action or "").strip().lower()
    semantic_text = f"{intent} {text} {key}".lower()
    if action_norm == "click_xy" or x is not None or y is not None:
        return BrowserActionType.COORDINATE_CLICK, BrowserRisk.HIGH
    if action_norm in {"scroll"}:
        return BrowserActionType.SCROLL, BrowserRisk.LOW
    if action_norm in {"key", "hotkey"}:
        key_tokens = {part.strip().lower() for part in key.replace("+", " ").split()}
        if "enter" in key_tokens:
            return BrowserActionType.SUBMIT, BrowserRisk.HIGH
        return BrowserActionType.PRESS, BrowserRisk.MEDIUM
    if action_norm in {"type", "fill", "insert_text", "smart_type"}:
        if _contains_submit_semantics(semantic_text):
            return BrowserActionType.SUBMIT, BrowserRisk.HIGH
        return BrowserActionType.TYPE, BrowserRisk.MEDIUM
    if action_norm == "click" and _contains_submit_semantics(semantic_text):
        if any(
            word in semantic_text
            for word in (
                "pay",
                "purchase",
                "buy",
                "place order",
                "checkout",
                "delete",
                "remove",
                "transfer",
                "付款",
                "支付",
                "购买",
                "下单",
                "删除",
                "转账",
                "清空",
            )
        ):
            return BrowserActionType.SUBMIT, BrowserRisk.CRITICAL
        return BrowserActionType.SUBMIT, BrowserRisk.HIGH
    return BrowserActionType.CLICK, BrowserRisk.MEDIUM


def annotate_browser_result(
    result: dict[str, Any],
    *,
    action: BrowserActionType,
    risk: BrowserRisk,
    backend: str,
    fallback: str = "",
    sensitive: bool = False,
    untrusted_observation: bool = True,
) -> dict[str, Any]:
    annotated = dict(result)
    session = annotated.get("session") if isinstance(annotated.get("session"), dict) else {}
    annotated["browser_boundary"] = {
        "action": action.value,
        "risk": risk.value,
        "backend": backend,
        "fallback": fallback,
        "url": annotated.get("url") or session.get("url") or "",
        "title": annotated.get("title") or session.get("title") or "",
        "session": session,
        "sensitive": bool(sensitive),
        "untrusted_observation": bool(untrusted_observation),
    }
    return annotated


def is_logged_in_sensitive(*, use_my_login: bool = False, surface: str = "", session: dict[str, Any] | None = None) -> bool:
    if use_my_login:
        return True
    surface_text = str(surface or "").lower()
    if "native" in surface_text or "attached" in surface_text:
        return True
    session = session or {}
    return bool(session.get("attached") or session.get("profile") in {"native", "cdp"})


def _contains_submit_semantics(text: str) -> bool:
    return any(word in text for word in _SUBMIT_WORDS)
