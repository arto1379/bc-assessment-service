from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class FailureType(str, Enum):
    """Why a lookup did not produce a usable result.

    The whole point of this enum is that the old code collapsed every one of
    these into a bare `None`, which the API reported as "Property not found" —
    so a blocked scraper was indistinguishable from an address that genuinely
    does not exist.
    """

    NOT_FOUND = "not_found"  # autocomplete returned no real suggestion
    BLOCKED = "blocked"  # challenge / 403 / 429 / redirected off-site
    TIMEOUT = "timeout"  # a step exceeded its budget
    PARSE_EMPTY = "parse_empty"  # result page loaded but carried no total_value
    ERROR = "error"  # anything else
    BUSY = "busy"  # too many queued requests; caller should retry

    @property
    def is_scraper_fault(self) -> bool:
        """True when the failure is ours/BC's, not the caller's address."""
        return self is not FailureType.NOT_FOUND


@dataclass
class LookupOutcome:
    """Result of one lookup attempt, carrying enough context to alert and retry."""

    result: Optional["AssessmentResult"] = None
    failure: Optional[FailureType] = None
    reason: Optional[str] = None
    url: Optional[str] = None
    step: Optional[str] = None
    restarted: bool = False
    snapshot_path: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.failure is None

    @classmethod
    def success(cls, result: "AssessmentResult") -> "LookupOutcome":
        return cls(result=result)

    @classmethod
    def failed(
        cls,
        failure: FailureType,
        reason: str,
        *,
        url: str | None = None,
        step: str | None = None,
        restarted: bool = False,
        snapshot_path: str | None = None,
    ) -> "LookupOutcome":
        return cls(
            failure=failure,
            reason=reason,
            url=url,
            step=step,
            restarted=restarted,
            snapshot_path=snapshot_path,
        )


class AssessmentResult(BaseModel):
    total_value: Optional[str] = None
    year_built: Optional[str] = None
    number_of_bed: Optional[str] = None
    number_of_baths: Optional[str] = None
    description: Optional[str] = None
    land_size: Optional[str] = None
    strata_area: Optional[str] = None
    building_storeys: Optional[str] = None


class AssessmentData(BaseModel):
    result: AssessmentResult
    status: int = 200


class SuccessResponse(BaseModel):
    status: str = "success"
    data: AssessmentData


class ErrorResponse(BaseModel):
    status: str = "error"
    message: str = ""
    # Added field: lets callers tell "no such property" from "scraper is blocked".
    # Existing clients that only read `status`/`message` are unaffected.
    error_type: Optional[str] = None
