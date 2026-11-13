import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import failures as failures_db
from app.db.cache import cache_count
from app.db.connection import close_connection, init_db
from app.schemas import (
    AssessmentData,
    ErrorResponse,
    FailureType,
    SuccessResponse,
)
from app.services.batch import batch_worker
from app.services.browser import browser_manager
from app.services.lookup import (
    lookup_by_address,
    retry_failed_lookups,
    shutdown_browser,
    warmup_browser,
)
from app.services.stats import get_stats
from app.services.telegram_notify import flush_pending

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# Caller-facing message per failure type. Success and NOT_FOUND are byte-for-byte
# what the service returned before, so existing clients are unaffected; the
# `error_type` field is purely additive.
_FAILURE_MESSAGES = {
    FailureType.NOT_FOUND: "Property not found",
    FailureType.BLOCKED: "Lookup temporarily unavailable",
    FailureType.TIMEOUT: "Lookup timed out",
    FailureType.PARSE_EMPTY: "Lookup returned incomplete data",
    FailureType.ERROR: "Lookup failed",
    FailureType.BUSY: "Service busy, please retry shortly",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    await warmup_browser()
    if settings.batch_enabled:
        # Resumes any job left unfinished by a restart.
        await batch_worker.start()
    try:
        yield
    finally:
        await batch_worker.stop()
        await shutdown_browser()
        await flush_pending()
        close_connection()


app = FastAPI(title="BC Assessment Proxy", version="0.2.0", lifespan=lifespan)


@app.get("/address")
async def get_address(
    response: Response,
    address: str = Query(..., min_length=1, description="Formatted address string"),
):
    address = address.strip()
    if not address:
        return ErrorResponse(
            status="error",
            message="Address is required",
            error_type="invalid_input",
        )

    outcome = await lookup_by_address(address)

    if outcome.ok and outcome.result is not None:
        return SuccessResponse(data=AssessmentData(result=outcome.result, status=200))

    failure = outcome.failure or FailureType.ERROR
    if failure is FailureType.BUSY and outcome.retry_after:
        # Additive: the HTTP status and body of `busy` are unchanged.
        response.headers["Retry-After"] = str(outcome.retry_after)
    return ErrorResponse(
        status="error",
        message=_FAILURE_MESSAGES.get(failure, "Lookup failed"),
        error_type=failure.value,
    )


@app.get("/health")
async def health():
    """Report real scraper health, not just process liveness.

    Returns 503 when the scraper has failed repeatedly, so systemd and uptime
    monitors can see a dead browser instead of a process that is merely running.
    """
    health = browser_manager.health
    healthy = browser_manager.is_healthy

    body = {
        "ok": healthy,
        "provider": settings.lookup_provider,
        "cached_addresses": cache_count(),
        "unresolved_failures": failures_db.failure_counts(),
        **health.as_dict(),
    }

    if not healthy:
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/stats")
async def stats():
    base = await get_stats()
    base["unresolved_failures"] = failures_db.failure_counts()
    base["cached_addresses"] = cache_count()
    return base


@app.get("/failures")
async def list_failures(
    limit: int = Query(100, ge=1, le=1000),
    include_resolved: bool = False,
    due_only: bool = False,
):
    """Inspect the retry queue: what failed, why, when, and what is due."""
    return {
        "counts": failures_db.failure_counts(),
        "items": failures_db.list_failures(
            limit=limit, include_resolved=include_resolved, due_only=due_only
        ),
    }


@app.post("/failures/retry")
async def retry_failures(limit: int = Query(10, ge=1, le=100)):
    """Re-attempt queued failures whose backoff has elapsed."""
    return await retry_failed_lookups(limit=limit)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
