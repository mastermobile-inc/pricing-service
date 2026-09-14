"""Physical leg routing; never changes accounting or final destinations."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.core.config import get_settings
from app.models import (
    LogisticsDraftItem,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsWarehouse,
)
from app.models.logistics import LogisticsDraftAudit


def choices(session, transfer, source_id):
    settings = get_settings()
    if not settings.logistics_transit_routing_enabled or (transfer.payload or {}).get(
        "external_carrier_flow"
    ):
        return []
    if (
        not source_id
        or transfer.source_document_type not in {"rtu", "transfer"}
        or source_id == transfer.target_warehouse_id
    ):
        return []
    target = transfer.target_warehouse
    pilot = {v.lower() for v in settings.logistics_stage_pilot_warehouse_external_ids}
    if not target.is_active or target.external_id.lower() not in pilot:
        return []
    result = [{"mode": "direct", "warehouse_id": target.id, "warehouse_name": target.name}]
    source = session.get(LogisticsWarehouse, source_id) if source_id else None
    allowed = {v.lower() for v in settings.logistics_transit_source_external_ids}
    if source is None or not source.is_active or source.external_id.lower() not in allowed & pilot:
        return result
    ref = (settings.logistics_central_transit_external_id or "").lower()
    cache_key = ("logistics_transit", ref)
    if cache_key not in session.info:
        session.info[cache_key] = session.scalar(
            select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == ref)
        )
    central = session.info[cache_key]
    if (
        central
        and central.is_active
        and central.kind in {"central", "transit"}
        and ref in pilot
        and central.id not in {source_id, target.id}
    ):
        result.append(
            {"mode": "via_transit", "warehouse_id": central.id, "warehouse_name": central.name}
        )
    return result


def destination(session, transfer, source_id, mode):
    for option in choices(session, transfer, source_id):
        if option["mode"] == mode:
            return option["warehouse_id"]
    raise HTTPException(409, "Этот маршрут недоступен. Обновите документ и проверьте склад")


def details(transfer, source_id):
    return {
        "final_warehouse_id": transfer.target_warehouse_id,
        "final_warehouse_name": transfer.target_warehouse.name,
        "route_options": choices(object_session(transfer), transfer, source_id),
    }


def change_draft_route(session, *, draft_id, item_id, actor_user_id, mode):
    from app.services import logistics

    draft = logistics._get_draft_for_update(session, draft_id)
    actor = logistics._get_actor(session, actor_user_id)
    logistics._require_draft_mutation_access(actor, draft)
    if draft.status != "open" or draft.draft_type != "handoff":
        raise HTTPException(409, "Маршрут изменяется только в открытом черновике передачи")
    item = session.scalar(
        select(LogisticsDraftItem).where(
            LogisticsDraftItem.id == item_id, LogisticsDraftItem.draft_id == draft_id
        )
    )
    if item is None:
        raise HTTPException(404, "Документ не найден в черновике")
    target = destination(session, item.transfer, draft.warehouse_id, mode)
    from app.services.logistics_pending import require_handoff_ready

    require_handoff_ready(session, item.transfer, draft.warehouse_id)
    if item.dropoff_warehouse_id != target:
        session.add(
            LogisticsDraftAudit(
                draft_id=draft.id,
                actor_user_id=actor.id,
                event_type="route_changed",
                created_at=logistics.utcnow(),
                details={
                    "item_id": item.id,
                    "previous_dropoff_id": item.dropoff_warehouse_id,
                    "dropoff_id": target,
                },
            )
        )
        item.dropoff_warehouse_id = target
        session.commit()
    return logistics._serialize_draft(logistics._get_draft(session, draft_id))


def reroute(
    session,
    *,
    transfer_id,
    actor_user_id,
    mode,
    reason,
    expected_version,
    idempotency_key,
    source="bitrix",
):
    from app.services import logistics

    actor = logistics._get_actor(session, actor_user_id)
    logistics._require_role(actor, {"admin"})
    if (
        not reason.strip()
        or len(reason) > 1000
        or not idempotency_key.strip()
        or len(idempotency_key) > 128
    ):
        raise HTTPException(422, "Укажите причину и ключ операции допустимой длины")
    if not get_settings().logistics_transit_routing_enabled:
        raise HTTPException(403, "Изменение маршрута выключено")
    logistics.require_transfer_in_warehouse_scope(
        session,
        transfer_id=transfer_id,
        allowed_external_ids=get_settings().logistics_stage_pilot_warehouse_external_ids,
    )
    transfer = session.scalar(
        select(LogisticsTransfer).where(LogisticsTransfer.id == transfer_id).with_for_update()
    )
    if transfer is None:
        raise HTTPException(404, "Документ не найден")
    session.expire_all()
    state = transfer.state
    event_key = logistics._bounded_event_key("reroute", transfer_id, idempotency_key)
    previous = session.scalar(
        select(LogisticsTransferEvent).where(LogisticsTransferEvent.idempotency_key == event_key)
    )
    if previous:
        if (
            previous.user_id != actor.id
            or previous.meta.get("mode") != mode
            or previous.meta.get("expected_version") != expected_version
            or previous.comment != reason.strip()
        ):
            raise HTTPException(409, "Ключ операции уже использован с другими данными")
        return {"status": "ok", "version": previous.meta["result_version"]}
    if state is None or state.status != "in_transit" or state.version != expected_version:
        raise HTTPException(
            409, "Состояние изменилось. Обновите список; приёмка могла быть уже подтверждена"
        )
    handoff = session.scalar(
        select(LogisticsTransferEvent)
        .where(
            LogisticsTransferEvent.transfer_id == transfer_id,
            LogisticsTransferEvent.event_type == "handed_to_driver",
        )
        .order_by(LogisticsTransferEvent.id.desc())
    )
    if handoff is None:
        raise HTTPException(409, "Не найдена подтверждённая передача текущего рейса")
    target = destination(session, transfer, handoff.warehouse_id, mode)
    if not reason.strip():
        raise HTTPException(422, "Укажите причину изменения маршрута")
    old_target = state.dropoff_warehouse_id
    event = LogisticsTransferEvent(
        transfer_id=transfer.id,
        event_type="route_dropoff_changed",
        event_at=logistics.utcnow(),
        warehouse_id=None,
        dropoff_warehouse_id=target,
        driver_id=state.driver_id,
        user_id=actor.id,
        comment=reason.strip(),
        source=source,
        idempotency_key=event_key,
        document_ref=transfer.document_number,
        meta={
            "previous_dropoff_id": old_target,
            "dropoff_id": target,
            "mode": mode,
            "expected_version": expected_version,
            "result_version": expected_version + 1,
        },
    )
    session.add(event)
    state.dropoff_warehouse_id = target
    state.last_event_type = event.event_type
    state.last_event_at = event.event_at
    state.last_user_id = actor.id
    if handoff.meta and handoff.meta.get("draft_id"):
        draft = logistics._get_draft(session, handoff.meta["draft_id"])
        item = logistics._get_or_create_route_item(
            session,
            route_run_id=draft.route_run_id,
            transfer_id=transfer.id,
            dropoff_warehouse_id=target,
        )
        if item is not None:
            item.dropoff_warehouse_id = target
    logistics._commit_state_change(session)
    return {"status": "ok", "version": state.version}
