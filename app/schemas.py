from typing import Optional

from pydantic import BaseModel


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
