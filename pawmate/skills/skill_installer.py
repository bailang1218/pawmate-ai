"""
Safe skill installer — download, extract, validate, register.
Reports each install step for frontend progress display.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from pawmate.core.safety.resource_limits import read_text_limited
from pawmate.skills.dependency_checker import check_skill_dependencies
from pawmate.skills.clawhub_client import ClawHubClient
from pawmate.skills.skill_manifest import parse_skill_md
from pawmate.skills.skill_store import (
    get_default_skills_dir,
    is_installed,
    register_skill,
    write_origin_json,
)

_logger = logging.getLogger("pawmate")

MAX_ZIP_SIZE = 50 * 1024 * 1024  # 50 MB (pre-download)
MAX_FILE_COUNT = 500
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
MAX_UNCOMPRESSED_ZIP_SIZE = 100 * 1024 * 1024  # 100 MB total
MAX_SKILL_MD_SIZE = 512 * 1024
SKILL_MD_REQUIRED = True

ZIP_SLIP_BLOCKED_NAMES = {"..", "~"}
ZIP_SLIP_BLOCKED_PREFIXES = {"../", "..\\", "/", "\\"}


class SkillInstallerError(Exception):
    """Error during skill installation."""
    def __init__(self, message: str, code: str = "import_failed", steps: Optional[List[Dict[str, Any]]] = None):
        self.code = code
        self.steps = steps or []
        super().__init__(message)


class SkillInstaller:
    """Install skills from ClawHub safely."""

    INSTALL_STEPS = [
        "fetch_detail",
        "download_zip",
        "validate_zip",
        "extract",
        "validate_skill_md",
        "parse_manifest",
        "write_metadata",
        "register",
    ]

    def __init__(
        self,
        client: ClawHubClient,
        skills_dir: Optional[Path] = None,
    ):
        self._client = client
        self._skills_dir = skills_dir or get_default_skills_dir()
        self._steps: List[Dict[str, Any]] = []

    def _record_step(
        self,
        name: str,
        status: str = "running",
        message: str = "",
        elapsed_ms: float = 0,
    ) -> None:
        self._steps.append({
            "name": name,
            "status": status,
            "message": message,
            "elapsed_ms": round(elapsed_ms),
        })

    def _fail_step(self, name: str, message: str, elapsed_ms: float = 0) -> None:
        self._steps.append({
            "name": name,
            "status": "failed",
            "message": message,
            "elapsed_ms": round(elapsed_ms),
        })

    def _steps_result(self) -> List[Dict[str, Any]]:
        return list(self._steps)

    def install_from_clawhub(
        self,
        slug: str,
        version: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Download and install a skill from ClawHub.

        Returns dict with 'entry' (registered metadata) and 'steps' (install log).
        Raises SkillInstallerError on failure.
        """
        self._steps = []

        # 0. Already installed check
        if not force and is_installed(slug, skills_dir=self._skills_dir):
            raise SkillInstallerError(
                f"Skill '{slug}' is already installed. Use force=True to override.",
                steps=self._steps_result(),
            )

        # 1. Fetch detail
        t0 = time.time()
        try:
            detail = self._client.get_skill_detail(slug)
            self._record_step("fetch_detail", "done", "Fetched ClawHub metadata", (time.time() - t0) * 1000)
        except Exception as e:
            self._fail_step("fetch_detail", str(e)[:200], (time.time() - t0) * 1000)
            raise SkillInstallerError(f"Failed to fetch skill detail: {e}", steps=self._steps_result()) from e

        skill_info = detail.get("skill", {}) or {}
        lv = detail.get("latestVersion", {}) or {}
        owner_info = detail.get("owner", {}) or {}
        actual_version = lv.get("version") or version or "0.0.0"

        # 2. Download zip
        t0 = time.time()
        _logger.info("[Installer] Downloading %s v%s ...", slug, version or "latest")
        try:
            zip_data = self._client.download_skill_zip(slug, version=version)
            if len(zip_data) > MAX_ZIP_SIZE:
                self._fail_step("download_zip", f"ZIP too large ({len(zip_data)} > {MAX_ZIP_SIZE})", (time.time() - t0) * 1000)
                raise SkillInstallerError(
                    f"ZIP too large ({len(zip_data)} bytes > {MAX_ZIP_SIZE})",
                    code="zip_too_large",
                    steps=self._steps_result(),
                )
            self._record_step("download_zip", "done", f"Downloaded ({len(zip_data)} bytes)", (time.time() - t0) * 1000)
        except Exception as e:
            if isinstance(e, SkillInstallerError):
                raise
            self._fail_step("download_zip", str(e)[:200], (time.time() - t0) * 1000)
            raise SkillInstallerError(f"Download failed: {e}", steps=self._steps_result()) from e

        # 3. Validate zip
        t0 = time.time()
        if len(zip_data) < 4 or zip_data[:4] != b"PK\x03\x04":
            self._fail_step("validate_zip", "Invalid ZIP header", (time.time() - t0) * 1000)
            raise SkillInstallerError("Invalid ZIP file: missing PK header", steps=self._steps_result())
        self._record_step("validate_zip", "done", "ZIP header valid", (time.time() - t0) * 1000)

        # 4. Extract zip
        t0 = time.time()
        target_dir = self._skills_dir / slug
        if not force and target_dir.exists():
            self._fail_step("extract", f"Target dir exists: {target_dir}", (time.time() - t0) * 1000)
            raise SkillInstallerError(
                f"Target directory {target_dir} already exists. "
                "Use force=True to overwrite.",
                steps=self._steps_result(),
            )

        extract_result = self._safe_extract_zip(zip_data, target_dir, force=force)
        if not extract_result.get("ok"):
            self._fail_step("extract", extract_result.get("error", "Extraction failed"), (time.time() - t0) * 1000)
            raise SkillInstallerError(extract_result.get("error", "Extraction failed"), steps=self._steps_result())
        self._record_step("extract", "done", f"Extracted {extract_result.get('file_count', '?')} files", (time.time() - t0) * 1000)

        # 5. Validate SKILL.md
        t0 = time.time()
        skill_md_path = target_dir / "SKILL.md"
        if SKILL_MD_REQUIRED and not skill_md_path.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
            self._fail_step("validate_skill_md", "SKILL.md not found", (time.time() - t0) * 1000)
            raise SkillInstallerError("SKILL.md is missing from the skill archive", steps=self._steps_result())
        self._record_step("validate_skill_md", "done", "SKILL.md found", (time.time() - t0) * 1000)

        # 6. Parse manifest
        t0 = time.time()
        limited_skill_md = read_text_limited(
            skill_md_path,
            encoding="utf-8",
            errors="replace",
            max_bytes=MAX_SKILL_MD_SIZE,
        )
        if limited_skill_md.truncated:
            self._fail_step("parse_manifest", "SKILL.md exceeds size limit", (time.time() - t0) * 1000)
            raise SkillInstallerError(
                f"SKILL.md exceeds size limit ({limited_skill_md.size_bytes} > {MAX_SKILL_MD_SIZE} bytes)",
                code="skill_md_too_large",
                steps=self._steps_result(),
            )
        skill_md_text = limited_skill_md.text
        try:
            manifest = parse_skill_md(skill_md_text)
            self._record_step("parse_manifest", "done", "Manifest parsed", (time.time() - t0) * 1000)
        except Exception as e:
            self._fail_step("parse_manifest", str(e)[:200], (time.time() - t0) * 1000)
            raise SkillInstallerError(f"Failed to parse SKILL.md manifest: {e}", steps=self._steps_result()) from e

        # 7. Write origin.json
        t0 = time.time()
        try:
            origin = {
                "slug": slug,
                "version": actual_version,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "source": "clawhub",
                "origin_url": f"https://clawhub.ai/skills/{slug}",
                "download_url": (
                    f"https://clawhub.ai/api/v1/download?slug={slug}&version={actual_version}"
                ),
                "owner": owner_info.get("handle") or owner_info.get("displayName", ""),
                "display_name": skill_info.get("displayName", slug),
                "summary": skill_info.get("summary") or manifest.get("summary", ""),
                "manifest_warnings": manifest.get("warnings", []),
            }
            write_origin_json(slug, origin, skills_dir=self._skills_dir)
            self._record_step("write_metadata", "done", f"data/skills/{slug}", (time.time() - t0) * 1000)
        except Exception as e:
            self._fail_step("write_metadata", str(e)[:200], (time.time() - t0) * 1000)
            raise SkillInstallerError(f"Failed to write metadata: {e}", steps=self._steps_result()) from e

        # 8. Register in skills.json
        t0 = time.time()
        requires = manifest.get("requires", {}) or {}
        dependency_check = check_skill_dependencies(requires)
        metadata = {
            "slug": slug,
            "name": skill_info.get("displayName", slug),
            "version": actual_version,
            "owner": owner_info.get("handle") or owner_info.get("displayName", ""),
            "summary": skill_info.get("summary") or manifest.get("summary", ""),
            "source": "clawhub",
            "origin_url": origin["origin_url"],
            "download_url": origin["download_url"],
            "requires": requires,
            "dependency_check": dependency_check,
            "warnings": manifest.get("warnings", []),
        }

        try:
            entry = register_skill(metadata, skills_dir=self._skills_dir)
            self._write_dependency_check(slug, dependency_check)
            self._record_step("register", "done", f"Status: {entry.get('status', '?')}", (time.time() - t0) * 1000)
        except ValueError:
            from pawmate.skills.skill_store import load_installed_skills as _load
            entry = next(
                (i for i in _load(self._skills_dir / "skills.json") if i.get("slug") == slug),
                metadata,
            )
            self._record_step("register", "done", "Already registered", (time.time() - t0) * 1000)

        _logger.info(
            "[Installer] Installed %s v%s (status=%s)",
            slug, actual_version, entry.get("status", "?"),
        )

        return {
            "entry": entry,
            "steps": self._steps_result(),
        }

    def _safe_extract_zip(
        self,
        zip_bytes: bytes,
        target_dir: Path,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Extract a zip archive safely."""
        # Validate ZIP header
        if len(zip_bytes) < 4 or zip_bytes[:4] != b"PK\x03\x04":
            return {"ok": False, "error": "Invalid ZIP file"}

        try:
            with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
                names = zf.namelist()

                # File count limit
                if len(names) > MAX_FILE_COUNT:
                    return {
                        "ok": False,
                        "error": f"ZIP contains too many files ({len(names)} > {MAX_FILE_COUNT})",
                    }

                total_uncompressed = 0
                # Validate before extraction
                for name in names:
                    self._validate_zip_entry(name, zf)
                    total_uncompressed += zf.getinfo(name).file_size
                    if total_uncompressed > MAX_UNCOMPRESSED_ZIP_SIZE:
                        return {
                            "ok": False,
                            "error": (
                                f"ZIP uncompressed size too large "
                                f"({total_uncompressed} > {MAX_UNCOMPRESSED_ZIP_SIZE})"
                            ),
                        }

                # Extract
                if not force and target_dir.exists():
                    return {
                        "ok": False,
                        "error": f"Target directory {target_dir} exists",
                    }

                target_dir.mkdir(parents=True, exist_ok=True)

                target_root = target_dir.resolve()
                for name in names:
                    safe_name = self._safe_zip_path(name)
                    target_path = (target_dir / safe_name).resolve()
                    try:
                        target_path.relative_to(target_root)
                    except ValueError:
                        return {
                            "ok": False,
                            "error": f"Zip Slip blocked: {name}",
                        }
                    if name.endswith("/"):
                        target_path.mkdir(parents=True, exist_ok=True)
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name, "r") as src, open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)

            return {"ok": True, "file_count": len(names)}

        except zipfile.BadZipFile as e:
            return {"ok": False, "error": f"Bad ZIP file: {e}"}
        except Exception as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            return {"ok": False, "error": f"Extraction error: {e}"}

    def _validate_zip_entry(self, name: str, zf: zipfile.ZipFile) -> None:
        """Validate a single ZIP entry before extraction."""
        parts = Path(name).parts
        for part in parts:
            if part in ZIP_SLIP_BLOCKED_NAMES:
                raise SkillInstallerError(f"Zip Slip blocked: {name}")

        for prefix in ZIP_SLIP_BLOCKED_PREFIXES:
            if name.startswith(prefix):
                raise SkillInstallerError(f"Zip Slip blocked (absolute path): {name}")

        info = zf.getinfo(name)
        if info.file_size > MAX_FILE_SIZE:
            raise SkillInstallerError(
                f"File too large: {name} ({info.file_size} bytes > {MAX_FILE_SIZE})"
            )

    @staticmethod
    def _safe_zip_path(name: str) -> str:
        """Clean a zip entry name to prevent path traversal."""
        name = name.replace("\\", "/")
        name = name.lstrip("/")
        if ":" in name:
            name = name.split(":", 1)[-1].lstrip("/\\")
        return name

    # ------------------------------------------------------------------
    # Local import from folder or ZIP
    # ------------------------------------------------------------------

    def import_from_folder(self, folder_path: str, force: bool = False) -> Dict[str, Any]:
        """Import a skill from a local folder containing SKILL.md.

        Returns structured result dict with entry and steps.
        Raises SkillInstallerError on failure.
        """
        self._steps = []
        src = Path(folder_path).resolve()

        if not src.exists():
            raise SkillInstallerError(f"Path not found: {folder_path}", steps=[])
        if not src.is_dir():
            raise SkillInstallerError(f"Not a directory: {folder_path}", steps=[])

        # Forbidden paths
        _check_forbidden_import_path(src, self._skills_dir)

        # Check SKILL.md
        skill_md_path = src / "SKILL.md"
        if not skill_md_path.exists():
            raise SkillInstallerError(
                "SKILL.md not found in the selected folder",
                code="missing_skill_md",
                steps=[],
            )

        # Parse manifest
        try:
            limited_skill_md = read_text_limited(
                skill_md_path,
                encoding="utf-8",
                errors="replace",
                max_bytes=MAX_SKILL_MD_SIZE,
            )
            if limited_skill_md.truncated:
                raise SkillInstallerError(
                    f"SKILL.md exceeds size limit ({limited_skill_md.size_bytes} > {MAX_SKILL_MD_SIZE} bytes)",
                    code="skill_md_too_large",
                    steps=[],
                )
            skill_md_text = limited_skill_md.text
            manifest = parse_skill_md(skill_md_text)
        except SkillInstallerError:
            raise
        except Exception as e:
            raise SkillInstallerError(
                f"Failed to parse SKILL.md manifest: {e}",
                code="invalid_manifest",
                steps=[],
            )

        slug = _derive_slug(manifest, src.name)

        # Check not already installed
        if not force and _check_dir_available(slug, self._skills_dir):
            raise SkillInstallerError(
                f"Skill '{slug}' is already installed at data/skills/{slug}",
                code="already_installed",
                steps=[],
            )

        target_dir = self._skills_dir / slug

        # Copy folder
        t0 = time.time()
        try:
            _copy_skill_dir(src, target_dir)
            self._record_step("copy_files", "done", f"Copied to data/skills/{slug}", (time.time() - t0) * 1000)
        except Exception as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise SkillInstallerError(
                f"Failed to copy skill: {e}",
                code="import_failed",
                steps=self._steps_result(),
            )

        # Write .pawmate metadata
        lv = manifest.get("version", "0.0.0")
        actual_version = str(lv) if lv else "0.0.0"
        self._write_import_metadata(slug, actual_version, manifest, "local_folder", {
            "original_path": str(src),
        })

        # Register in skills.json
        entry = self._register_imported(slug, manifest, actual_version, "local_folder")

        _logger.info(
            "[Installer] Imported %s v%s from folder (status=%s)",
            slug, actual_version, entry.get("status", "?"),
        )

        return {
            "entry": entry,
            "steps": self._steps_result(),
        }

    def import_from_zip(self, zip_path: str, force: bool = False) -> Dict[str, Any]:
        """Import a skill from a local ZIP file.

        Returns structured result dict with entry and steps.
        Raises SkillInstallerError on failure.
        """
        self._steps = []
        src = Path(zip_path).resolve()

        if not src.exists():
            raise SkillInstallerError(f"ZIP not found: {zip_path}", code="local_zip_not_found", steps=[])
        if not src.is_file():
            raise SkillInstallerError(f"Not a file: {zip_path}", code="invalid_zip", steps=[])

        size = src.stat().st_size
        if size > MAX_ZIP_SIZE:
            raise SkillInstallerError(
                f"ZIP too large ({size} bytes > {MAX_ZIP_SIZE})",
                code="zip_too_large",
                steps=[],
            )

        zip_bytes = src.read_bytes()

        # Validate zip header
        if len(zip_bytes) < 4 or zip_bytes[:4] != b"PK\x03\x04":
            raise SkillInstallerError("Invalid ZIP file", code="invalid_zip", steps=[])

        # Extract to temp directory
        tmp_dir = self._skills_dir / "_tmp" / f"import_{int(time.time())}"
        t0 = time.time()
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            extract_ok = self._safe_extract_zip_to(zip_bytes, tmp_dir)
            if not extract_ok.get("ok"):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                err_msg = extract_ok.get("error", "Extraction failed")
                code = "zip_slip_detected" if "Zip Slip" in err_msg else "invalid_zip"
                raise SkillInstallerError(err_msg, code=code, steps=[])
            self._record_step("extract_zip", "done", f"Extracted to temp", (time.time() - t0) * 1000)
        except SkillInstallerError:
            raise
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise SkillInstallerError(f"Extraction error: {e}", code="invalid_zip", steps=[]) from e

        # Find SKILL.md (handle nested folder)
        skill_dir = _find_skill_root(tmp_dir)
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise SkillInstallerError(
                "SKILL.md not found in ZIP",
                code="missing_skill_md",
                steps=self._steps_result(),
            )

        # Parse manifest
        try:
            limited_skill_md = read_text_limited(
                skill_md_path,
                encoding="utf-8",
                errors="replace",
                max_bytes=MAX_SKILL_MD_SIZE,
            )
            if limited_skill_md.truncated:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise SkillInstallerError(
                    f"SKILL.md exceeds size limit ({limited_skill_md.size_bytes} > {MAX_SKILL_MD_SIZE} bytes)",
                    code="skill_md_too_large",
                    steps=self._steps_result(),
                )
            skill_md_text = limited_skill_md.text
            manifest = parse_skill_md(skill_md_text)
        except SkillInstallerError:
            raise
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise SkillInstallerError(
                f"Failed to parse SKILL.md manifest: {e}",
                code="invalid_manifest",
                steps=self._steps_result(),
            )

        slug = _derive_slug(manifest, skill_dir.name)

        # Check not already installed
        if not force and _check_dir_available(slug, self._skills_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise SkillInstallerError(
                f"Skill '{slug}' is already installed at data/skills/{slug}",
                code="already_installed",
                steps=self._steps_result(),
            )

        target_dir = self._skills_dir / slug
        if target_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise SkillInstallerError(
                f"Target directory data/skills/{slug} exists",
                code="already_installed",
                steps=self._steps_result(),
            )

        # Move from temp to final location
        t0 = time.time()
        try:
            skill_dir.rename(target_dir)
            self._record_step("move_to_skills", "done", f"Moved to data/skills/{slug}", (time.time() - t0) * 1000)
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise SkillInstallerError(
                f"Failed to move: {e}",
                code="import_failed",
                steps=self._steps_result(),
            )

        # Clean up temp
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Write .pawmate metadata
        lv = manifest.get("version", "0.0.0")
        actual_version = str(lv) if lv else "0.0.0"
        self._write_import_metadata(slug, actual_version, manifest, "local_zip", {
            "original_filename": src.name,
        })

        # Register in skills.json
        entry = self._register_imported(slug, manifest, actual_version, "local_zip")

        _logger.info(
            "[Installer] Imported %s v%s from ZIP (status=%s)",
            slug, actual_version, entry.get("status", "?"),
        )

        return {
            "entry": entry,
            "steps": self._steps_result(),
        }

    def _safe_extract_zip_to(self, zip_bytes: bytes, target_dir: Path) -> Dict[str, Any]:
        """Extract zip bytes to a target directory with safety checks."""
        try:
            with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
                names = zf.namelist()
                if len(names) > MAX_FILE_COUNT:
                    return {"ok": False, "error": f"ZIP contains too many files ({len(names)} > {MAX_FILE_COUNT})"}
                total_uncompressed = 0
                for name in names:
                    self._validate_zip_entry(name, zf)
                    total_uncompressed += zf.getinfo(name).file_size
                    if total_uncompressed > MAX_UNCOMPRESSED_ZIP_SIZE:
                        return {
                            "ok": False,
                            "error": (
                                f"ZIP uncompressed size too large "
                                f"({total_uncompressed} > {MAX_UNCOMPRESSED_ZIP_SIZE})"
                            ),
                        }
                target_dir.mkdir(parents=True, exist_ok=True)
                target_root = target_dir.resolve()
                for name in names:
                    safe_name = self._safe_zip_path(name)
                    target_path = (target_dir / safe_name).resolve()
                    try:
                        target_path.relative_to(target_root)
                    except ValueError:
                        return {"ok": False, "error": f"Zip Slip blocked: {name}"}
                    if name.endswith("/"):
                        target_path.mkdir(parents=True, exist_ok=True)
                        continue
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name, "r") as src, open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            return {"ok": True, "file_count": len(names)}
        except zipfile.BadZipFile as e:
            return {"ok": False, "error": f"Bad ZIP file: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"Extraction error: {e}"}

    def _write_import_metadata(
        self, slug: str, version: str, manifest: Dict[str, Any],
        source: str, source_info: Dict[str, Any],
    ) -> None:
        """Write .pawmate/origin.json, review.json, dependency_check.json."""
        import json as _json
        from datetime import datetime, timezone

        pawmate_dir = self._skills_dir / slug / ".pawmate"
        pawmate_dir.mkdir(parents=True, exist_ok=True)

        # origin.json
        origin = {
            "source": source,
            "slug": slug,
            "version": version,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "local_path": f"data/skills/{slug}",
            **source_info,
        }
        with open(pawmate_dir / "origin.json", "w", encoding="utf-8") as f:
            _json.dump(origin, f, indent=2, ensure_ascii=False)
        self._record_step("write_origin", "done", f"data/skills/{slug}/.pawmate/origin.json")

        # review.json
        review = {
            "reviewed": False,
            "status": "needs_review",
            "notes": "Imported locally and requires review before enabling.",
        }
        with open(pawmate_dir / "review.json", "w", encoding="utf-8") as f:
            _json.dump(review, f, indent=2, ensure_ascii=False)
        self._record_step("write_review", "done", f"data/skills/{slug}/.pawmate/review.json")

        dep_check = check_skill_dependencies(manifest.get("requires", {}) or {})
        self._write_dependency_check(slug, dep_check)

    def _write_dependency_check(self, slug: str, dependency_check: Dict[str, Any]) -> None:
        import json as _json

        pawmate_dir = self._skills_dir / slug / ".pawmate"
        pawmate_dir.mkdir(parents=True, exist_ok=True)
        with open(pawmate_dir / "dependency_check.json", "w", encoding="utf-8") as f:
            _json.dump(dependency_check, f, indent=2, ensure_ascii=False)
        self._record_step("write_dep_check", "done", f"data/skills/{slug}/.pawmate/dependency_check.json")

    def _register_imported(
        self, slug: str, manifest: Dict[str, Any],
        version: str, source: str,
    ) -> Dict[str, Any]:
        """Register imported skill in skills.json."""
        summary = manifest.get("summary", "")
        requires = manifest.get("requires", {}) or {}
        dependency_check = check_skill_dependencies(requires)
        warnings = manifest.get("warnings", [])
        display_name = manifest.get("name", slug)

        metadata = {
            "slug": slug,
            "name": display_name,
            "version": version,
            "owner": "",
            "summary": summary,
            "source": source,
            "origin_url": "",
            "download_url": "",
            "requires": requires,
            "dependency_check": dependency_check,
            "warnings": warnings,
        }

        try:
            entry = register_skill(metadata, skills_dir=self._skills_dir)
            self._record_step("register", "done", f"Status: {entry.get('status', '?')}")
        except ValueError:
            from pawmate.skills.skill_store import load_installed_skills as _load
            entry = next(
                (i for i in _load(self._skills_dir / SKILLS_JSON) if i.get("slug") == slug),
                metadata,
            )
            self._record_step("register", "done", "Already registered")

        return entry


# --------------------------------------------------------------------------
# Module-level helpers for local import
# --------------------------------------------------------------------------


def _check_forbidden_import_path(src: Path, skills_dir: Path) -> None:
    """Raise SkillInstallerError if src is a forbidden import location."""
    src_resolved = src.resolve()
    skills_resolved = skills_dir.resolve()

    if src_resolved == skills_resolved:
        raise SkillInstallerError(
            "Cannot import data/skills directory itself",
            code="import_source_forbidden",
        )

    try:
        import pawmate.skills as _psk
        psk_path = Path(_psk.__file__).resolve().parent
        if src_resolved == psk_path or psk_path in src_resolved.parents:
            raise SkillInstallerError(
                "Cannot import from pawmate/skills source directory",
                code="import_source_forbidden",
            )
    except (ImportError, AttributeError):
        pass


def _derive_slug(manifest: Dict[str, Any], folder_name: str) -> str:
    """Derive a safe slug from manifest or folder name."""
    import re
    slug = manifest.get("slug", "") or manifest.get("name", folder_name)
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "-", slug).strip("-").lower()
    if not slug or not re.match(r"^[a-zA-Z0-9_.-]+$", slug):
        raise SkillInstallerError(
            f"Invalid slug derived: {slug!r}", code="invalid_slug"
        )
    if ".." in slug:
        raise SkillInstallerError(
            f"Path traversal blocked in slug: {slug!r}", code="invalid_slug"
        )
    return slug


def _check_dir_available(slug: str, skills_dir: Path) -> bool:
    """Check if slug is already registered in skills.json."""
    from pawmate.skills.skill_store import is_installed
    return is_installed(slug, skills_dir=skills_dir)


def _copy_skill_dir(src: Path, dst: Path) -> None:
    """Copy a skill directory to destination safely."""
    if dst.exists():
        raise SkillInstallerError(
            f"Target directory {dst} already exists", code="already_installed"
        )
    shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
    if not (dst / "SKILL.md").exists():
        shutil.rmtree(dst, ignore_errors=True)
        raise SkillInstallerError("SKILL.md was not copied", code="import_failed")


def _find_skill_root(tmp_dir: Path) -> Path:
    """Find skill root inside extracted ZIP (handles nested top-level dir)."""
    entries = list(tmp_dir.iterdir())
    dirs = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return tmp_dir
