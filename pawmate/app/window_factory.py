"""
Window factory — single entry point for creating the main application window.

Routes to Web UI (default) or legacy Qt UI based on config, with backward
compatibility for the old use_web_ui boolean flag.

Resolution order: ui_mode > use_web_ui > default "web".
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def create_main_window(
    config: Dict[str, Any],
    services: Optional[Dict[str, Any]] = None,
) -> Any:
    """Create and return the main application window.

    Parameters
    ----------
    config : dict
        Application configuration (from config.json / PawMateApp._cfg).
    services : dict or None
        Optional shared service references (reserved for future use).

    Returns
    -------
    WebChatWindow or MainWindow
        The created window instance, ready to show().

    Raises
    ------
    ValueError
        If ui_mode is not one of "web" or "legacy".
    """
    ui_mode = _resolve_ui_mode(config)

    if ui_mode == "web":
        from pawmate.ui.web_chat_window import WebChatWindow
        import logging
        _log = logging.getLogger("pawmate")

        mock = _resolve_web_mock_mode(config)
        _log.info("[WindowFactory] ui_mode=%s web_ui_mock_mode=%s", ui_mode, mock)
        window = WebChatWindow(mock_mode=mock)
        return window

    raise ValueError(f"Unknown ui_mode: {ui_mode!r}")


def _resolve_web_mock_mode(config: Dict[str, Any]) -> bool:
    """Resolve web_ui_mock_mode from config.

    Priority: ui.web_ui_mock_mode (explicit) > legacy fields (fallback) > False.
    Legacy fallback checks: ui.mock, web.mock, web.web_ui_mock (top-level).
    """
    ui_cfg = config.get("ui", {})
    if "web_ui_mock_mode" in ui_cfg:
        return bool(ui_cfg["web_ui_mock_mode"])
    # Legacy fallback fields (only if explicit new field is absent)
    for legacy_key in ("mock", "web_mock"):
        val = ui_cfg.get(legacy_key)
        if val is not None:
            return bool(val)
    web_cfg = config.get("web", {})
    val = web_cfg.get("mock")
    if val is not None:
        return bool(val)
    return False


def _resolve_ui_mode(config: Dict[str, Any]) -> str:
    """Resolve ui_mode with legacy use_web_ui compatibility."""
    ui_cfg = config.get("ui", {})
    explicit = ui_cfg.get("ui_mode")
    if explicit:
        return str(explicit)
    if "use_web_ui" in ui_cfg:
        return "web" if bool(ui_cfg["use_web_ui"]) else "legacy"
    return "web"
