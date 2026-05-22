"""HTTP middleware.

PayloadSizeLimitMiddleware enforces `Settings.max_payload_bytes` before the
body is parsed by Pydantic. Without this, an attacker could send a multi-megabyte
JSON body that Pydantic would happily parse (since per-field length caps don't
bound the overall body), exhausting memory.

The middleware checks Content-Length first (fast path) and then guards against
chunked or unreliable Content-Length by buffering up to the limit and rejecting
once the limit is exceeded. Buffered body is fed back to downstream handlers
via a wrapped `receive` callable.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class PayloadSizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Callable):
        # Skip GET/DELETE which shouldn't carry bodies anyway.
        if request.method not in ("POST", "PUT", "PATCH"):
            return await call_next(request)

        # Fast path: trusted Content-Length.
        cl_header = request.headers.get("content-length")
        if cl_header is not None:
            try:
                cl = int(cl_header)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_content_length"},
                )
            if cl > self._max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "payload_too_large",
                        "limit_bytes": self._max_bytes,
                    },
                )

        # Slow path: buffer the body up to the limit. If exceeded → 413.
        # Re-feed the buffered body via a wrapped `receive` so downstream
        # handlers see the same bytes.
        body = bytearray()
        more_body = True
        while more_body:
            message = await request.receive()
            chunk = message.get("body", b"") or b""
            body.extend(chunk)
            if len(body) > self._max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "payload_too_large",
                        "limit_bytes": self._max_bytes,
                    },
                )
            more_body = message.get("more_body", False)

        async def _replay():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        request._receive = _replay   # type: ignore[attr-defined]
        return await call_next(request)
