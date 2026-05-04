"""Security primitives for the ViFi API.

Centralizes everything that touches authentication, authorization,
CORS, rate limiting, and error redaction so the surface that needs
review for HIPAA / FDA cybersecurity premarket submission is a single
file.

Settings (env-driven so secrets never enter committed config):

    VIFI_AUTH_MODE          : "none" (dev only) | "api_key" (production)
                              default: "none"
    VIFI_API_KEYS           : comma-separated allowed API keys
                              required when AUTH_MODE=api_key
    VIFI_API_KEYS_FILE      : path to a JSON file of key metadata
                              (preferred over VIFI_API_KEYS for prod);
                              format: { "<key>": {"expires_at": "...",
                              "revoked": false, "name": "...", "scopes": [...]}}
    VIFI_CORS_ORIGINS       : comma-separated allowed origins (default: empty)
    VIFI_RATE_LIMIT         : "<count>/<period>" (default: "60/minute")
    VIFI_REVEAL_ERRORS      : "true" exposes 5xx detail (dev only)
    VIFI_TRUSTED_PROXIES    : comma-separated CIDRs whose X-Forwarded-For
                              we honor for rate-limit + log purposes
                              (default: empty — trust no proxies)

Public endpoints (NO auth required):
    GET  /health, /readyz   -- liveness for orchestrator probes
    GET  /roadmap           -- product surface metadata, no PHI
    GET  /                  -- root banner

Why middleware (not per-route): every new endpoint added by mistake
should be auth-required by default. Opt-in public endpoints listed
explicitly here. Fail-closed.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
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


PUBLIC_PATHS: frozenset[str] = frozenset({
    "/", "/health", "/readyz", "/roadmap",
    "/api/v1/", "/api/v1/health", "/api/v1/readyz", "/api/v1/roadmap",
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


def _load_api_keys_file() -> dict[str, dict]:
    """Load a JSON file of key metadata, or {}.

    Format:
      {
        "vifi_<token>": {
          "name": "clinician-dashboard",
          "scopes": ["read:hr", "read:rr"],
          "expires_at": "2027-01-01T00:00:00Z",
          "revoked": false
        }
      }
    """
    path = os.environ.get("VIFI_API_KEYS_FILE")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.error("VIFI_API_KEYS_FILE %s did not parse to a dict", path)
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.error("could not read VIFI_API_KEYS_FILE=%s: %s", path, exc)
        return {}


def get_api_keys() -> set[str]:
    """Allowed API keys (comma-separated VIFI_API_KEYS) ∪ active keys
    from VIFI_API_KEYS_FILE.

    "Active" = not revoked, not expired.

    Empty set in api_key mode means *no one* can call protected
    endpoints — fail-closed by design.
    """
    keys: set[str] = set()
    raw = os.environ.get("VIFI_API_KEYS", "")
    keys.update(k.strip() for k in raw.split(",") if k.strip())

    file_keys = _load_api_keys_file()
    now = datetime.now(timezone.utc)
    for k, meta in file_keys.items():
        if meta.get("revoked"):
            continue
        exp_str = meta.get("expires_at")
        if exp_str:
            try:
                exp = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                if exp <= now:
                    continue
            except ValueError:
                log.error("invalid expires_at %r on key %r", exp_str, k[:8])
                continue
        keys.add(k)
    return keys


def get_cors_origins() -> list[str]:
    """Allowed CORS origins. Empty disables cross-origin requests."""
    raw = os.environ.get("VIFI_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def get_rate_limit() -> str:
    return os.environ.get("VIFI_RATE_LIMIT", "60/minute")


def reveal_errors() -> bool:
    return os.environ.get("VIFI_REVEAL_ERRORS", "false").lower() == "true"


def get_trusted_proxies() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse VIFI_TRUSTED_PROXIES into IP networks. Used to decide whether
    X-Forwarded-For from upstream is trustworthy."""
    raw = os.environ.get("VIFI_TRUSTED_PROXIES", "")
    nets = []
    for cidr in (c.strip() for c in raw.split(",") if c.strip()):
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            log.error("invalid VIFI_TRUSTED_PROXIES entry %r: %s", cidr, exc)
    return nets


