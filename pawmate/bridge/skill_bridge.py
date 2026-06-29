"""Skill boundary adapter used by UI bridges.

This module is the only Phase 4 adapter that should know about concrete
``pawmate.skills`` internals. WebBridge depends on the SkillPort shape instead
of importing skill implementation modules directly.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol


class SkillPort(Protocol):
    """Minimal WebBridge-facing skill operations.

    TODO: These methods return WebBridge-compatible JSON-key dictionaries as a
    compatibility shim. If WS or typed UI reuses this boundary later, replace
    the shim with typed DTOs.
    """

    def list_clawhub_skills(self) -> dict[str, Any]: ...
    def search_clawhub_skills(self, query: str) -> dict[str, Any]: ...
    def get_clawhub_skill_detail(self, slug: str) -> dict[str, Any]: ...
    def install_clawhub_skill(self, slug: str, version: str = "") -> dict[str, Any]: ...
    def diagnose_clawhub_connection(self) -> dict[str, Any]: ...
    def import_local_skill_folder(self, folder_path: str) -> dict[str, Any]: ...
    def import_local_skill_zip(self, zip_path: str) -> dict[str, Any]: ...
    def list_local_skills(self) -> dict[str, Any]: ...
    def set_skill_status(self, slug: str, new_status: str) -> dict[str, Any]: ...
    def get_local_skill_detail(self, slug: str) -> dict[str, Any]: ...
    def list_builtin_skills(self) -> dict[str, Any]: ...
    def install_builtin_skill(self, slug: str) -> dict[str, Any]: ...
    def uninstall_local_skill(self, slug: str) -> dict[str, Any]: ...


class SkillBridgeError(Exception):
    """Boundary error with WebBridge-compatible code/details metadata."""

    def __init__(
        self,
        message: str,
        code: str = "clawhub_unknown",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class SkillBridge:
    """Concrete adapter from SkillPort to the current skills subsystem."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        installer: Any | None = None,
        manager_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._client = client
        self._installer = installer
        self._manager_factory = manager_factory

    def _get_client(self) -> Any:
        if self._client is None:
            from pawmate.skills.clawhub_client import ClawHubClient

            self._client = ClawHubClient()
        return self._client

    def _get_installer(self) -> Any:
        if self._installer is None:
            from pawmate.skills.skill_installer import SkillInstaller

            self._installer = SkillInstaller(self._get_client())
        return self._installer

    def _make_manager(self) -> Any:
        if self._manager_factory is not None:
            return self._manager_factory()
        from pawmate.skills.skill_manager import SkillManager

        return SkillManager()

    def list_clawhub_skills(self) -> dict[str, Any]:
        try:
            result = self._get_client().list_skills(limit=50)
            return {"items": result.get("items") or []}
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def search_clawhub_skills(self, query: str) -> dict[str, Any]:
        try:
            return {"items": self._get_client().search_skills(query)}
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def get_clawhub_skill_detail(self, slug: str) -> dict[str, Any]:
        try:
            client = self._get_client()
            return {
                "skill": client.get_skill_detail(slug),
                "skill_md": client.get_skill_file(slug, "SKILL.md"),
            }
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def install_clawhub_skill(self, slug: str, version: str = "") -> dict[str, Any]:
        # TODO-Phase7: route skill installation through the permission service.
        try:
            ver = version.strip() or None
            return {"installed": self._get_installer().install_from_clawhub(slug, version=ver)}
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def diagnose_clawhub_connection(self) -> dict[str, Any]:
        try:
            return dict(self._get_client().diagnose())
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def import_local_skill_folder(self, folder_path: str) -> dict[str, Any]:
        # TODO-Phase7: route local skill import through the permission service.
        try:
            return {"imported": self._get_installer().import_from_folder(folder_path)}
        except Exception as exc:
            raise _to_skill_bridge_error(exc, include_installer_code_in_details=True) from exc

    def import_local_skill_zip(self, zip_path: str) -> dict[str, Any]:
        # TODO-Phase7: route local skill import through the permission service.
        try:
            return {"imported": self._get_installer().import_from_zip(zip_path)}
        except Exception as exc:
            raise _to_skill_bridge_error(exc, include_installer_code_in_details=True) from exc

    def list_local_skills(self) -> dict[str, Any]:
        try:
            from pawmate.skills.skill_store import list_local_skills

            return {"items": list_local_skills()}
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def set_skill_status(self, slug: str, new_status: str) -> dict[str, Any]:
        # TODO-Phase7: route skill status mutation through the permission service.
        try:
            from pawmate.skills.skill_store import update_skill_status

            item = update_skill_status(slug, new_status)
            if item:
                return {"item": item}
            raise SkillBridgeError(f"Skill '{slug}' not found")
        except SkillBridgeError:
            raise
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def get_local_skill_detail(self, slug: str) -> dict[str, Any]:
        try:
            manager = self._make_manager()
            detail = manager.get_detail(slug)
            if not detail:
                raise SkillBridgeError(f"Skill '{slug}' not found")
            return {"detail": detail, "skill_md": manager.read_skill_md(slug) or ""}
        except SkillBridgeError:
            raise
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def list_builtin_skills(self) -> dict[str, Any]:
        try:
            return {"items": self._make_manager().list_builtin_skills()}
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def install_builtin_skill(self, slug: str) -> dict[str, Any]:
        # TODO-Phase7: route built-in skill install through the permission service.
        try:
            return {"installed": self._make_manager().install_builtin(slug)}
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc

    def uninstall_local_skill(self, slug: str) -> dict[str, Any]:
        # TODO-Phase7: route local skill uninstall through the permission service.
        try:
            return {"result": self._make_manager().uninstall(slug)}
        except Exception as exc:
            raise _to_skill_bridge_error(exc) from exc


def create_default_skill_bridge() -> SkillPort:
    return SkillBridge()


def _to_skill_bridge_error(
    exc: Exception,
    *,
    include_installer_code_in_details: bool = False,
) -> SkillBridgeError:
    from pawmate.skills.clawhub_client import ClawHubError
    from pawmate.skills.skill_installer import SkillInstallerError

    if isinstance(exc, SkillBridgeError):
        return exc
    if isinstance(exc, ClawHubError):
        return SkillBridgeError(str(exc), code=exc.code, details=exc.details)
    if isinstance(exc, SkillInstallerError):
        details: dict[str, Any] | None = None
        if include_installer_code_in_details:
            details = {"code": exc.code}
        if exc.steps:
            details = details or {}
            details["steps"] = exc.steps
        return SkillBridgeError(str(exc), code=exc.code, details=details)
    return SkillBridgeError(str(exc))
