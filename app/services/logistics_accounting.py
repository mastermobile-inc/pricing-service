"""Scan-led protocol, opt-in per immutable order plan; 1C owns all stock."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.core.config import get_settings
from app.models.logistics import LogisticsTransferEvent, LogisticsWarehouse
from app.models.logistics_accounting import LogisticsAccountingEvent, LogisticsReceiptCheck
from app.models.logistics_order_plan import LogisticsOrderPlan, LogisticsOrderPlanUnit
from app.schemas.logistics_accounting import AccountingAck, PackageLine, PackageReceiptInput

PROTOCOL = "scan-led-v1"


def require_commands_enabled() -> None:
    if not get_settings().logistics_scan_accounting_enabled:
        raise HTTPException(409, "scan-led commands are disabled; native 1C acceptance required")


def enabled(unit) -> bool:
    return (
        unit.plan is not None and (unit.plan.payload or {}).get("accounting_protocol") == PROTOCOL
    )


def manifest(payload: dict | None) -> list[dict]:
    try:
        rows = [
            PackageLine.model_validate(row).model_dump(mode="json")
            for row in (payload or {}).get("lines", [])
        ]
    except (ValidationError, TypeError) as exc:
        raise HTTPException(409, "invalid package goods manifest") from exc
    if not rows or len({row["line_key"] for row in rows}) != len(rows):
        raise HTTPException(409, "package requires an unambiguous goods manifest")
    return sorted(rows, key=lambda row: row["line_key"])


def freeze_started_unit(session: Session, unit, incoming: dict | None) -> None:
    if unit.id is None or not enabled(unit):
        return
    if session.scalar(
        select(LogisticsAccountingEvent.id).where(LogisticsAccountingEvent.unit_id == unit.id)
    ):
        if manifest(unit.payload) != manifest(incoming):
            raise HTTPException(409, "sent package contents are immutable; manual review required")


def receipt_status(session: Session, unit) -> str | None:
    return session.scalar(
        select(LogisticsReceiptCheck.status).where(LogisticsReceiptCheck.unit_id == unit.id)
    )


def status_for_unit(session: Session, unit) -> dict:
    events = session.scalars(
        select(LogisticsAccountingEvent).where(LogisticsAccountingEvent.unit_id == unit.id)
    ).all()
    return {
        "transfer_id": unit.transfer_id,
        "receipt_status": receipt_status(session, unit),
        "accounting_status": (
            "error"
            if any(row.status == "error" for row in events)
            else (
                "pending"
                if any(row.status == "pending" for row in events)
                else "applied" if events else "not_started"
            )
        ),
    }


def ready_for_pickup(session: Session, unit) -> bool:
    if not enabled(unit):
        return True
    if receipt_status(session, unit) != "matched":
        return False
    applied = set(
        session.scalars(
            select(LogisticsAccountingEvent.operation).where(
                LogisticsAccountingEvent.unit_id == unit.id,
                LogisticsAccountingEvent.status == "applied",
            )
        )
    )
    return applied == {"dispatch", "final_receipt"}


def validate_receipt(unit, data: PackageReceiptInput) -> tuple[str, list[dict]]:
    expected = {row["line_key"]: Decimal(row["quantity"]) for row in manifest(unit.payload)}
    actual = {row.line_key: row.quantity for row in data.lines}
    # Every expected row must be counted explicitly, including zero quantity.
    if not set(expected).issubset(actual):
        raise HTTPException(409, "count every expected goods line before confirming receipt")
    matched = expected == actual and not data.damaged
    return ("matched" if matched else "discrepancy"), [
        row.model_dump(mode="json") for row in data.lines
    ]


def record_fact(session: Session, *, unit, event, actor, receipt=None) -> None:
    if not enabled(unit):
        return
    require_commands_enabled()
    if event.event_type == "handed_to_driver":
        if receipt_status(session, unit) is not None:
            raise HTTPException(409, "final receipt already recorded; package cannot be sent again")
        operation = "dispatch"
    elif (
        event.event_type == "accepted_at_point"
        and event.warehouse_id == unit.plan.final_warehouse_id
    ):
        if receipt is None:
            raise HTTPException(409, "final receipt requires an item-by-item count")
        status, lines = validate_receipt(unit, receipt)
        session.add(
            LogisticsReceiptCheck(
                unit_id=unit.id,
                actor_user_id=actor.id,
                physical_event_id=event.id,
                status=status,
                lines=lines,
                damaged=receipt.damaged,
            )
        )
        if status == "discrepancy":
            return  # Physical receipt persists; no automatic inventory/issue approval.
        operation = "final_receipt"
    else:
        return  # An intermediate warehouse is not the final accounting receipt.
    existing = session.scalar(
        select(LogisticsAccountingEvent).where(
            LogisticsAccountingEvent.unit_id == unit.id,
            LogisticsAccountingEvent.operation == operation,
        )
    )
    if existing is not None:
        return  # A subsequent transport leg must not write off the same goods twice.
    leg = session.scalar(
        select(LogisticsTransferEvent)
        .where(
            LogisticsTransferEvent.transfer_id == event.transfer_id,
            LogisticsTransferEvent.event_type == "handed_to_driver",
            LogisticsTransferEvent.id <= event.id,
        )
        .order_by(LogisticsTransferEvent.id.desc())
        .limit(1)
    )
    if leg is None:
        raise HTTPException(409, "package transport leg not found")
    leg_source = session.get(LogisticsWarehouse, leg.warehouse_id)
    leg_target = session.get(LogisticsWarehouse, leg.dropoff_warehouse_id)
    payload = {
        "protocol": PROTOCOL,
        "origin_order_external_id": unit.plan.origin_order_external_id,
        "plan_key": unit.plan.plan_key,
        "plan_version": unit.plan.plan_version,
        "unit_key": unit.unit_key,
        "package_external_id": unit.transfer_external_id,
        "leg_id": leg.id,
        "leg_source_warehouse_external_id": leg_source.external_id,
        "leg_target_warehouse_external_id": leg_target.external_id,
        "physical_event_id": event.id,
        "actor_user_id": actor.id,
        "actor_external_id": actor.external_id,
        "event_at": event.event_at.isoformat(),
        "source_warehouse_external_id": unit.source_warehouse.external_id,
        "final_warehouse_external_id": unit.plan.final_warehouse.external_id,
        "warehouse_id": event.warehouse_id,
        "dropoff_warehouse_id": event.dropoff_warehouse_id,
        "driver_id": event.driver_id,
        "lines": manifest(unit.payload),
        "receipt": receipt.model_dump(mode="json") if receipt is not None else None,
    }
    session.add(
        LogisticsAccountingEvent(
            event_id=str(uuid4()),
            unit_id=unit.id,
            physical_event_id=event.id,
            operation=operation,
            status="pending",
            payload=payload,
        )
    )


def pending_events(session: Session, limit: int = 100) -> list[dict]:
    require_commands_enabled()
    dispatch = aliased(LogisticsAccountingEvent)
    dispatch_applied = (
        select(dispatch.id)
        .where(
            dispatch.unit_id == LogisticsAccountingEvent.unit_id,
            dispatch.operation == "dispatch",
            dispatch.status == "applied",
        )
        .exists()
    )
    rows = session.scalars(
        select(LogisticsAccountingEvent)
        .where(
            LogisticsAccountingEvent.status.in_(["pending", "error"]),
            (LogisticsAccountingEvent.operation == "dispatch") | dispatch_applied,
        )
        .order_by(LogisticsAccountingEvent.id)
        .limit(limit)
    ).all()
    result = []
    for row in rows:
        result.append(
            {
                "event_id": row.event_id,
                "operation": row.operation,
                "status": row.status,
                "payload": row.payload,
            }
        )
    return result


def acknowledge(session: Session, event_id: str, ack: AccountingAck):
    # Serialise different package ACKs of one order, so the last one observes
    # all previously committed units before emitting order-ready downstream.
    plan_id = session.scalar(
        select(LogisticsOrderPlanUnit.plan_id)
        .join(
            LogisticsAccountingEvent, LogisticsAccountingEvent.unit_id == LogisticsOrderPlanUnit.id
        )
        .where(LogisticsAccountingEvent.event_id == event_id)
    )
    if plan_id is not None:
        session.execute(
            select(LogisticsOrderPlan.id).where(LogisticsOrderPlan.id == plan_id).with_for_update()
        )
    row = session.scalar(
        select(LogisticsAccountingEvent)
        .where(LogisticsAccountingEvent.event_id == event_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "accounting event not found")
    result = {"documents": sorted(set(ack.documents))}
    if row.status == "applied":
        if ack.status != "applied" or row.result != result:
            raise HTTPException(409, "accounting result is immutable")
        return row
    if ack.status == "applied" and row.operation == "final_receipt":
        applied = session.scalar(
            select(LogisticsAccountingEvent.id).where(
                LogisticsAccountingEvent.unit_id == row.unit_id,
                LogisticsAccountingEvent.operation == "dispatch",
                LogisticsAccountingEvent.status == "applied",
            )
        )
        if applied is None:
            raise HTTPException(409, "dispatch accounting must be acknowledged first")
    row.status = ack.status
    row.result = result if ack.status == "applied" else None
    row.error = ack.error if ack.status == "error" else None
    row.applied_at = datetime.now(timezone.utc) if ack.status == "applied" else None
    session.flush()
    return row
