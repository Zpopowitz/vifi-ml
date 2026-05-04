"""Security primitives for the ViFi API.

Centralizes everything that touches authentication, authorization,
CORS, rate limiting, and error redaction so the surface that needs
review for HIPAA / FDA cybersecurity premarket submission is a single
file.

Settings -- all driven by environment variables so secrets never live
in code or config files committed to git:

    VIFI_AUTH_MODE          : "none" (dev only) | "api_key" (production)
                              default: "none"
    VIFI_API_KEYS           : comma-separated allowed API keys
                              required when AUTH_MODE=api_key
    VIFI_CORS_ORIGINS       : comma-separated allowed origins
                              default: "" (no cross-origin requests)
    VIFI_RATE_LIMIT         : "<count>/<period>" e.g. "60/minute"
                              default: "60/minute"
    VIFI_REVEAL_ERRORS      : "true" exposes 5xx detail (dev only).
                              default: "false" (prod -- generic 500s)

Public endpoints (NO auth required, intentionally):
    GET  /health    -- liveness for orchestrator probes
    GET  /roadmap   -- product surface metadata, no PHI
    GET  /          -- root, returns a static banner

Every other endpoint requires a valid API key when AUTH_MODE=api_key.

Why this is implemented in middleware (not per-route): every new
endpoint added by mistake should be auth-required by default. Opt-in
public endpoints are listed explicitly here. Fail-closed.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from enum import Enum
from typing import Iterable, Optional

from fastapi import HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("vifi.security")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class AuthMode(str, Enum):
    NONE = "none"
    API_KEY = "api_key"


# Endpoints reachable without an API key. Probes (/health) and product
# metadata (/roadmap) only. Anything that touches PHI -- /predict*,
# /identify, /api/v1/stream -- is NOT here.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/", "/health", "/roadmap",
    "/api/v1/", "/api/v1/health", "/api/v1/roadmap",
    "/docs", "/redoc", "/openapi.json",
})


def get_auth_mode() -> AuthMode:
    raw = os.environ.get("VIFI_AUTH_MODE", "none").lower()
    try:
        return AuthMode(raw)
    except ValueError:
        log.error("VIFI_AUTH_MODE=%r is not valid; falling back to api_key "
                  "(fail-closed). Valid: none, api_key", raw)
        return AuthMode.API_KEY


def get_api_keys() -> set[str]:
    """Allowed API keys (comma-separated in VIFI_API_KEYS).

    Empty set in api_key mode means *no one* can call protected
    endpoints -- fail-closed by design.
    """
    raw = os.environ.get("VIFI_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def get_cors_origins() -> list[str]:
    """Allowed CORS origins. Empty list disables cross-origin requests
    entirely (the dashboard runs on its own host inside the same compose
    network so this is safe by default)."""
    raw = os.environ.get("VIFI_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def get_rate_limit() -> str:
    return os.environ.get("VIFI_RATE_LIMIT", "60/minute")


def reveal_errors() -> bool:
    return os.environ.get("VIFI_REVEAL_ERRORS", "false").lower() == "true"


def generate_api_key() -> str:
    """Generate a cryptographically random API key. Use for new keys:

        python -c "from security import generate_api_key; print(generate_api_key())"
    """
    return f"vifi_{secrets.token_urlsafe(32)}"


# ---------------------------------------------------------------------------
# API key extraction + validation
# ---------------------------------------------------------------------------

def _extract_key(headers, query_params) -> Optional[str]:
    """Pull an API key from the request, in this priority order:

      1. Authorization: Bearer <key>     (preferred -- standard)
      2. X-API-Key: <key>                (convenience)
      3. ?api_key=<key>                  (WebSocket fallback only;
                                           browsers can't set headers
                                           when opening a WebSocket)

    Returns None if no key is present.
    """
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip() or None
    x_api_key = headers.get("x-api-key") or headers.get("X-API-Key")
    if x_api_key:
        return x_api_key.strip() or None
    qp_key = query_params.get("api_key") if query_params else None
    return qp_key.strip() if qp_key else None


def _key_is_valid(key: Optional[str], allowed: Iterable[str]) -> bool:
    """Constant-time check against the allowed set so an attacker can't
    time how much of the key matched."""
    if not key:
        return False
    for valid in allowed:
        if secrets.compare_digest(key, valid):
            return True
    return False


def require_api_key(request: Request) -> None:
    """Dependency: raises 401 if the request is missing/invalid auth.

    Called explicitly from endpoint dependencies. The middleware below
    (`AuthMiddleware`) is the global enforcement point; this helper is
    available for places (like WebSocket handlers) that can't use the
    middleware directly.
    """
    mode = get_auth_mode()
    if mode == AuthMode.NONE:
        return
    keys = get_api_keys()
    if not keys:
        # Fail-closed: api_key mode with no keys configured = nobody allowed.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "auth_misconfigured")
    key = _extract_key(request.headers, request.query_params)
    if not _key_is_valid(key, keys):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid_or_missing_api_key")


async def authorize_websocket(websocket: WebSocket) -> bool:
    """WebSocket-friendly auth check. Returns True if request is allowed.

    Closes the socket with code 1008 (policy violation) and returns False
    when auth fails. WebSockets must be closed cleanly; we don't raise.
    """
    mode = get_auth_mode()
    if mode == AuthMode.NONE:
        return True
    keys = get_api_keys()
    if not keys:
        await websocket.close(code=1011, reason="auth_misconfigured")
        return False
    key = _extract_key(websocket.headers, websocket.query_params)
    if not _key_is_valid(key, keys):
        await websocket.close(code=1008, reason="invalid_or_missing_api_key")
        return False
    return True


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    """Global API-key gate.

    Protects every HTTP route except the explicitly-public paths. Always
    fails closed: misconfiguration => 503, missing key => 401, unknown
    key => 401. Constant-time key comparison prevents timing oracles.

    WebSockets bypass HTTP middleware in Starlette/FastAPI; the
    `/api/v1/stream` handler calls `authorize_websocket()` itself.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)
        try:
            require_api_key(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.detail,
                         "request_id": _request_id(request)},
            )
        return await call_next(request)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request-id to every request.

    Surfaces in error responses + audit log entries so an operator can
    correlate a user-reported failure to a server-side stack trace
    without asking the user for PHI.
    """

    async def dispatch(self, request: Request, call_next):
        rid = (request.headers.get("x-request-id")
               or uuid.uuid4().hex[:16])
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "no-rid")


# ---------------------------------------------------------------------------
# Error redaction
# ---------------------------------------------------------------------------

async def redacted_exception_handler(request: Request, exc: Exception):
    """Catch-all 500 handler that NEVER puts internal detail on the wire.

    Logs the real exception + request id server-side. The client gets
    a generic message + the request id so support can correlate.

    HTTPException subclasses have already been intercepted by FastAPI
    by the time this runs, so they keep their original detail (those
    are intentional 4xx messages, never include PHI).
    """
    rid = _request_id(request)
    log.exception("unhandled exception (rid=%s, path=%s)",
                  rid, request.url.path)
    body: dict = {"error": "internal_error", "request_id": rid}
    if reveal_errors():
        body["detail"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(status_code=500, content=body)


# ---------------------------------------------------------------------------
# Boot-time validation
# ---------------------------------------------------------------------------

def validate_config_or_raise() -> dict:
    """Verify the security env at process start so a misconfigured
    deployment fails to boot instead of silently allowing all traffic.

    Returns a small status dict for logging. Raises RuntimeError on
    fatal misconfiguration.
    """
    mode = get_auth_mode()
    keys = get_api_keys()
    cors = get_cors_origins()

    if mode == AuthMode.NONE:
        log.warning(
            "VIFI_AUTH_MODE=none -- API is OPEN. This must NEVER be set "
            "in production. Set VIFI_AUTH_MODE=api_key + VIFI_API_KEYS."
        )
    elif mode == AuthMode.API_KEY and not keys:
        raise RuntimeError(
            "VIFI_AUTH_MODE=api_key but VIFI_API_KEYS is empty. "
            "Refusing to boot fail-open. Generate a key with: "
            "python -c 'from security import generate_api_key; "
            "print(generate_api_key())'"
        )

    return {
        "auth_mode": mode.value,
        "n_api_keys": len(keys),
        "cors_origins": cors,
        "rate_limit": get_rate_limit(),
        "reveal_errors": reveal_errors(),
    }


# ---------------------------------------------------------------------------
# Lightweight in-process rate limiter
# ---------------------------------------------------------------------------
# We don't pull in slowapi as a dep just to gate a handful of endpoints.
# This is a fixed-window per-(remote-addr, endpoint) limiter that's
# good enough for a single-API-instance deployment. Multi-instance
# deployments behind a load balancer should switch this to a shared
# Redis token bucket -- documented in SECURITY.md.

class _FixedWindowLimiter:
    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._counters: dict[tuple[str, str], list[float]] = {}

    def check(self, key: tuple[str, str]) -> tuple[bool, int]:
        now = time.monotonic()
        bucket = self._counters.setdefault(key, [])
        cutoff = now - self.window_s
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= self.limit:
            retry_after = max(1, int(bucket[0] + self.window_s - now))
            return False, retry_after
        bucket.append(now)
        return True, 0


def parse_rate_limit(spec: str) -> tuple[int, float]:
    """Parse "<count>/<period>" e.g. "60/minute" -> (60, 60.0)."""
    count_str, period = spec.split("/")
    count = int(count_str)
    period = period.strip().lower()
    seconds = {"second": 1.0, "minute": 60.0, "hour": 3600.0,
               "day": 86400.0}.get(period)
    if seconds is None:
        raise ValueError(f"unknown rate-limit period: {period!r}")
    return count, seconds


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-(client-ip, path) fixed-window rate limit.

    Limit comes from VIFI_RATE_LIMIT env. Skips public paths (so
    health checks and probes never trip the limit).
    """

    def __init__(self, app, *, spec: Optional[str] = None) -> None:
        super().__init__(app)
        count, window = parse_rate_limit(spec or get_rate_limit())
        self._limiter = _FixedWindowLimiter(count, window)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        ok, retry_after = self._limiter.check((client_ip, request.url.path))
        if not ok:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"retry-after": str(retry_after)},
                content={"error": "rate_limited",
                         "retry_after_s": retry_after,
                         "request_id": _request_id(request)},
            )
        return await call_next(request)
