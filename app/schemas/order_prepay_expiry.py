from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool


class PaymentAvailabilityEvent(BaseModel):
    """A persisted site transition, not a browser timestamp or a reserve date."""

    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    sequence: int = Field(ge=1, strict=True)
    occurred_at: AwareDatetime
    available: StrictBool


class PaymentAvailabilityClock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["web_prepay_72h_v2"]
    source: Literal["site_gate_v1"] | None = None
    valid_until: AwareDatetime | None = None
    enrollment_id: UUID
    # Enrollment is emitted by creation of a NEW order after rollout.
    enrolled_at: AwareDatetime
    events: list[PaymentAvailabilityEvent] = Field(min_length=1, max_length=512)


class PaymentClosureHold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    batch_id: UUID
    frozen_at: AwareDatetime
    clock_revision: int = Field(ge=1, strict=True)


class SitePrepaySnapshot(BaseModel):
    """Facts supplied only by the authenticated native site collector."""

    model_config = ConfigDict(extra="forbid")

    site_order_id: str = Field(pattern=r"^[1-9][0-9]{0,11}$")
    payment_clock: PaymentAvailabilityClock | None = None
    closure_hold: PaymentClosureHold | None = None
    payment_started: StrictBool = False
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
    action: Literal["refresh", "prepare_closure", "cancel_site", "manual_review", "complete"]
    closure_document_ref: str | None = None
    closure_document_number: str | None = None
    reason: str | None = None
    expected_created_at: AwareDatetime
    expected_amount: Decimal
    payment_system_id: int
    expected_clock_id: UUID | None = None


class PrepayWorkResponse(BaseModel):
    checked_at: datetime
    apply_enabled: StrictBool
    items: list[PrepayWorkItem]


class PrepayKnownResponse(BaseModel):
    site_order_ids: list[str]
    next_cursor: int | None = None
