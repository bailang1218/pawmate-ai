"""Narrow facade for side-effect security policy checks."""
from __future__ import annotations

from typing import Any, Callable, Protocol

from pawmate.core.safety.file_boundary import assert_file_path_allowed
from pawmate.core.safety.path_security import sanitize_path
from pawmate.core.runtime.runtime_config import get_security_config


class ConfirmationGate(Protocol):
    async def request_confirmation(self, name: str, input_data: dict) -> bool: ...


class SecurityService:
    """Thin entry point for security checks shared by side-effect callers."""

    def __init__(
        self,
        security_config_provider: Callable[[], dict[str, Any]] = get_security_config,
        confirm_gate: ConfirmationGate | None = None,
    ) -> None:
        self._security_config_provider = security_config_provider
        self._confirm_gate = confirm_gate

    def set_confirm_gate(self, confirm_gate: ConfirmationGate | None) -> None:
        self._confirm_gate = confirm_gate

    def check_path(self, path: str, *, mode: str) -> str:
        """Validate a path with the existing path_security policy.

        ``mode`` is recorded by callers for the future policy facade, but this
        skeleton deliberately preserves today's path_security behavior.
        """
        safe_path = sanitize_path(path, self._security_config_provider())
        assert_file_path_allowed(safe_path, mode=mode)
        return safe_path

    async def require_confirmation(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        reason: str = "",
    ) -> bool:
        """Ask the existing confirmation gate before a side-effect runs."""
        if self._confirm_gate is None:
            return False
        request_payload = dict(payload)
        if reason:
            request_payload["reason"] = reason
        return await self._confirm_gate.request_confirmation(action, request_payload)


__all__ = ["SecurityService"]
