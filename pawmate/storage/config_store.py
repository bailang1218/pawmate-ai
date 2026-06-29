"""Simple persistent key-value config store for PawMate."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def _default_config_path() -> Path:
    env_path = os.environ.get("PAWMATE_CONFIG_STORE")
    if env_path:
        return Path(env_path).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "PawMate" / "config_store.json"

    return Path.home() / ".pawmate" / "config_store.json"


class ConfigStore:
    def __init__(self, path: Optional[str] = None):
        self._path = Path(path).expanduser().resolve() if path else _default_config_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self._save()

    def update(self, values: Dict[str, Any]) -> None:
        self._data.update(values)
        self._save()

    def all(self) -> Dict[str, Any]:
        return dict(self._data)
