from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool


class SitePrepaySnapshot(BaseModel):
    """Facts supplied only by the authenticated native site collector."""

    model_config = ConfigDict(extra="forbid")

    site_order_id: str = Field(pattern=r"^[1-9][0-9]{0,11}$")
    created_at: AwareDatetime
    observed_at: AwareDatetime
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: Literal["RUB"]
    payment_system_id: int
    payment_row_system_id: int
    payment_row_count: int = Field(ge=0)
    payment_row_amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    paid_amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    has_payment_history: StrictBool
    has_shipment_history: StrictBool
    marked: StrictBool
    delivery_allowed: StrictBool
    canceled: StrictBool
    status: str = Field(min_length=1, max_length=8)


class PrepayTickRequest(BaseModel):
    snapshots: list[SitePrepaySnapshot] = Field(max_length=20)


class PrepayWorkItem(BaseModel):
    batch_id: str
    site_order_id: str
    action: Literal["refresh", "cancel_site", "manual_review", "complete"]
    closure_document_ref: str | None = None
    closure_document_number: str | None = None
    reason: str | None = None
    expected_created_at: AwareDatetime
    expected_amount: Decimal
    payment_system_id: int


class PrepayWorkResponse(BaseModel):
    checked_at: datetime
    apply_enabled: StrictBool
    items: list[PrepayWorkItem]


class PrepayKnownResponse(BaseModel):
    site_order_ids: list[str]
    next_cursor: int | None = None
