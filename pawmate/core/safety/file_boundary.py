"""File access boundary checks shared by local tool handlers."""
from __future__ import annotations

from pathlib import Path


class FileBoundaryError(Exception):
    """Raised when a file path or content shape is unsafe for model context."""


SECRET_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "known_hosts",
}

SECRET_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".crt",
    ".cer",
}

TEXT_LIKE_SUFFIXES = {
    "",
    ".bat",
    ".cfg",
    ".cmd",
    ".conf",
    ".css",
    ".csv",
    ".env.example",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".py",
    ".ps1",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def is_secret_path(path: str | Path) -> bool:
    target = Path(path)
    name = target.name.lower()
    if name in SECRET_FILENAMES:
        return True
    if target.suffix.lower() in SECRET_SUFFIXES:
        return True
    return any(part.lower() in {".ssh", ".aws", ".azure", ".gnupg"} for part in target.parts)


def is_binary_sample(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        text_bytes = sum(1 for byte in data if byte in (9, 10, 13) or 32 <= byte <= 126 or byte >= 128)
        return (text_bytes / max(len(data), 1)) < 0.85


def assert_text_file_readable(path: str | Path) -> None:
    target = Path(path)
    if is_secret_path(target):
        raise FileBoundaryError(f"secret-like file is not readable by model tools: {target}")
    if target.suffix.lower() not in TEXT_LIKE_SUFFIXES:
        raise FileBoundaryError(f"binary or unsupported file type is not readable as text: {target}")
    if target.exists() and target.is_file():
        with target.open("rb") as fp:
            sample = fp.read(4096)
        if is_binary_sample(sample):
            raise FileBoundaryError(f"binary file is not readable as text: {target}")


def assert_file_path_allowed(path: str | Path, *, mode: str) -> None:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "read":
        assert_text_file_readable(path)
    elif normalized_mode in {"write", "move", "execute"} and is_secret_path(path):
        raise FileBoundaryError(f"secret-like file is not writable/executable by model tools: {path}")
