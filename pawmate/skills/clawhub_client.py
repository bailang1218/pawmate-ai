"""
ClawHub API client — search, detail, file, download for skills marketplace.
Error classification: all failures carry a machine-readable code and details dict.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_logger = logging.getLogger("pawmate")

MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
DEFAULT_TIMEOUT = 60  # ClawHub is a remote service; generous timeout avoids premature failure
MAX_RETRIES = 2       # Retry transient SSL/network errors
RETRY_DELAY = 0.8     # Seconds between retries


class ClawHubError(Exception):
    """Base error for ClawHub API failures.

    Attributes:
        code:     Machine-readable error code (see ERROR_CODES).
        message:  Human-readable description.
        details:  Dict with structured diagnostic info (url, status, body_prefix, etc.)
        detail:   Legacy string field — same as str(details) for backward compat.
    """

    ERROR_CODES = {
        "clawhub_timeout",
        "clawhub_dns_error",
        "clawhub_ssl_error",
        "clawhub_network_error",
        "clawhub_http_401",
        "clawhub_http_403",
        "clawhub_http_404",
        "clawhub_http_429",
        "clawhub_http_error",
        "clawhub_invalid_json",
        "clawhub_unexpected_schema",
        "clawhub_endpoint_unavailable",
        "clawhub_no_version",
        "clawhub_too_large",
        "clawhub_unknown",
    }

    def __init__(
        self,
        message: str,
        code: str = "clawhub_unknown",
        details: Optional[Dict[str, Any]] = None,
    ):
        self.code = code if code in self.ERROR_CODES else "clawhub_unknown"
        self.details = details or {}
        # Legacy compat: detail is a str summary of the details dict
        self.detail = json.dumps(self.details, ensure_ascii=False) if self.details else message
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dict for WebBridge transmission."""
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


