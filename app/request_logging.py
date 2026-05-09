"""Optional JSON access logs when ``LOG_JSON=1`` (Railway log drains)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("app.request")

HOT_PATHS = frozenset(
    {
        "/redeem-payment-code",
        "/generate-pdf",
        "/api/my-purchase-history",
    }
)


def _log_json_enabled() -> bool:
    return os.environ.get("LOG_JSON", "").strip().lower() in ("1", "true", "yes")


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):  # type: ignore[override]
        if not _log_json_enabled():
            return await call_next(request)

        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        t0 = time.perf_counter()
        path = request.url.path
        hot = path in HOT_PATHS or path.startswith("/api/my-purchase-history")

        try:
            response = await call_next(request)
        except Exception:
            dt_ms = (time.perf_counter() - t0) * 1000
            logger.exception(
                "http_request_failed request_id=%s path=%s duration_ms=%.2f hot=%s",
                rid,
                path,
                dt_ms,
                hot,
            )
            raise

        dt_ms = (time.perf_counter() - t0) * 1000
        line = {
            "msg": "http_request",
            "request_id": rid,
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "duration_ms": round(dt_ms, 2),
            "hot_path": hot,
        }
        logger.info(json.dumps(line, ensure_ascii=False))
        response.headers["X-Request-ID"] = rid
        return response
