"""
Runtime data directory paths — centralised path management.

All runtime data (skills, memory, downloads, cache, logs, etc.)
lives under a single data root, defaulting to <project_root>/data/.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def get_project_root() -> Path:
    """Return the project root directory (parent of pawmate/)."""
    return Path(__file__).resolve().parent.parent.parent


class AppPaths:
    """Centralised runtime data directory paths.

    Priority for data_root:
      1. config["data"]["root_dir"]
      2. PAWMATE_DATA_DIR environment variable
      3. <project_root>/data/
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        data_root: Optional[Path] = None,
        config: Optional[dict] = None,
    ):
        self._project_root = (project_root or get_project_root()).resolve()
        self._data_root = self._resolve_data_root(data_root, config or {})
        self._config = config or {}

    # ── Root paths ────────────────────────────────────────────

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def data_root(self) -> Path:
        return self._data_root

    # ── Runtime sub-directories ───────────────────────────────

    @property
    def skills_dir(self) -> Path:
        """Directory for installed user skills (ClawHub / local)."""
        cfg_skills = (self._config.get("skills") or {}).get("local_dir")
        if cfg_skills:
            return self._resolve_rel(cfg_skills)
        return self.data_root / "skills"

    @property
    def memory_dir(self) -> Path:
        """Reserved for future memory engine."""
        return self.data_root / "memory"

    @property
    def conversations_dir(self) -> Path:
        """Reserved for future conversation history."""
        return self.data_root / "conversations"

    @property
    def cache_dir(self) -> Path:
        """Reserved for cache data."""
        return self.data_root / "cache"

    @property
    def logs_dir(self) -> Path:
        """Reserved for application logs."""
        return self.data_root / "logs"

    @property
    def downloads_dir(self) -> Path:
        """New download cache. Legacy root/downloads/ not affected."""
        return self.data_root / "downloads"

    @property
    def qtwebengine_dir(self) -> Path:
        """Reserved for future QtWebEngine profile/cache migration."""
        return self.data_root / "qtwebengine"

    # ── Directory creation ────────────────────────────────────

    def ensure_dirs(self) -> None:
        """Create all runtime data directories.

        Does NOT touch:
        - <project_root>/downloads/   (legacy, not migrated)
        - <project_root>/.qtwebengine_data/  (legacy, not migrated)
        """
        for attr in (
            "skills_dir",
            "memory_dir",
            "conversations_dir",
            "cache_dir",
            "logs_dir",
            "downloads_dir",
            "qtwebengine_dir",
        ):
            getattr(self, attr).mkdir(parents=True, exist_ok=True)

    # ── Internal helpers ──────────────────────────────────────

    def _resolve_data_root(self, data_root: Optional[Path], config: dict) -> Path:
        # 1. Explicit data_root argument
        if data_root is not None:
            return data_root.resolve()

        # 2. Config key
        cfg_root = config.get("data", {}).get("root_dir")
        if cfg_root:
            return self._resolve_rel(cfg_root)

        # 3. Environment variable
        env_root = os.environ.get("PAWMATE_DATA_DIR")
        if env_root:
            return Path(env_root).resolve()

        # 4. Default: project_root/data/
        return (self._project_root / "data").resolve()

    def _resolve_rel(self, path_str: str) -> Path:
        """Resolve a potentially relative path against project_root."""
        p = Path(path_str)
        if p.is_absolute():
            return p.resolve()
        return (self._project_root / p).resolve()


# Module-level convenience
_default_paths: Optional[AppPaths] = None


def get_app_paths(config: Optional[dict] = None) -> AppPaths:
    """Get the default AppPaths singleton."""
    global _default_paths
    if _default_paths is None:
        _default_paths = AppPaths(config=config or {})
    return _default_paths


def ensure_runtime_dirs(config: Optional[dict] = None) -> None:
    """Ensure all runtime directories exist."""
    get_app_paths(config).ensure_dirs()
