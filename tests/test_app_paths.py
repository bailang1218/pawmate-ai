from __future__ import annotations

from pawmate.memory.memory_store import MemoryStore
from pawmate.storage import app_paths
from pawmate.storage.session_store import SessionStore


def test_configured_data_root_owns_shared_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_paths, "_default_paths", None)
    (tmp_path / "sessions.db").touch()
    paths = app_paths.configure_app_paths({"data": {"root_dir": str(tmp_path)}})

    session_store = SessionStore()
    memory_store = MemoryStore()

    assert paths.database_path == tmp_path / "sessions.db"
    assert session_store.db_path == paths.database_path
    assert memory_store.db_path == paths.database_path
    assert paths.logs_dir.parent == tmp_path
    assert paths.skills_dir.parent == tmp_path
