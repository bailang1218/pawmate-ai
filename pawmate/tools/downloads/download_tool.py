"""
下载工具，用于从网络下载文件到本地。
"""
import asyncio
import aiohttp
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse
import hashlib
import json
import re
from datetime import datetime

from pawmate.core.safety.file_boundary import FileBoundaryError
from pawmate.core.safety.network_boundary import (
    NetworkBoundaryError,
    download_boundary_metadata,
    is_executable_download,
    validate_external_http_url,
)
from pawmate.core.safety.path_security import PathSecurityError
from pawmate.core.safety.security_service import SecurityService
from pawmate.tools.core.registry import (
    APPROVAL_CONFIRM,
    RiskLevel,
    SideEffectLevel,
    ToolCategory,
    ToolDef,
    ToolRegistry,
)
from pawmate.storage.app_paths import get_app_paths


logger = logging.getLogger("pawmate")
MAX_DOWNLOAD_BYTES = int(os.getenv("PAWMATE_MAX_DOWNLOAD_BYTES", str(100 * 1024 * 1024)))


def _download_dir(destination_folder: str = "") -> Path:
    if destination_folder:
        return Path(destination_folder).expanduser()
    return get_app_paths().downloads_dir


def _safe_filename(filename: str) -> str:
    name = Path(filename.strip()).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name)
    return name or "download.bin"


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    idx = 1
    while True:
        candidate = path.parent / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


async def download_file(
    url: str,
    destination_folder: str = "",
    filename: str = "",
    timeout: int = 60,
    security_service: Optional[SecurityService] = None,
    already_confirmed: bool = False,
) -> dict:
    """Download a file, optionally requiring confirmation before side effects."""
    try:
        service = security_service or SecurityService()
        safe_url = validate_external_http_url(url)
        dest_path, file_path, final_filename = _prepare_download_target(
            safe_url,
            destination_folder,
            filename,
            service,
        )
        executable = is_executable_download(file_path)

        if executable and security_service is None and not already_confirmed:
            return {
                "ok": False,
                "operation": "download_file",
                "error_type": "approval_unavailable",
                "message": "executable downloads require confirmation",
                "url": safe_url,
                "filename": final_filename,
            }

        if security_service is not None and not already_confirmed:
            allowed = await security_service.require_confirmation(
                "download_file",
                {
                    "url": safe_url,
                    "destination_folder": destination_folder,
                    "filename": final_filename,
                    "dest": str(file_path),
                    "timeout": timeout,
                    "executable": executable,
                },
                reason="download_file writes a network response to disk",
            )
            if not allowed:
                return {
                    "ok": False,
                    "operation": "download_file",
                    "error_type": "approval_denied",
                    "message": "download_file was not approved",
                    "url": safe_url,
                }

        result = await _download_file_impl(safe_url, dest_path, file_path, timeout)
        if isinstance(result, dict):
            result.update(download_boundary_metadata(
                original_url=safe_url,
                final_url=str(result.get("url") or safe_url),
                filename=final_filename,
                executable=executable,
            ))
        return result

    except (NetworkBoundaryError, PathSecurityError, FileBoundaryError) as e:
        return {"ok": False, "operation": "download_file", "error_type": "security", "message": str(e), "url": url}
    except Exception as e:
        return {"ok": False, "operation": "download_file", "error_type": "prepare_error", "message": str(e), "url": url}


def _prepare_download_target(
    url: str,
    destination_folder: str,
    filename: str,
    security_service: SecurityService,
) -> tuple[Path, Path, str]:
    dest_path = _download_dir(destination_folder)

    if not filename:
        parsed_url = urlparse(url)
        filename = Path(parsed_url.path).name
        if not filename or '.' not in filename:
            hash_obj = hashlib.md5(url.encode())
            filename = f"download_{hash_obj.hexdigest()[:8]}"
    filename = _safe_filename(filename)

    dest_path = Path(security_service.check_path(str(dest_path), mode="write"))
    file_path = _dedupe_path(dest_path / filename)
    security_service.check_path(str(file_path), mode="write")
    return dest_path, file_path, filename


