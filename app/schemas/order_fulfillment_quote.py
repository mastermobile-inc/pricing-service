"""Предварительный расчёт, не резерв и не разрешение оплаты."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Key = Annotated[str, Field(min_length=1, max_length=128)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6)]
Delivery = Literal["pickup", "cdek", "russian_post", "yandex_delivery"]


class QuoteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StockIdentity(QuoteModel):
    product_id: Key
    characteristic_id: Key | None = None
    series_id: Key | None = None
    unit_id: Key = "piece"

    def key(self) -> tuple:
        return (self.product_id, self.characteristic_id, self.series_id, self.unit_id)


class SourceAllocation(QuoteModel):
    warehouse_id: Key
    quantity: Quantity


class QuoteLine(StockIdentity):
    line_id: Key
    quantity: Quantity
    # None: recommend; explicit list: exact choice, never silently replace it.
    sources: list[SourceAllocation] | None = Field(default=None, min_length=1, max_length=50)


class FulfillmentQuoteRequest(QuoteModel):
    delivery_method: Delivery
    pickup_point_id: Key | None = None
    lines: list[QuoteLine] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def consistent(self):
        if (self.delivery_method == "pickup") != (self.pickup_point_id is not None):
            raise ValueError("pickup_point_id required only for pickup")
        if len({row.line_id for row in self.lines}) != len(self.lines):
            raise ValueError("duplicate line_id")
        return self


class QuoteLeg(QuoteModel):
    source_warehouse_id: str
    target_warehouse_id: str
    departure_at: datetime
    received_at: datetime


class QuotedAllocation(SourceAllocation):
    warehouse_name: str
    ready_at: datetime
    legs: list[QuoteLeg]


class QuoteOption(QuoteModel):
    warehouse_id: str
    warehouse_name: str
    available_quantity: Decimal
    needs_transfer: bool
    intercity: bool


class QuotedLine(StockIdentity):
    line_id: str
    quantity: Decimal
    sources: list[QuotedAllocation]
    options: list[QuoteOption]


class FulfillmentQuoteResponse(QuoteModel):
    contract_version: Literal["fulfillment_quote_v1"] = "fulfillment_quote_v1"
    quote_id: str
    quoted_at: datetime
    payment_condition_until: datetime
    ready_at: datetime
    carrier_handoff_at: datetime | None = None
    carrier_delivery_at: datetime | None = None
    consolidation_warehouse_id: str
    consolidation_warehouse_name: str
    delivery_method: Delivery
    lines: list[QuotedLine]
    warnings: list[str]
    test_only: Literal[True] = True
    inventory_provenance: Literal["synthetic_fixture"] = "synthetic_fixture"
    display_label: str
    reservation_created: Literal[False] = False
    payment_allowed: Literal[False] = False
    production_eligible: Literal[False] = False
