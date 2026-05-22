from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from memory_service.config import Settings, get_settings


def get_pool(request: Request) -> asyncpg.Pool:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db pool not ready")
    return pool


# auto_error=False so we can produce our own 401 with a consistent body shape and
# only when a token is actually configured.
_bearer = HTTPBearer(auto_error=False)


def require_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> None:
    """Auth dependency. Whitelisted endpoints (e.g. /health) skip this by not depending on it.

    If MEMORY_AUTH_TOKEN is empty the dependency passes unconditionally.
    """
    expected = settings.auth_token
    if not expected:
        return
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


PoolDep = Annotated[asyncpg.Pool, Depends(get_pool)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AuthDep = Annotated[None, Depends(require_auth)]
