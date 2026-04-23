"""
On-LAN ISAPI forward: Digest auth to device host/port. Any /ISAPI/ path and method.
Called from the RabbitMQ consumer (no HTTP gateway URL).
"""

from __future__ import annotations

import logging
from typing import Optional

import requests
from requests.auth import HTTPDigestAuth

logger = logging.getLogger("hikvision-lan-agent.isapi")


def forward_isapi_to_device(
    device_host: str,
    device_port: int,
    method: str,
    path: str,
    body: Optional[bytes],
    username: str,
    password: str,
    insecure_skip_verify: bool,
    timeout: float,
) -> tuple[int, bytes, Optional[str]]:
    """
    Returns (status_code, response_body, response_content_type).
    """
    method = (method or "GET").upper().strip()
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/ISAPI/"):
        raise ValueError("path must start with /ISAPI/")
    scheme = "https"
    if device_port == 80:
        scheme = "http"
    url = f"{scheme}://{device_host}:{device_port}{path}"

    auth = None
    if username:
        auth = HTTPDigestAuth(username, password)

    verify_tls = not insecure_skip_verify
    timeout_tuple = (max(3.0, min(timeout, 180.0)), max(3.0, min(timeout, 180.0)))

    logger.info("ISAPI %s %s (user=%s)", method, url, username or "-")

    headers: dict[str, str] = {}
    if body:
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

    resp = requests.request(
        method=method,
        url=url,
        data=body if body else None,
        headers=headers or None,
        auth=auth,
        verify=verify_tls,
        timeout=timeout_tuple,
    )
    ct = resp.headers.get("Content-Type")
    return resp.status_code, resp.content, ct