async def _download_file_impl(url: str, dest_path: Path, file_path: Path, timeout: int) -> dict:
    try:
        dest_path.mkdir(parents=True, exist_ok=True)

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                    if response.status != 200:
                        return {
                            "ok": False,
                            "operation": "download_file",
                            "error_type": "http_error",
                            "status": response.status,
                            "reason": response.reason,
                            "url": url,
                        }

                    total_size = response.headers.get('Content-Length')
                    if total_size:
                        total_size = int(total_size)
                    else:
                        total_size = 0

                    if total_size and total_size > MAX_DOWNLOAD_BYTES:
                        return {
                            "ok": False,
                            "operation": "download_file",
                            "error_type": "download_too_large",
                            "max_bytes": MAX_DOWNLOAD_BYTES,
                            "content_length": total_size,
                            "url": url,
                        }

                    downloaded_size = 0
                    chunk_size = 8192

                    try:
                        with open(file_path, 'wb') as f:
                            async for chunk in response.content.iter_chunked(chunk_size):
                                downloaded_size += len(chunk)
                                if downloaded_size > MAX_DOWNLOAD_BYTES:
                                    raise ValueError(
                                        f"download exceeds PAWMATE_MAX_DOWNLOAD_BYTES={MAX_DOWNLOAD_BYTES}"
                                    )
                                f.write(chunk)

                                if total_size > 0:
                                    progress = (downloaded_size / total_size) * 100
                                    logger.debug(
                                        "download progress: %.1f%% (%s/%s bytes)",
                                        progress,
                                        downloaded_size,
                                        total_size,
                                    )
                    except ValueError as exc:
                        try:
                            file_path.unlink(missing_ok=True)
                        except Exception:
                            logger.warning("failed to remove oversized partial download: %s", file_path)
                        return {
                            "ok": False,
                            "operation": "download_file",
                            "error_type": "download_too_large",
                            "max_bytes": MAX_DOWNLOAD_BYTES,
                            "downloaded_bytes": downloaded_size,
                            "message": str(exc),
                            "url": url,
                        }

                    exists = file_path.exists()
                    size = file_path.stat().st_size if exists else 0
                    return {
                        "ok": exists and size == downloaded_size,
                        "operation": "download_file",
                        "path": str(file_path),
                        "filename": file_path.name,
                        "size_bytes": size,
                        "url": str(response.url),
                        "postcondition": {
                            "file_exists": exists,
                            "expected_bytes": downloaded_size,
                            "actual_bytes": size,
                        },
                    }

            except asyncio.TimeoutError:
                return {"ok": False, "operation": "download_file", "error_type": "timeout", "timeout": timeout, "url": url}
            except Exception as e:
                return {"ok": False, "operation": "download_file", "error_type": "download_error", "message": str(e), "url": url}

    except Exception as e:
        return {"ok": False, "operation": "download_file", "error_type": "prepare_error", "message": str(e), "url": url}


