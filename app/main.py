from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

from app.schemas import AssessmentData, ErrorResponse, SuccessResponse
from app.services.lookup import lookup_by_address, shutdown_browser, warmup_browser
from app.services.stats import get_stats


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await warmup_browser()
    yield
    await shutdown_browser()


app = FastAPI(title="BC Assessment Proxy", version="0.1.0", lifespan=lifespan)


@app.get("/address")
async def get_address(
    address: str = Query(..., min_length=1, description="Formatted address string"),
):
    address = address.strip()
    result = await lookup_by_address(address)

    if result is None:
        return ErrorResponse(status="error", message="Property not found")

    return SuccessResponse(
        data=AssessmentData(result=result, status=200),
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/stats")
async def stats():
    return await get_stats()


if __name__ == "__main__":
    import uvicorn

    from app.config import settings

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
