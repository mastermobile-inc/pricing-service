"""Price decisions are separate from purchase order lines."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProcurementPriceBatch(Base):
    __tablename__ = "procurement_price_batch"
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    owner: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    version: Mapped[int] = mapped_column(default=1)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    message_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    document_ref: Mapped[str | None] = mapped_column(String(64))
    document_number: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    lines: Mapped[list["ProcurementPriceLine"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )


class ProcurementPriceLine(Base):
    __tablename__ = "procurement_price_line"
    __table_args__ = (UniqueConstraint("batch_id", "code", "price_type"),)
    batch_id: Mapped[int] = mapped_column(ForeignKey("procurement_price_batch.id"), index=True)
    code: Mapped[str] = mapped_column(String(100))
    price_type: Mapped[str] = mapped_column(String(16))
    # Exact decimal strings also preserve the expected snapshot for conflict detection.
    old_price: Mapped[str | None] = mapped_column(String(32))
    new_price: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8), default="RUB")


class ProcurementPriceEvent(Base):
    __tablename__ = "procurement_price_event"
    batch_id: Mapped[int] = mapped_column(ForeignKey("procurement_price_batch.id"), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcurementPricePreset(Base):
    __tablename__ = "procurement_price_preset"
    __table_args__ = (UniqueConstraint("owner", "name"),)
    owner: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(100))
    filters: Mapped[dict[str, Any]] = mapped_column(JSON)