async def download_with_metadata(
    url: str,
    destination_folder: str = "",
    filename: str = "",
    include_metadata: bool = True,
    timeout: int = 60,
    security_service: Optional[SecurityService] = None,
    already_confirmed: bool = False,
) -> dict:
    """
    下载文件并保存元数据。

    Args:
        url: 下载链接
        destination_folder: 目标文件夹
        filename: 保存的文件名
        include_metadata: 是否保存元数据
        timeout: 超时时间

    Returns:
        下载结果信息
    """
    try:
        service = security_service or SecurityService()
        safe_url = validate_external_http_url(url)
        dl_dest_path, dl_file_path, final_filename = _prepare_download_target(
            safe_url,
            destination_folder,
            filename,
            service,
        )
        metadata_target = dl_file_path.with_suffix(dl_file_path.suffix + ".meta.json")
        executable = is_executable_download(dl_file_path)

        if executable and security_service is None and not already_confirmed:
            return {
                "ok": False,
                "operation": "download_with_metadata",
                "error_type": "approval_unavailable",
                "message": "executable downloads require confirmation",
                "url": safe_url,
                "filename": final_filename,
            }

        if security_service is not None and not already_confirmed:
            allowed = await security_service.require_confirmation(
                "download_with_metadata",
                {
                    "url": safe_url,
                    "destination_folder": destination_folder,
                    "filename": final_filename,
                    "dest": str(dl_file_path),
                    "metadata_path": str(metadata_target),
                    "include_metadata": include_metadata,
                    "timeout": timeout,
                    "executable": executable,
                },
                reason="download_with_metadata writes a network response and metadata to disk",
            )
            if not allowed:
                return {
                    "ok": False,
                    "operation": "download_with_metadata",
                    "error_type": "approval_denied",
                    "message": "download_with_metadata was not approved",
                    "url": safe_url,
                }

        # 确保目标文件夹存在
        dl_dest_path.mkdir(parents=True, exist_ok=True)

        # 首先获取文件信息
        async with aiohttp.ClientSession() as session:
            # HEAD 请求获取文件信息
            try:
                async with session.head(safe_url, timeout=aiohttp.ClientTimeout(total=10)) as head_resp:
                    content_type = head_resp.headers.get('Content-Type', 'unknown')
                    content_length = head_resp.headers.get('Content-Length', 'unknown')
                    final_url = str(head_resp.url)  # 重定向后的URL
            except:
                # 如果HEAD请求失败，稍后在GET请求中获取信息
                content_type = 'unknown'
                content_length = 'unknown'
                final_url = safe_url

        # 执行下载
        result = await _download_file_impl(safe_url, dl_dest_path, dl_file_path, timeout)
        if isinstance(result, dict):
            result.update(download_boundary_metadata(
                original_url=safe_url,
                final_url=str(result.get("url") or final_url or safe_url),
                filename=final_filename,
                executable=executable,
            ))

        if include_metadata and isinstance(result, dict) and result.get("ok"):
            # 创建元数据文件
            downloaded_path = Path(str(result.get("path", "")))
            metadata_file = downloaded_path.with_suffix(downloaded_path.suffix + ".meta.json")
            metadata = {
                "url": final_url,
                "original_url": safe_url,
                "content_type": content_type,
                "content_length": content_length,
                "download_time": datetime.now().isoformat(timespec="seconds"),
                "result": result
            }

            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            result["metadata_path"] = str(metadata_file)

        return result

    except (NetworkBoundaryError, PathSecurityError, FileBoundaryError) as e:
        return {"ok": False, "operation": "download_with_metadata", "error_type": "security", "message": str(e), "url": url}
    except Exception as e:
        return {"ok": False, "operation": "download_with_metadata", "error_type": "metadata_error", "message": str(e), "url": url}


def register_download_tools(
    registry: ToolRegistry,
    security_service: SecurityService | None = None,
) -> None:
    """Register download tools."""
    service = security_service or SecurityService()

    async def confirmed_download_file(
        url: str,
        destination_folder: str = "",
        filename: str = "",
        timeout: int = 60,
    ) -> dict:
        return await download_file(
            url,
            destination_folder,
            filename,
            timeout,
            security_service=service,
            already_confirmed=True,
        )

    async def confirmed_download_with_metadata(
        url: str,
        destination_folder: str = "",
        filename: str = "",
        include_metadata: bool = True,
        timeout: int = 60,
    ) -> dict:
        return await download_with_metadata(
            url,
            destination_folder,
            filename,
            include_metadata,
            timeout,
            security_service=service,
            already_confirmed=True,
        )

    defs = [
        ToolDef(
            name="download_file",
            description="从网络下载文件到本地，支持多种文件类型和进度监控",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要下载的文件URL"},
                    "destination_folder": {"type": "string", "default": "", "description": "保存到的本地文件夹；留空则保存到 PawMate data/downloads"},
                    "filename": {"type": "string", "default": "", "description": "保存的文件名，留空则从URL推断"},
                    "timeout": {"type": "integer", "minimum": 10, "maximum": 600, "default": 60, "description": "下载超时时间（秒）"}
                },
                "required": ["url"]
            },
            handler=confirmed_download_file,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.NETWORK,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
        ToolDef(
            name="download_with_metadata",
            description="下载文件并保存元数据信息，用于后续分析和追踪",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要下载的文件URL"},
                    "destination_folder": {"type": "string", "default": "", "description": "保存到的本地文件夹；留空则保存到 PawMate data/downloads"},
                    "filename": {"type": "string", "default": "", "description": "保存的文件名，留空则从URL推断"},
                    "include_metadata": {"type": "boolean", "default": True, "description": "是否保存元数据文件"},
                    "timeout": {"type": "integer", "minimum": 10, "maximum": 600, "default": 60, "description": "下载超时时间（秒）"}
                },
                "required": ["url"]
            },
            handler=confirmed_download_with_metadata,
            approval=APPROVAL_CONFIRM,
            category=ToolCategory.NETWORK,
            risk=RiskLevel.HIGH,
            side_effect=SideEffectLevel.LOCAL_WRITE,
        ),
    ]

    for tool_def in defs:
        registry.register(tool_def)
