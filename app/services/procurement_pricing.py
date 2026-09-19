"""Analytical calculations and immutable, user-owned price decisions."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.procurement_pricing import (
    ProcurementPriceBatch,
    ProcurementPriceEvent,
    ProcurementPriceLine,
)
from app.schemas.procurement_pricing import PriceBatchCreate, PricingFilter, PricingRow


class PricingConflict(ValueError):
    pass


def ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return (numerator / denominator * 100).quantize(Decimal(".01")) if denominator > 0 else None


def forecast(
    total: Decimal, start: date, end: date, today: date, complete: bool
) -> tuple[Decimal | None, str]:
    if end < today:
        return None, "completed"
    if not complete:
        return None, "history_incomplete"
    days = (today - start).days
    if days <= 0:
        return None, "no_completed_days"
    return (total / days * ((end - start).days + 1)).quantize(Decimal(".01")), "ready"


def filter_rows(rows: list[PricingRow], filters: PricingFilter) -> list[PricingRow]:
    def included(row: PricingRow) -> bool:
        if filters.search.casefold() not in (row.name + " " + row.code).casefold():
            return False
        for key in ("subject", "brand", "quality"):
            if getattr(filters, key) and getattr(row, key) != getattr(filters, key):
                return False
        for field, prefix in [("profitability", "profitability"), ("defect_pct", "defect")]:
            value = getattr(row, field)
            lo, hi = getattr(filters, prefix + "_min"), getattr(filters, prefix + "_max")
            if (lo is not None or hi is not None) and value is None:
                return False
            if lo is not None and value < lo or hi is not None and value > hi:
                return False
        if filters.trend != "all":
            if (
                row.dynamics_pct is None
                or (filters.trend == "up" and row.dynamics_pct <= 0)
                or (filters.trend == "down" and row.dynamics_pct >= 0)
            ):
                return False
        return True

    selected = [r for r in rows if included(r)]
    known = [r for r in selected if getattr(r, filters.sort) is not None]
    missing = [r for r in selected if getattr(r, filters.sort) is None]
    known.sort(
        key=lambda r: (
            (
                getattr(r, filters.sort).casefold()
                if filters.sort == "name"
                else getattr(r, filters.sort)
            ),
            r.code,
        ),
        reverse=filters.descending,
    )
    return known + sorted(missing, key=lambda r: r.code)


def actor_key(session) -> str:
    return f"{session.domain}:{session.user_id}"


def event(db: Session, batch: ProcurementPriceBatch, actor: str, kind: str, detail=None):
    db.add(ProcurementPriceEvent(batch_id=batch.id, actor=actor, kind=kind, detail=detail or {}))


def get_batch(db: Session, batch_id: int, owner: str, *, lock=False) -> ProcurementPriceBatch:
    query = select(ProcurementPriceBatch).where(
        ProcurementPriceBatch.id == batch_id, ProcurementPriceBatch.owner == owner
    )
    if lock:
        query = query.with_for_update()
    batch = db.scalar(query)
    if batch is None:
        raise LookupError("Пакет не найден")
    return batch


def create_batch(db: Session, payload: PriceBatchCreate, owner: str) -> ProcurementPriceBatch:
    existing = db.scalar(
        select(ProcurementPriceBatch).where(
            ProcurementPriceBatch.request_key == str(payload.request_key)
        )
    )
    if existing:
        original = [
            (x.code, x.price_type, x.old_price, x.new_price, x.currency) for x in existing.lines
        ]
        incoming = [
            (
                x.code,
                x.price_type,
                str(x.old_price) if x.old_price is not None else None,
                str(x.new_price),
                x.currency,
            )
            for x in payload.lines
        ]
        if existing.owner != owner or original != incoming:
            raise PricingConflict("Идентификатор запроса уже использован для другого пакета")
        return existing
    batch = ProcurementPriceBatch(request_key=str(payload.request_key), owner=owner, status="draft")
    batch.lines = [
        ProcurementPriceLine(
            code=x.code,
            price_type=x.price_type,
            old_price=str(x.old_price) if x.old_price is not None else None,
            new_price=str(x.new_price),
            currency=x.currency,
        )
        for x in payload.lines
    ]
    db.add(batch)
    db.flush()
    event(db, batch, owner, "created")
    return batch


def approve_batch(
    db: Session, batch_id: int, version: int, owner: str, read_prices: Callable
) -> ProcurementPriceBatch:
    batch = get_batch(db, batch_id, owner, lock=True)
    if batch.status == "approved" and batch.version == version + 1:
        return batch
    if batch.status != "draft" or batch.version != version:
        raise PricingConflict("Пакет уже изменён или утверждён")
    prices = read_prices(sorted({line.code for line in batch.lines}))
    for line in batch.lines:
        key = (line.code, line.price_type)
        if key not in prices:
            raise PricingConflict(f"{line.code}: актуальная цена не подтверждена")
        current = prices[key]
        expected = Decimal(line.old_price) if line.old_price is not None else None
        if current["price"] != expected or current["currency"] != line.currency:
            raise PricingConflict(f"{line.code}: цена или валюта изменились в 1С, обновите таблицу")
    batch.status = "approved"
    batch.version += 1
    batch.approved_at = datetime.now(UTC)
    event(db, batch, owner, "approved")
    return batch