class ClawHubClient:
    """Client for the ClawHub skill marketplace API."""

    def __init__(
        self,
        api_base: str = "https://clawhub.ai",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._headers = {"User-Agent": "PawMate/2.1"}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search_skills(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Search ClawHub for skills matching *query*.

        Uses /api/v1/search?q=<query>. Returns up to *limit* items.
        """
        resp = self._get("/api/v1/search", params={"q": query})
        items = resp.get("results") or []
        if isinstance(items, list):
            return items[:limit]
        return []

    def list_skills(
        self, limit: int = 20, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """Browse skills with optional cursor for pagination.

        Uses /api/v1/skills?limit=N&cursor=... which returns a public catalog.
        """
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get("/api/v1/skills", params=params)

    def get_skill_detail(self, slug: str) -> Dict[str, Any]:
        """Get detailed information about a specific skill."""
        return self._get(f"/api/v1/skills/{slug}")

    def get_skill_file(self, slug: str, path: str = "SKILL.md") -> str:
        """Fetch a file from a skill repository (returns raw text)."""
        return self._get_raw(
            f"/api/v1/skills/{slug}/file", params={"path": path}
        )

    def download_skill_zip(
        self, slug: str, version: Optional[str] = None
    ) -> bytes:
        """Download the skill as a ZIP archive.

        If version is None, attempts to discover it from detail endpoint.
        """
        if not version:
            detail = self.get_skill_detail(slug)
            lv = detail.get("latestVersion") or {}
            version = lv.get("version")
            if not version:
                raise ClawHubError(
                    f"Cannot determine version for {slug}",
                    code="clawhub_no_version",
                    details={"slug": slug},
                )
        return self._get_binary(
            "/api/v1/download",
            params={"slug": slug, "version": version},
        )

    # ------------------------------------------------------------------
    # Connection diagnosis (lightweight, no downloads)
    # ------------------------------------------------------------------

    def diagnose(self) -> Dict[str, Any]:
        """Run lightweight diagnostics against known ClawHub endpoints.

        Returns a dict with checked_at, endpoints[], and recommendation.
        Does NOT download or install anything.
        """
        results: List[Dict[str, Any]] = []
        endpoints = [
            ("skills", "/api/v1/skills", None),
            ("skills_limit", "/api/v1/skills", {"limit": 1}),
            ("search_star", "/api/v1/search", {"q": "*"}),
            ("search_browser", "/api/v1/search", {"q": "browser"}),
        ]
        for name, path, params in endpoints:
            entry: Dict[str, Any] = {"name": name, "url": self._build_url(path, params)}
            t0 = time.time()
            try:
                req = Request(entry["url"], headers=self._headers)
                with urlopen(req, timeout=10) as resp:
                    elapsed = time.time() - t0
                    entry["ok"] = True
                    entry["status"] = resp.status
                    entry["elapsed_ms"] = round(elapsed * 1000)
                    entry["content_type"] = resp.headers.get("content-type")
            except HTTPError as e:
                elapsed = time.time() - t0
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                entry["ok"] = False
                entry["status"] = e.code
                entry["elapsed_ms"] = round(elapsed * 1000)
                entry["error_code"] = f"clawhub_http_{e.code}"
                entry["body_prefix"] = body
            except URLError as e:
                elapsed = time.time() - t0
                reason_str = str(e.reason)
                err_code = _classify_url_error(e)
                entry["ok"] = False
                entry["elapsed_ms"] = round(elapsed * 1000)
                entry["error_code"] = err_code
                entry["reason"] = reason_str
            except Exception as e:
                elapsed = time.time() - t0
                entry["ok"] = False
                entry["elapsed_ms"] = round(elapsed * 1000)
                entry["error_code"] = "clawhub_unknown"
                entry["reason"] = repr(e)[:200]
            results.append(entry)

        # Determine recommendation
        ok_endpoints = [r for r in results if r.get("ok")]
        recommendation = "Unable to reach ClawHub"
        for r in ok_endpoints:
            if r["name"] in ("skills", "skills_limit"):
                recommendation = "Use /api/v1/skills for browse"
                break
            if r["name"] == "search_browser":
                recommendation = "Use search endpoint for browse"
                break

        return {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "endpoints": results,
            "recommendation": recommendation,
            "api_base": self._api_base,
            "timeout": self._timeout,
        }

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _build_url(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> str:
        url = f"{self._api_base}{path}"
        if params:
            import urllib.parse

            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            if qs:
                url = f"{url}?{qs}"
        return url

    def _classify_error(
        self, e: Exception, url: str, elapsed: float
    ) -> ClawHubError:
        """Classify any exception into a ClawHubError with proper code/details."""
        details: Dict[str, Any] = {
            "url": url,
            "timeout": self._timeout,
            "elapsed": round(elapsed, 3),
        }

        if isinstance(e, HTTPError):
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            code = e.code
            details["status"] = code
            details["body_prefix"] = body
            rh = {k.lower(): v for k, v in e.headers.items()}
            if "retry-after" in rh:
                details["retry_after"] = rh["retry-after"]
            if "x-ratelimit-remaining" in rh:
                details["rate_remaining"] = rh["x-ratelimit-remaining"]

            if code == 401:
                return ClawHubError(
                    "ClawHub 拒绝访问，需要认证 (HTTP 401)",
                    code="clawhub_http_401",
                    details=details,
                )
            elif code == 403:
                return ClawHubError(
                    "ClawHub 拒绝访问，权限不足 (HTTP 403)",
                    code="clawhub_http_403",
                    details=details,
                )
            elif code == 404:
                return ClawHubError(
                    f"ClawHub endpoint 或资源不存在 (HTTP 404)",
                    code="clawhub_http_404",
                    details=details,
                )
            elif code == 429:
                retry = details.get("retry_after", "?")
                return ClawHubError(
                    f"ClawHub 请求限流 (HTTP 429)，建议 {retry}s 后重试",
                    code="clawhub_http_429",
                    details=details,
                )
            else:
                return ClawHubError(
                    f"ClawHub HTTP {code} for {url}",
                    code="clawhub_http_error",
                    details={**details, "status": code},
                )

        if isinstance(e, URLError):
            reason_str = str(e.reason)
            details["reason"] = reason_str
            err_code = _classify_url_error(e)
            # Check for timeout
            r_lower = reason_str.lower()
            if "timed out" in r_lower or "timeout" in r_lower:
                details["stage"] = "read" if "read" in r_lower else "connect"
                return ClawHubError(
                    f"连接 ClawHub 超时 ({details['stage']})",
                    code="clawhub_timeout",
                    details=details,
                )
            if err_code == "clawhub_dns_error":
                return ClawHubError(
                    f"ClawHub DNS 解析失败: {reason_str}",
                    code="clawhub_dns_error",
                    details=details,
                )
            if err_code == "clawhub_ssl_error":
                return ClawHubError(
                    f"ClawHub SSL/TLS 错误: {reason_str}",
                    code="clawhub_ssl_error",
                    details=details,
                )
            return ClawHubError(
                f"连接 ClawHub 网络错误: {reason_str}",
                code="clawhub_network_error",
                details=details,
            )

        if isinstance(e, json.JSONDecodeError):
            return ClawHubError(
                f"ClawHub 返回的不是有效 JSON",
                code="clawhub_invalid_json",
                details={**details, "parse_error": str(e)[:200]},
            )

        # Generic/fallback
        return ClawHubError(
            f"ClawHub 请求失败: {type(e).__name__}: {e}",
            code="clawhub_unknown",
            details={**details, "exception": repr(e)[:300]},
        )

    # ------------------------------------------------------------------
    # Retry helpers for transient SSL/network errors
    # ------------------------------------------------------------------

    @staticmethod
    def _is_transient(e: Exception) -> bool:
        """Check if an exception is a transient SSL/network error worth retrying.

        Returns False for HTTP status errors (4xx), JSON parse errors,
        and definitive failures.
        """
        if isinstance(e, HTTPError):
            return False  # Server responded — don't retry 4xx/5xx
        if isinstance(e, json.JSONDecodeError):
            return False  # Won't change on retry
        if isinstance(e, ClawHubError):
            return False  # Already classified — don't re-retry
        if isinstance(e, URLError):
            reason_str = str(e.reason).lower()
            # SSL errors, connection resets, DNS flakes: retry
            if "unexpected_eof" in reason_str:
                return True
            if "ssl" in reason_str:
                return True
            if "connection reset" in reason_str or "connection refused" in reason_str:
                return True
            if "timed out" in reason_str or "timeout" in reason_str:
                return True
            if "temporary failure in name resolution" in reason_str:
                return True
            return False
        if isinstance(e, OSError):
            # Socket errors, SSL module errors
            reason = str(e).lower()
            if "ssl" in reason or "eof" in reason or "connection" in reason:
                return True
            if "timed out" in reason:
                return True
            return False
        return False

    def _urlopen_with_retry(self, url: str) -> Any:
        """Open a URL with automatic retry for transient errors.

        Returns the response context manager.
        Raises ClawHubError if all retries fail.
        """
        import time as _time
        last_error: Optional[Exception] = None
        t0 = _time.time()

        for attempt in range(1 + MAX_RETRIES):
            try:
                req = Request(url, headers=self._headers)
                return urlopen(req, timeout=self._timeout)
            except (HTTPError, URLError, OSError) as e:
                last_error = e
                elapsed = _time.time() - t0
                if not self._is_transient(e) or attempt >= MAX_RETRIES:
                    raise
                _logger.info(
                    "[ClawHub] Retry %d/%d after %.2fs: %s",
                    attempt + 1, MAX_RETRIES, elapsed, str(e)[:80],
                )
                _time.sleep(RETRY_DELAY)

        # All retries exhausted — re-raise the last error
        if last_error is not None:
            raise last_error
        # Shouldn't reach here, but satisfy type checker
        raise ClawHubError("Max retries exceeded", code="clawhub_unknown")

    def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        url = self._build_url(path, params)
        _logger.debug("[ClawHub] GET %s", url)
        t0 = time.time()
        try:
            with self._urlopen_with_retry(url) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text)
        except (HTTPError, URLError, json.JSONDecodeError, OSError) as e:
            elapsed = time.time() - t0
            raise self._classify_error(e, url, elapsed) from e

    def _get_raw(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> str:
        url = self._build_url(path, params)
        _logger.debug("[ClawHub] GET RAW %s", url)
        t0 = time.time()
        try:
            with self._urlopen_with_retry(url) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, OSError) as e:
            elapsed = time.time() - t0
            raise self._classify_error(e, url, elapsed) from e

    def _get_binary(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> bytes:
        url = self._build_url(path, params)
        _logger.debug("[ClawHub] GET BINARY %s", url)
        t0 = time.time()
        try:
            with self._urlopen_with_retry(url) as resp:
                data = resp.read()
                if len(data) > MAX_DOWNLOAD_SIZE:
                    raise ClawHubError(
                        f"下载太大 ({len(data)} bytes, 上限 {MAX_DOWNLOAD_SIZE})",
                        code="clawhub_too_large",
                        details={"url": url, "size": len(data), "max": MAX_DOWNLOAD_SIZE},
                    )
                return data
        except (HTTPError, URLError, OSError) as e:
            elapsed = time.time() - t0
            raise self._classify_error(e, url, elapsed) from e


# --------------------------------------------------------------------------
# Module-level helpers
# --------------------------------------------------------------------------


def _classify_url_error(e: URLError) -> str:
    """Classify a URLError into a specific error code."""
    reason = e.reason
    reason_str = str(reason).lower()

    # Timeouts
    if isinstance(reason, TimeoutError) or "timed out" in reason_str:
        return "clawhub_timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "clawhub_network_error"
    if isinstance(reason, ConnectionResetError):
        return "clawhub_network_error"

    # Socket-level classification
    reason_s = str(reason)
    if "getaddrinfo" in reason_s or "Name or service not known" in reason_s:
        return "clawhub_dns_error"
    if "Temporary failure in name resolution" in reason_s:
        return "clawhub_dns_error"
    if "certificate" in reason_s.lower() or "ssl" in reason_s.lower():
        return "clawhub_ssl_error"
    if "connection refused" in reason_s.lower():
        return "clawhub_network_error"
    if "connection reset" in reason_s.lower():
        return "clawhub_network_error"
    if "no route to host" in reason_s.lower():
        return "clawhub_network_error"
    if "network is unreachable" in reason_s.lower():
        return "clawhub_network_error"

    return "clawhub_network_error"