def generate_api_key() -> str:
    """Generate a cryptographically random API key."""
    return f"vifi_{secrets.token_urlsafe(32)}"


# ---------------------------------------------------------------------------
# Path normalization (I064)
# ---------------------------------------------------------------------------

def _normalize_path(path: str) -> str:
    """Normalize URL path so trailing slashes and double slashes can't be
    used to bypass auth. /health/ and /api/v1//health both resolve here.
    """
    if not path:
        return "/"
    # Collapse double slashes.
    while "//" in path:
        path = path.replace("//", "/")
    # Strip trailing slash (except root).
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


# ---------------------------------------------------------------------------
# API key extraction + validation
# ---------------------------------------------------------------------------

def _extract_key(headers, query_params) -> Optional[str]:
    """Pull an API key from the request, in priority order:
      1. Authorization: Bearer <key>
      2. X-API-Key: <key>
      3. ?api_key=<key>     (WebSocket fallback only)
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
    """Constant-time check against the allowed set."""
    if not key:
        return False
    for valid in allowed:
        if secrets.compare_digest(key, valid):
            return True
    return False


def _client_ip(request: Request) -> str:
    """Resolve the real client IP, honoring X-Forwarded-For only when
    the immediate peer is in VIFI_TRUSTED_PROXIES."""
    immediate = request.client.host if request.client else "unknown"
    trusted = get_trusted_proxies()
    if not trusted:
        return immediate
    try:
        peer = ipaddress.ip_address(immediate)
    except ValueError:
        return immediate
    if not any(peer in n for n in trusted):
        return immediate
    # XFF is comma-separated; rightmost is the immediate proxy, leftmost
    # is the original client. Walk right-to-left through the trusted
    # proxies and stop at the first untrusted hop.
    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return immediate
    chain = [c.strip() for c in xff.split(",") if c.strip()]
    for hop in reversed(chain):
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            return hop
        if not any(ip in n for n in trusted):
            return hop
    return chain[0] if chain else immediate


def require_api_key(request: Request) -> None:
    """Dependency: raises 401 if the request is missing/invalid auth."""
    mode = get_auth_mode()
    if mode == AuthMode.NONE:
        return
    keys = get_api_keys()
    if not keys:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "auth_misconfigured")
    key = _extract_key(request.headers, request.query_params)
    if not _key_is_valid(key, keys):
        # Structured failed-auth log (I063): includes IP, path, UA, and
        # first 6 chars of the attempted key for forensics. Never logs
        # the full key.
        log.warning(
            "auth_failed", extra={
                "event": "auth_failed",
                "client_ip": _client_ip(request),
                "path": request.url.path,
                "method": request.method,
                "user_agent": request.headers.get("user-agent", "?"),
                "attempted_key_prefix": (key[:6] + "...") if key else "(none)",
            },
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid_or_missing_api_key")


async def authorize_websocket(websocket: WebSocket) -> bool:
    """WebSocket-friendly auth check. Closes the socket on failure."""
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

    Protects every HTTP route except the explicitly-public paths.
    Always fails closed: misconfiguration => 503, missing key => 401,
    unknown key => 401. Path is normalized (I064) so trailing slashes
    and double slashes can't be used to bypass.

    WebSockets bypass HTTP middleware in Starlette/FastAPI; the
    `/api/v1/stream` handler calls `authorize_websocket()` itself.
    """

    async def dispatch(self, request: Request, call_next):
        normalized = _normalize_path(request.url.path)
        if normalized in PUBLIC_PATHS:
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
    """Attach a request-id to every request."""

    async def dispatch(self, request: Request, call_next):
        rid = (request.headers.get("x-request-id")
               or uuid.uuid4().hex[:16])
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth security headers.

    Caddy adds these at the proxy in prod, but a direct API hit (dev,
    internal cluster traffic, k8s probe) shouldn't escape unhardened.
    """

    HEADERS = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        # Cache PHI-bearing responses to memory only (defense in depth
        # against shared caches).
        "cache-control": "no-store",
    }

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for k, v in self.HEADERS.items():
            response.headers.setdefault(k, v)
        return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "no-rid")


# ---------------------------------------------------------------------------
# Error redaction
# ---------------------------------------------------------------------------

async def redacted_exception_handler(request: Request, exc: Exception):
    """Catch-all 500 handler that NEVER puts internal detail on the wire."""
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
    deployment fails to boot instead of silently allowing all traffic."""
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
            "VIFI_AUTH_MODE=api_key but no API keys configured. "
            "Set VIFI_API_KEYS or VIFI_API_KEYS_FILE. Refusing to "
            "boot fail-open. Generate a key: "
            "python -c 'from security import generate_api_key; "
            "print(generate_api_key())'"
        )

    # Validate CORS origins look like URLs (I068).
    for origin in cors:
        if not (origin.startswith(("http://", "https://"))):
            raise RuntimeError(
                f"VIFI_CORS_ORIGINS entry {origin!r} is not a URL "
                f"(must start with http:// or https://). A typo here "
                f"silently never matches and breaks the dashboard."
            )

    return {
        "auth_mode": mode.value,
        "n_api_keys": len(keys),
        "cors_origins": cors,
        "rate_limit": get_rate_limit(),
        "reveal_errors": reveal_errors(),
        "trusted_proxies": [str(n) for n in get_trusted_proxies()],
        "audit_chain_active": bool(os.environ.get("VIFI_AUDIT_CHAIN_KEY")),
        "audit_encryption_active": bool(
            os.environ.get("VIFI_AUDIT_ENCRYPTION_KEY")
        ),
    }


