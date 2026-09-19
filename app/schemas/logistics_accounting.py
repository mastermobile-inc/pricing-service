from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PackageLine(BaseModel):
    line_key: str = Field(min_length=1, max_length=128)
    product_external_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    barcode: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(gt=0, max_digits=15, decimal_places=3)
    characteristic_external_id: str | None = None
    series_external_id: str | None = None


class ReceiptCount(BaseModel):
    line_key: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(ge=0, max_digits=15, decimal_places=3)


class PackageReceiptInput(BaseModel):
    transfer_id: int = Field(gt=0)
    lines: list[ReceiptCount] = Field(min_length=1, max_length=1000)
    damaged: bool = False

    @model_validator(mode="after")
    def unique_lines(self):
        keys = [row.line_key for row in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate receipt line")
        return self


class AccountingAck(BaseModel):
    status: Literal["applied", "error"]
    documents: list[str] = Field(default_factory=list, max_length=10)
    error: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_evidence(self):
        if self.status == "applied" and (
            not self.documents or any(not value.strip() for value in self.documents)
        ):
            raise ValueError("applied acknowledgement requires 1C document references")
        if self.status == "error" and not self.error:
            raise ValueError("error acknowledgement requires a reason")
        return self
