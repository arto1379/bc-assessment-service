"""HTTP endpoints for batch lookups (POST/GET/DELETE /batch, CSV export).

All routes sit behind the BATCH_ENABLED flag (503 when off) and an X-API-Key
check (401). Errors use the service's usual `{"status": "error", ...}` body.
"""

from __future__ import annotations

import csv
import hmac
import io
import json
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response

from app.config import settings
from app.schemas import BatchSubmitRequest, ErrorResponse
from app.services import batch as batch_service

MAX_ADDRESS_LENGTH = 200
MAX_PAGE_SIZE = 500


class BatchHTTPError(Exception):
    def __init__(self, status_code: int, message: str, error_type: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=ErrorResponse(
                message=self.message, error_type=self.error_type
            ).model_dump(),
        )


def _invalid(message: str) -> BatchHTTPError:
    return BatchHTTPError(400, message, "invalid_input")


async def require_batch_access(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    if not settings.batch_enabled:
        raise BatchHTTPError(503, "Batch lookups are disabled", "disabled")
    keys = settings.get_batch_api_keys()
    supplied = (x_api_key or "").encode()
    # No keys configured means nobody gets in (fail closed).
    if not supplied or not any(hmac.compare_digest(supplied, k.encode()) for k in keys):
        raise BatchHTTPError(401, "Missing or invalid X-API-Key", "unauthorized")


router = APIRouter(prefix="/batch", dependencies=[Depends(require_batch_access)])


def _validate(body: BatchSubmitRequest) -> list[str]:
    addresses = body.addresses
    if not addresses:
        raise _invalid("addresses must contain at least one address")
    limit = settings.batch_max_addresses
    if len(addresses) > limit:
        raise _invalid(f"addresses has {len(addresses)} entries; the limit is {limit}")
    for idx, address in enumerate(addresses):
        if not address or not address.strip():
            raise _invalid(f"addresses[{idx}] is blank")
        if len(address) > MAX_ADDRESS_LENGTH:
            raise _invalid(
                f"addresses[{idx}] is longer than {MAX_ADDRESS_LENGTH} characters"
            )
    if body.callback_url is not None:
        _validate_callback_url(body.callback_url)
    return addresses


def _validate_callback_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise _invalid("callback_url must be an https URL")


def _status_body(data: dict) -> dict:
    return {"status": "success", "data": data}


def _not_found(job_id: str) -> BatchHTTPError:
    return BatchHTTPError(404, f"Unknown batch job {job_id!r}", "not_found")


@router.post("", status_code=202)
async def submit_batch(body: BatchSubmitRequest):
    addresses = _validate(body)
    try:
        data = await batch_service.submit(
            addresses, client_ref=body.client_ref, callback_url=body.callback_url
        )
    except batch_service.BatchLimitError as exc:
        raise BatchHTTPError(429, str(exc), "limit_reached") from exc
    return _status_body(data)


@router.get("/{job_id}")
async def get_batch(
    job_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
):
    data = await batch_service.get_status(job_id, offset=offset, limit=limit)
    if data is None:
        raise _not_found(job_id)
    return _status_body(data)


@router.delete("/{job_id}")
async def cancel_batch(job_id: str):
    data = await batch_service.cancel(job_id)
    if data is None:
        raise _not_found(job_id)
    return _status_body(data)


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Stop spreadsheet apps from evaluating a cell as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


@router.get("/{job_id}/results.csv")
async def export_batch_csv(job_id: str):
    job = await batch_service.get_status(job_id, offset=0, limit=1)
    if job is None:
        raise _not_found(job_id)

    fields = batch_service.result_fields()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["index", "address", "state", "source", "error_type", *fields])
    async for row in batch_service.iter_all_items(job_id):
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        writer.writerow(
            [
                row["idx"],
                _csv_safe(row["address_raw"]),
                row["state"],
                row["source"] or "",
                row["error_type"] or "",
                *(_csv_safe(result.get(field)) for field in fields),
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="batch-{job_id}.csv"'},
    )