# ---------------------------------------------------------------------------
# Lightweight in-process rate limiter
# ---------------------------------------------------------------------------
# Single-instance fixed-window. Multi-instance deployments behind a load
# balancer should use a shared Redis token bucket — see SECURITY.md.

class _LRUFixedWindowLimiter:
    """Fixed-window counter with bounded LRU eviction (I058).

    Without LRU, the counter dict grew without bound; per-IP buckets
    accumulated forever. Now bounded; oldest entries evicted when over
    capacity.
    """

    def __init__(self, limit: int, window_s: float, *,
                 max_buckets: int = 100_000) -> None:
        self.limit = limit
        self.window_s = window_s
        self._max_buckets = max_buckets
        self._counters: "OrderedDict[tuple[str, str], list[float]]" = (
            OrderedDict()
        )

    def check(self, key: tuple[str, str]) -> tuple[bool, float]:
        now = time.monotonic()
        bucket = self._counters.get(key)
        if bucket is None:
            bucket = []
            self._counters[key] = bucket
            if len(self._counters) > self._max_buckets:
                self._counters.popitem(last=False)
        else:
            self._counters.move_to_end(key)
        cutoff = now - self.window_s
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= self.limit:
            retry_after = max(1.0, bucket[0] + self.window_s - now)
            return False, retry_after
        bucket.append(now)
        return True, 0.0


# Backwards compat alias for tests.
_FixedWindowLimiter = _LRUFixedWindowLimiter


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
    """Per-(client-ip, path) fixed-window rate limit with LRU eviction.

    Honors X-Forwarded-For when the immediate peer is in
    VIFI_TRUSTED_PROXIES (otherwise every client behind a proxy
    shares one bucket).

    Skips public paths (so health checks never trip the limit).
    """

    def __init__(self, app, *, spec: Optional[str] = None) -> None:
        super().__init__(app)
        count, window = parse_rate_limit(spec or get_rate_limit())
        self._limiter = _LRUFixedWindowLimiter(count, window)

    async def dispatch(self, request: Request, call_next):
        normalized = _normalize_path(request.url.path)
        if normalized in PUBLIC_PATHS:
            return await call_next(request)
        client_ip = _client_ip(request)
        ok, retry_after = self._limiter.check((client_ip, normalized))
        if not ok:
            # Float-precision Retry-After (I065).
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"retry-after": f"{retry_after:.2f}"},
                content={"error": "rate_limited",
                         "retry_after_s": float(round(retry_after, 2)),
                         "request_id": _request_id(request)},
            )
        return await call_next(request)
