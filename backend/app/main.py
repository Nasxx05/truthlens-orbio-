"""TrustLens backend entrypoint.

Run locally with:
    uvicorn app.main:app --reload --port 8000

And, to take scraping off the request path:
    python -m app.worker
"""

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import cache
from app.logging_config import configure_logging
from app.queue import broker, queue_stats
from app.routes import analyze
from app.security import client_identity, limiter

configure_logging()
logger = logging.getLogger("app.main")

app = FastAPI(
    title="TrustLens API",
    description="Cross-platform review and video-verdict trust assistant.",
    version="0.9.0",
)

# The popup runs on a chrome-extension:// origin. Chrome allows the request on
# the strength of the extension's host_permissions, but the middleware keeps
# local development predictable across browsers and dev tools. The standalone
# web app (served over http://localhost, http://127.0.0.1, or deployed to
# Vercel) is allowed too; file:// pages send Origin: null and are deliberately
# not matched here, so webapp/ must be served by a static HTTP server rather
# than opened directly.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^(chrome-extension|moz-extension)://.*$"
        r"|^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
        r"|^https://([a-z0-9-]+\.)*vercel\.app$"
    ),
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Correlation id, body-size cap, and rate limiting.

    Ordered deliberately: reject oversized or too-frequent requests before any
    handler runs, since the whole point is to spend nothing on them.
    """
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    started = time.monotonic()

    # An oversized body is either a mistake or an attack; either way there is
    # no legitimate 1 MB analyze request.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
        logger.warning(
            "request body too large",
            extra={"event_type": "body_too_large", "request_id": request_id, "bytes": int(declared)},
        )
        return JSONResponse(
            status_code=413,
            content={"detail": f"request body exceeds {settings.max_body_bytes} bytes"},
            headers={"X-Request-ID": request_id},
        )

    # Only the expensive endpoints are limited; /health must stay callable by
    # monitoring at any rate.
    if request.url.path.startswith("/analyze"):
        identity = client_identity(request)
        verdict = await limiter.check(
            identity, limit=settings.rate_limit, window=settings.rate_limit_window
        )
        if not verdict.allowed:
            logger.warning(
                "rate limited",
                extra={
                    "event_type": "rate_limited",
                    "request_id": request_id,
                    "limit": verdict.limit,
                    "window_s": settings.rate_limit_window,
                },
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"rate limit exceeded ({verdict.limit} requests per "
                        f"{settings.rate_limit_window}s)"
                    )
                },
                headers={
                    "Retry-After": str(verdict.retry_after),
                    "X-RateLimit-Limit": str(verdict.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-Request-ID": request_id,
                },
            )

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "unhandled request failure",
            extra={"event_type": "request_error", "request_id": request_id,
                   "path": request.url.path},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal error"},
            headers={"X-Request-ID": request_id},
        )

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request complete",
        extra={
            "event_type": "request",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return response


@app.on_event("startup")
async def startup() -> None:
    """Open optional dependencies. Neither is required to serve traffic."""
    await cache.connect()
    queued = await broker.available()
    logger.info(
        "service ready",
        extra={
            "event_type": "startup",
            "cache": cache.available,
            "queue": queued,
            "private_targets_allowed": settings.allow_private_targets,
        },
    )
    if settings.allow_private_targets:
        logger.warning(
            "ALLOW_PRIVATE_TARGETS is enabled — the backend may fetch private "
            "addresses. Do not run this way in production.",
            extra={"event_type": "insecure_config"},
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    await cache.close()
    await broker.close()


app.include_router(analyze.router, tags=["analyze"])


@app.get("/health")
async def health() -> dict:
    """Liveness plus the state of every optional dependency."""
    return {
        "status": "ok",
        "service": "trustlens",
        "version": app.version,
        "cache": await cache.stats(),
        "queue": await queue_stats(),
        "config": {
            "filter_enabled": settings.filter_enabled,
            "cache_ttl_hours": settings.cache_ttl_hours,
            "rate_limit": f"{settings.rate_limit}/{settings.rate_limit_window}s",
            "allow_private_targets": settings.allow_private_targets,
            "worker_concurrency": settings.worker_concurrency,
        },
    }
