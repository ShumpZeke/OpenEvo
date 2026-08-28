from __future__ import annotations

import logging
import socket
import time
from collections.abc import Mapping

import httpx2

_LOGGER = logging.getLogger(__name__)
_LIMITS = httpx2.Limits(
    max_connections=200,
    max_keepalive_connections=40,
    keepalive_expiry=30.0,
)
_SOCKET_OPTIONS: list[tuple[int, int, int]] = [
    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
]


def _request_started(request: httpx2.Request) -> None:
    request.extensions["request_start"] = time.perf_counter()


def _request_completed(response: httpx2.Response) -> None:
    started = response.request.extensions.get("request_start")
    elapsed = time.perf_counter() - started if isinstance(started, float) else 0.0
    _LOGGER.info(
        "HTTP %s %s %d %.3fs %s",
        response.request.method,
        response.request.url,
        response.status_code,
        elapsed,
        response.http_version,
    )


def create_http_client(
    base_url: str,
    timeout_s: float,
    retries: int,
    headers: Mapping[str, str],
) -> httpx2.Client:
    timeout = httpx2.Timeout(
        connect=min(10.0, timeout_s),
        read=timeout_s,
        write=min(10.0, timeout_s),
        pool=min(10.0, timeout_s),
    )
    transport = httpx2.HTTPTransport(
        http2=True,
        retries=retries,
        limits=_LIMITS,
        socket_options=_SOCKET_OPTIONS,
    )
    return httpx2.Client(
        transport=transport,
        timeout=timeout,
        base_url=f"{base_url.rstrip('/')}/",
        headers=dict(headers),
        event_hooks={
            "request": [_request_started],
            "response": [_request_completed],
        },
        follow_redirects=True,
    )
