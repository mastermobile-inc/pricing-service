from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

PriceType = Literal["bronze", "platinum"]


class PricingFilter(BaseModel):
    start: date
    end: date
    search: str = Field(default="", max_length=200)
    subject: str = Field(default="", max_length=100)
    brand: str = Field(default="", max_length=100)
    quality: str = Field(default="", max_length=100)
    profitability_min: Decimal | None = None
    profitability_max: Decimal | None = None
    defect_min: Decimal | None = Field(default=None, ge=0)
    defect_max: Decimal | None = Field(default=None, ge=0)
    trend: Literal["all", "up", "down"] = "all"
    sort: Literal[
        "name",
        "bronze",
        "platinum",
        "sales_qty",
        "sales_amount",
        "profitability",
        "defect_pct",
        "dynamics_pct",
        "forecast_qty",
        "forecast_amount",
        "previous_year_qty",
        "previous_year_amount",
    ] = "name"
    descending: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end < self.start or (self.end - self.start).days > 731:
            raise ValueError("Период должен быть от 1 до 732 дней")
        for key in ["profitability", "defect"]:
            lo, hi = getattr(self, key + "_min"), getattr(self, key + "_max")
            if lo is not None and hi is not None and lo > hi:
                raise ValueError("Нижняя граница больше верхней")
        return self


class PricingExportFilter(PricingFilter):
    format: Literal["csv", "xlsx"] = "xlsx"


class CompetitorOffer(BaseModel):
    name: str
    price: Decimal
    currency: str | None = None
    url: str | None = None
    collected_at: datetime | None = None


class PricingRow(BaseModel):
    code: str
    name: str
    subject: str | None = None
    brand: str | None = None
    quality: str | None = None
    bronze: Decimal | None = None
    platinum: Decimal | None = None
    currency: str = "RUB"
    sales_qty: Decimal | None = None
    sales_amount: Decimal | None = None
    profitability: Decimal | None = None
    defect_qty: Decimal | None = None
    defect_pct: Decimal | None = None
    dynamics_pct: Decimal | None = None
    forecast_qty: Decimal | None = None
    forecast_amount: Decimal | None = None
    previous_year_qty: Decimal | None = None
    previous_year_amount: Decimal | None = None
    forecast_status: str = "history_missing"
    competitors: list[CompetitorOffer] = Field(default_factory=list)


class PricingTable(BaseModel):
    items: list[PricingRow]
    total: int
    start: date
    end: date
    observed_at: datetime
    facts_through: date | None
    warnings: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    qualities: list[str] = Field(default_factory=list)


class PriceChange(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str = Field(min_length=1, max_length=100)
    price_type: PriceType
    old_price: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    new_price: Decimal = Field(gt=0, max_digits=15, decimal_places=2)
    currency: Literal["RUB"] = "RUB"


class PriceBatchCreate(BaseModel):
    request_key: UUID
    lines: list[PriceChange] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def distinct_lines(self):
        keys = [(x.code, x.price_type) for x in self.lines]
        if len(set(keys)) != len(keys):
            raise ValueError("Повтор товара и типа цены")
        if any(x.old_price == x.new_price for x in self.lines):
            raise ValueError("Новая цена не отличается от текущей")
        return self


class PriceBatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner: str
    status: str
    version: int
    created_at: datetime
    approved_at: datetime | None = None
    document_number: str | None = None
    error: str | None = None
    lines: list[PriceChange]


class PriceApproval(BaseModel):
    version: int = Field(ge=1)


class PricePresetWrite(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    filters: PricingFilter


class PricePresetRead(PricePresetWrite):
    id: int


class PriceHistoryPoint(BaseModel):
    date: datetime
    price_type: PriceType
    price: Decimal
    currency: str
