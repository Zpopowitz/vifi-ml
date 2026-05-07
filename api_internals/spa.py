"""Static dashboard SPA mount for the FastAPI app.

Extracted from `api.py::create_app` by PR-H2. Mounts the
`dashboard/` directory at `/` with `html=True` so `/` serves
`index.html` automatically. Must be called LAST in the route
registration sequence so explicit API routes (`/health`,
`/predict`, etc.) take precedence over the SPA's catch-all.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI


def mount_dashboard_spa(app: FastAPI, *, dashboard_dir: Path) -> bool:
    """Mount the static dashboard SPA at `/`.

    Returns True if the mount happened, False if the directory was
    missing (UI disabled). Either way, never raises — a missing UI
    doesn't break the API.
    """
    log = logging.getLogger("vifi.api")
    if not dashboard_dir.is_dir():
        log.warning("dashboard directory %s not found; UI disabled", dashboard_dir)
        return False
    # Lazy import: starlette pulls aiofiles on this path; avoid the
    # cost when the SPA is disabled.
    from fastapi.staticfiles import StaticFiles  # noqa: PLC0415

    app.mount("/", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")
    log.info("dashboard SPA mounted at / from %s", dashboard_dir)
    return True
