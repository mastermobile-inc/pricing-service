#!/usr/bin/env python3
"""Переводит в «Ожидаем клиента» карточки, где мы ответили, а стадию не двинули.

Автоматический перевод стадии появился недавно, поэтому старые обращения остались
в «В работе»/«Новая» с уже данным ответом. Автозакрытие по молчанию клиента их не
увидит — оно работает только из «Ожидаем клиента», — и карточки будут висеть вечно.

Скрипт трогает только бесспорные случаи. Всё, где нужен человек, выводится
отдельным списком `review` и не меняется:

* есть внутренняя заметка — коллеги что-то обсуждали, стадия может быть осознанной;
* заведён и не завершён возврат — ждём товар, а не ответ клиента;
* висит неотправленная команда — ответ ещё в пути.

    python scripts/backfill_site_service_stage_after_reply.py           # только разбор
    python scripts/backfill_site_service_stage_after_reply.py --apply   # перевести
    python scripts/backfill_site_service_stage_after_reply.py --ticket 789
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import or_, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.infrastructure.db.session import session_scope  # noqa: E402
from app.models.customer_return import CustomerReturnShipment  # noqa: E402
from app.models.site_service_requests import (  # noqa: E402
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestMessage,
)
from app.services.expertise_bitrix import BitrixRestClient  # noqa: E402
from app.services.site_service_requests_worker import (  # noqa: E402
    SiteServiceRequestBitrixWriter,
)

# Возврат считается закрытым, когда товар у нас либо возврат отменён.
_CLOSED_RETURN_STATUSES = {"picked_up", "onec_return_confirmed", "cancelled"}
_OPEN_COMMAND_STATUSES = ("pending", "leased")


def _blocking_reason(session: Session, case: SiteServiceRequestCase) -> str | None:
    """Почему карточку нельзя двигать автоматически, либо ``None``."""

    has_note = session.scalar(
        select(SiteServiceRequestMessage.id)
        .where(
            SiteServiceRequestMessage.case_id == case.id,
            SiteServiceRequestMessage.message_kind == "internal_note",
        )
        .limit(1)
    )
    if has_note is not None:
        return "internal_note"

    pending_command = session.scalar(
        select(SiteServiceRequestCommand.id)
        .where(
            SiteServiceRequestCommand.case_id == case.id,
            SiteServiceRequestCommand.status.in_(_OPEN_COMMAND_STATUSES),
        )
        .limit(1)
    )
    if pending_command is not None:
        return "outbound_command_pending"

    if case.bitrix_item_id is not None:
        open_return = session.scalar(
            select(CustomerReturnShipment.id)
            .where(
                or_(
                    CustomerReturnShipment.service_request_item_id == int(case.bitrix_item_id),
                    CustomerReturnShipment.bitrix_case_id == str(case.bitrix_item_id),
                ),
                CustomerReturnShipment.status.not_in(_CLOSED_RETURN_STATUSES),
            )
            .limit(1)
        )
        if open_return is not None:
            return "open_return"
    return None


def plan_stage_backfill(
    session: Session,
    *,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter,
    apply: bool,
    ticket_id: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    stage_map = settings.site_service_requests_bitrix_stage_map
    client_stage_id = str(stage_map.get("client") or "")
    movable_stage_ids = {str(stage_map.get(key) or "") for key in ("new", "work")} - {""}
    if not client_stage_id or not movable_stage_ids:
        raise SystemExit("site_service_request_required_stage_missing")

    query = select(SiteServiceRequestCase).where(
        SiteServiceRequestCase.bitrix_item_id.is_not(None),
        SiteServiceRequestCase.first_response_at.is_not(None),
        SiteServiceRequestCase.awaiting_reply_since.is_(None),
        SiteServiceRequestCase.closed_without_response_at.is_(None),
    )
    if ticket_id is not None:
        query = query.where(SiteServiceRequestCase.source_ticket_id == ticket_id)
    cases = session.scalars(query.order_by(SiteServiceRequestCase.source_ticket_id)).all()

    moved: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for case in cases:
        visible_reply = session.scalar(
            select(SiteServiceRequestMessage.id)
            .where(
                SiteServiceRequestMessage.case_id == case.id,
                SiteServiceRequestMessage.direction == "outbound",
                SiteServiceRequestMessage.is_visible_to_customer.is_(True),
            )
            .limit(1)
        )
        if visible_reply is None:
            continue

        item = writer.get_item(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=int(case.bitrix_item_id),
        )
        current_stage = str(item.get("stageId") or "")
        if current_stage not in movable_stage_ids:
            continue

        row = {
            "ticketId": case.source_ticket_id,
            "itemId": case.bitrix_item_id,
            "stageId": current_stage,
        }
        reason = _blocking_reason(session, case)
        if reason is not None:
            review.append({**row, "reason": reason})
            continue

        row["applied"] = False
        if apply:
            readback = writer.update_item_fields(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=int(case.bitrix_item_id),
                fields={"stageId": client_stage_id},
            )
            if str(readback.get("stageId") or "") != client_stage_id:
                raise RuntimeError("bitrix_stage_backfill_readback_failed")
            writer.add_timeline_comment(
                entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                item_id=int(case.bitrix_item_id),
                comment=(
                    "Ответ клиенту по обращению уже отправлен, карточка переведена "
                    "в «Ожидаем клиента». "
                    f"[site-service-stage-backfill:{case.id}]"
                ),
            )
            row["applied"] = True
        moved.append(row)
    return {"moved": moved, "review": review}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Перевести стадию в Битриксе.")
    parser.add_argument("--ticket", type=int, default=None, help="Разобрать один тикет.")
    args = parser.parse_args()

    settings = get_settings()
    writer = SiteServiceRequestBitrixWriter(
        BitrixRestClient(
            str(settings.site_service_requests_bitrix_webhook_url),
            retry_transient_html_403=True,
        )
    )

    with session_scope(read_only=not args.apply) as session:
        plan = plan_stage_backfill(
            session,
            settings=settings,
            writer=writer,
            apply=args.apply,
            ticket_id=args.ticket,
        )

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "moved": len(plan["moved"]),
                "review": len(plan["review"]),
                **plan,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
