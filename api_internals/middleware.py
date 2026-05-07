"""Middleware + exception-handler installation for the FastAPI app.

Extracted from `api.py::create_app` by PR-H2. The middleware order
matters (FastAPI runs them outermost-first, which is the REVERSE of
`add_middleware` call order — last added wins outermost), so the
sequence here mirrors what `create_app` had verbatim.

This is a pure-function extraction: no closure variables captured,
no behavior change. The only reason it lived inside create_app was
historical.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from security import (
    AuthMiddleware,
    RateLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    get_cors_origins,
    redacted_exception_handler,
)


def install_middleware(app: FastAPI) -> None:
    """Wire every middleware + the redacting exception handler.

    Order (FastAPI runs outermost-first, which is reverse of the
    add_middleware call order): RequestId -> SecurityHeaders ->
    RateLimit -> Auth -> CORS -> GZip -> app.
    """
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # I046
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "x-api-key", "content-type", "x-request-id"],
    )
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)  # I056
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(Exception, redacted_exception_handler)
