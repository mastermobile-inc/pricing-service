"""Durable delivery of physical facts to 1C; never a stock ledger."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LogisticsAccountingEvent(Base):
    __tablename__ = "logistics_accounting_event"
    __table_args__ = (
        UniqueConstraint("unit_id", "operation", name="uq_logistics_accounting_unit_operation"),
    )

    event_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    unit_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_order_plan_unit.id", ondelete="RESTRICT"), nullable=False
    )
    physical_event_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_transfer_event.id", ondelete="RESTRICT"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)


class LogisticsReceiptCheck(Base):
    __tablename__ = "logistics_receipt_check"
    unit_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_order_plan_unit.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_user.id", ondelete="RESTRICT"), nullable=False
    )
    physical_event_id: Mapped[int] = mapped_column(
        ForeignKey("logistics_transfer_event.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    lines: Mapped[list] = mapped_column(JSON, nullable=False)
    damaged: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
