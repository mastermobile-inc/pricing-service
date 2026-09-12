from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SiteCustomerPriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: int = Field(gt=0)


class SiteCustomerPriceResponse(BaseModel):
    user_id: int
    status: Literal["confirmed", "retail", "conflict", "unavailable"]
    reason: str
    checked_at: datetime
    check_id: str
    price_type: str = ""
    expected_group: str = ""
    catalog_group_id: int | None = None
