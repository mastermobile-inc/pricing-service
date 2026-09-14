"""Read-only pending-document views and shared handoff eligibility."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import and_, case, exists, func, or_, select, true
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.models import (
    LogisticsDraftItem,
    LogisticsDriver,
    LogisticsManualReview,
    LogisticsWarehouse,
)
from app.models import LogisticsTransfer as Unit
from app.models import LogisticsTransferState as State
from app.services import logistics
from app.services.logistics_drivers import serialize


def document_freshness():
    settings = get_settings()
    result = {}
    for kind, path in (
        ("rtu", settings.logistics_rtu_sync_success_file),
        ("transfer", settings.logistics_transfer_sync_success_file),
    ):
        try:
            stamp = (
                datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc) if path else None
            )
        except OSError:
            stamp = None
        result[kind] = {
            "last_success_at": stamp,
            "stale": stamp is None
            or (datetime.now(timezone.utc) - stamp).total_seconds()
            > settings.logistics_sync_freshness_seconds,
        }
    return result


def current_warehouse():
    return case(
        (State.transfer_id.is_(None), Unit.source_warehouse_id), else_=State.current_warehouse_id
    )


def destination():
    if get_settings().logistics_transit_routing_enabled:
        return Unit.target_warehouse_id
    first = func.coalesce(Unit.document_target_warehouse_id, Unit.target_warehouse_id)
    return case((first == current_warehouse(), Unit.target_warehouse_id), else_=first)


def handoff_predicate(warehouse_id):
    # Missing explicit readiness is compatible with legacy imports of ready posted units.
    ready = func.coalesce(Unit.payload["ready_for_handoff"].as_boolean(), True)
    return and_(
        current_warehouse() == warehouse_id,
        or_(State.transfer_id.is_(None), State.status == logistics.STATUS_AT_WAREHOUSE),
        Unit.target_warehouse_id != warehouse_id,
        Unit.onec_deleted.is_(False),
        Unit.source_document_type.in_(["rtu", "transfer"]),
        Unit.onec_status == "posted",
        ready.is_(True),
        func.coalesce(Unit.payload["external_carrier_flow"].as_boolean(), False).is_(False),
        ~exists(
            select(LogisticsManualReview.id).where(
                or_(
                    LogisticsManualReview.transfer_id == Unit.id,
                    and_(
                        LogisticsManualReview.source_document_type == Unit.source_document_type,
                        LogisticsManualReview.source_external_id == Unit.external_id,
                    ),
                ),
                LogisticsManualReview.status == "open",
            )
        ),
    )


def require_handoff_ready(session, transfer, warehouse_id):
    target = aliased(LogisticsWarehouse)
    valid = session.scalar(
        select(Unit.id)
        .outerjoin(State, State.transfer_id == Unit.id)
        .join(target, target.id == destination())
        .where(Unit.id == transfer.id, handoff_predicate(warehouse_id), target.is_active.is_(True))
    )
    if valid is None:
        raise HTTPException(
            409,
            "Документ не готов к передаче или уже доставлен. Проверьте готовность и раздел разбора",
        )


def pending_documents(
    session,
    *,
    actor_user_id,
    operation,
    warehouse_id,
    draft_id=None,
    driver_id=None,
    limit=50,
    offset=0,
):
    actor = logistics._get_actor(session, actor_user_id)
    logistics._require_role(
        actor,
        (
            {"sender", "admin", "logist"}
            if operation == "handoff"
            else {"receiver", "admin", "logist"}
        ),
    )
    if actor.role not in {"admin", "logist"} and actor.default_warehouse_id != warehouse_id:
        raise HTTPException(403, "Недоступен чужой склад")
    logistics.require_warehouse_in_scope(
        session,
        warehouse_id=warehouse_id,
        allowed_external_ids=get_settings().logistics_stage_pilot_warehouse_external_ids,
    )
    draft = None
    if draft_id is not None:
        draft = logistics._get_draft(session, draft_id)
        logistics._require_draft_mutation_access(actor, draft)
        if (
            draft.draft_type != operation
            or draft.warehouse_id != warehouse_id
            or draft.status != "open"
        ):
            raise HTTPException(409, "Черновик не соответствует выбранной операции или складу")
    target = aliased(LogisticsWarehouse)
    target_id = destination() if operation == "handoff" else State.dropoff_warehouse_id
    if operation == "handoff" and draft:
        selected_dropoff = (
            select(LogisticsDraftItem.dropoff_warehouse_id)
            .where(
                LogisticsDraftItem.draft_id == draft.id, LogisticsDraftItem.transfer_id == Unit.id
            )
            .correlate(Unit)
            .scalar_subquery()
        )
        target_id = func.coalesce(selected_dropoff, target_id)
    stmt = select(
        Unit.id.label("transfer_id"),
        Unit.document_date,
        Unit.document_number,
        Unit.source_document_type,
        Unit.site_order_number,
        target.name.label("dropoff_warehouse_name"),
        State.driver_id,
        LogisticsDriver.full_name.label("driver_name"),
        case(
            (
                and_(
                    State.status == logistics.STATUS_AT_WAREHOUSE,
                    State.last_event_type == logistics.EVENT_ACCEPTED_AT_POINT,
                    State.current_warehouse_id != Unit.target_warehouse_id,
                    State.current_warehouse_id.in_(
                        select(LogisticsWarehouse.id).where(
                            LogisticsWarehouse.kind.in_(["central", "transit"])
                        )
                    ),
                ),
                "На транзите, ожидает следующей отправки",
            ),
            else_=None,
        ).label("status_label"),
    )
    stmt = stmt.outerjoin(State, State.transfer_id == Unit.id).join(target, target.id == target_id)
    stmt = stmt.outerjoin(LogisticsDriver, LogisticsDriver.id == State.driver_id)
    if operation == "handoff":
        stmt = stmt.where(handoff_predicate(warehouse_id), target.is_active.is_(True))
    else:
        stmt = stmt.where(
            State.status == logistics.STATUS_IN_TRANSIT, State.dropoff_warehouse_id == warehouse_id
        )
    driver_ids = (
        session.scalars(select(stmt.subquery().c.driver_id).distinct()).all()
        if operation == "receipt"
        else []
    )
    if operation == "receipt" and driver_id is not None:
        stmt = stmt.where(State.driver_id == driver_id)
    scanned = select(LogisticsDraftItem.transfer_id).where(
        LogisticsDraftItem.draft_id == (draft.id if draft else -1)
    )
    in_draft = Unit.id.in_(scanned)
    eligible = stmt.add_columns(in_draft.label("in_draft")).cte("eligible")
    counts = (
        select(
            func.count().label("total"),
            select(func.count(LogisticsDraftItem.id))
            .where(LogisticsDraftItem.draft_id == (draft.id if draft else -1))
            .scalar_subquery()
            .label("draft_total_count"),
            func.coalesce(func.sum(case((eligible.c.in_draft, 1), else_=0)), 0).label(
                "scanned_count"
            ),
        )
        .select_from(eligible)
        .subquery()
    )
    page = (
        select(eligible)
        .order_by(eligible.c.document_date, eligible.c.transfer_id)
        .offset(offset)
        .limit(limit)
        .subquery()
    )
    # One statement = one PostgreSQL snapshot for both page and counters, even
    # when another employee confirms concurrently or the requested page is empty.
    rows = (
        session.execute(
            select(counts, page)
            .select_from(counts.outerjoin(page, true()))
            .order_by(page.c.document_date, page.c.transfer_id)
        )
        .mappings()
        .all()
    )
    total, scanned_count = rows[0]["total"], rows[0]["scanned_count"]
    drivers = session.scalars(
        select(LogisticsDriver)
        .where(LogisticsDriver.id.in_([uid for uid in driver_ids if uid is not None]))
        .order_by(LogisticsDriver.full_name)
    ).all()
    return dict(
        items=[
            {key: row[key] for key in page.c.keys()}
            for row in rows
            if row["transfer_id"] is not None
        ],
        total=total,
        draft_total_count=rows[0]["draft_total_count"],
        scanned_count=scanned_count,
        remaining_count=total - scanned_count,
        offset=offset,
        limit=limit,
        freshness=document_freshness(),
        drivers=[serialize(row) for row in drivers],
    )
