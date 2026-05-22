"""API key authentication for protected endpoints."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from config import get_settings
from logger_config import log_auth_failure


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


async def verify_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """Validate X-API-Key header against BRIDGE_API_KEY from .env."""
    settings = get_settings()
    client_ip = _client_ip(request)
    path = request.url.path

    if not x_api_key:
        log_auth_failure("missing_api_key", client_ip, path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    if not secrets.compare_digest(x_api_key, settings.bridge_api_key):
        log_auth_failure("invalid_api_key", client_ip, path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return x_api_key
