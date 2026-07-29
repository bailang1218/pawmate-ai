"""Network and download boundary checks for tool handlers."""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse


class NetworkBoundaryError(Exception):
    """Raised when a URL or download target violates network policy."""


EXECUTABLE_SUFFIXES = {
    ".apk",
    ".app",
    ".bat",
    ".cmd",
    ".com",
    ".deb",
    ".dll",
    ".dmg",
    ".exe",
    ".jar",
    ".js",
    ".msi",
    ".pkg",
    ".ps1",
    ".rpm",
    ".scr",
    ".sh",
    ".vbs",
}


def validate_external_http_url(url: str) -> str:
    target = str(url or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"}:
        raise NetworkBoundaryError("only http and https URLs are allowed")
    if not parsed.hostname:
        raise NetworkBoundaryError("URL hostname is required")

    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        raise NetworkBoundaryError("localhost URLs are not allowed for model network tools")

    def _reject_internal_ip(ip_text: str) -> None:
        ip = ipaddress.ip_address(ip_text)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise NetworkBoundaryError("internal IP URLs are not allowed for model network tools")

    try:
        _reject_internal_ip(host)
        return target
    except ValueError:
        pass

    try:
        resolved = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise NetworkBoundaryError(f"could not resolve URL hostname: {host}") from exc

    for item in resolved:
        sockaddr = item[4]
        if not sockaddr:
            continue
        _reject_internal_ip(str(sockaddr[0]))
    return target


def is_executable_download(filename_or_path: str | Path) -> bool:
    return Path(str(filename_or_path)).suffix.lower() in EXECUTABLE_SUFFIXES


def download_boundary_metadata(
    *,
    original_url: str,
    final_url: str = "",
    filename: str = "",
    executable: bool = False,
) -> dict:
    return {
        "network_boundary": {
            "source_url": original_url,
            "final_url": final_url or original_url,
            "filename": filename,
            "executable": executable,
            "untrusted_observation": True,
        }
    }
