"""Shared HTTP client defaults for OpenAI-compatible streaming providers."""
from __future__ import annotations

from typing import Mapping

import httpx


MIN_STREAM_READ_TIMEOUT_SECONDS = 120.0


def make_streaming_client(
    *,
    base_url: str,
    api_key: str,
    headers: Mapping[str, str] | None = None,
    read_timeout: float = 180.0,
    max_connections: int = 4,
) -> httpx.AsyncClient:
    """Create an AsyncClient tuned for long-lived SSE responses.

    The model APIs used by PawMate are mostly stateless. Avoiding keep-alive
    reuse is a little less efficient, but it prevents stale pooled connections
    from surfacing as ``RemoteProtocolError: Server disconnected without
    sending a response`` on later turns.
    """
    merged_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Connection": "close",
    }
    if headers:
        merged_headers.update(headers)

    effective_read_timeout = max(MIN_STREAM_READ_TIMEOUT_SECONDS, float(read_timeout))

    return httpx.AsyncClient(
        base_url=str(base_url).rstrip("/") + "/",
        headers=merged_headers,
        timeout=httpx.Timeout(connect=10.0, read=effective_read_timeout, write=30.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=0,
            keepalive_expiry=0.0,
        ),
        http2=False,
    )
