#!/usr/bin/env python3
"""Проставляет «Клиент ждёт ответа с» по уже сохранённой переписке.

Отметка появляется при обработке нового сообщения, поэтому обращения, где клиент
написал давно и больше ничего не присылал, остались бы без неё навсегда — а это
ровно те карточки, которые и теряются. Скрипт считает то же самое по истории в
нашей базе: последний видимый клиенту ответ поддержки и первое сообщение клиента
после него.

    python scripts/backfill_site_service_awaiting_reply.py           # разбор без записи
    python scripts/backfill_site_service_awaiting_reply.py --apply   # проставить
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.infrastructure.db.session import session_scope  # noqa: E402
from app.models.site_service_requests import (  # noqa: E402
    SiteServiceRequestCase,
    SiteServiceRequestMessage,
)
from app.services.expertise_bitrix import BitrixRestClient  # noqa: E402
from app.services.site_service_requests_worker import (  # noqa: E402
    SiteServiceRequestBitrixWriter,
    resolved_site_service_request_field_map,
)

_SUPPORT_KINDS = {"support", "support-team", "support_team"}


def awaiting_since_from_history(
    messages: list[SiteServiceRequestMessage],
) -> datetime | None:
    """Момент, с которого обращение ждёт ответа, либо ``None``."""

    support_last: datetime | None = None
    for message in messages:
        if message.author_kind in _SUPPORT_KINDS and message.is_visible_to_customer:
            if support_last is None or message.created_at > support_last:
                support_last = message.created_at
    if support_last is None:
        # Первого ответа не было: этой паузой управляет SLA первого ответа.
        return None

    customer_after: datetime | None = None
    for message in messages:
        if message.author_kind != "customer":
            continue
        if message.created_at <= support_last:
            continue
        if customer_after is None or message.created_at < customer_after:
            customer_after = message.created_at
    return customer_after


def backfill(
    session: Session,
    *,
    settings: Settings,
    writer: SiteServiceRequestBitrixWriter | None,
    apply: bool,
) -> list[dict[str, Any]]:
    field_map = resolved_site_service_request_field_map(settings)
    cases = session.scalars(
        select(SiteServiceRequestCase)
        .where(
            SiteServiceRequestCase.awaiting_reply_since.is_(None),
            SiteServiceRequestCase.first_response_at.is_not(None),
        )
        .order_by(SiteServiceRequestCase.source_ticket_id)
    ).all()

    results: list[dict[str, Any]] = []
    for case in cases:
        messages = list(
            session.scalars(
                select(SiteServiceRequestMessage)
                .where(SiteServiceRequestMessage.case_id == case.id)
                .order_by(SiteServiceRequestMessage.created_at)
            ).all()
        )
        awaiting = awaiting_since_from_history(messages)
        if awaiting is None:
            continue

        row = {
            "ticketId": case.source_ticket_id,
            "itemId": case.bitrix_item_id,
            "awaitingSince": awaiting.isoformat(),
            "applied": False,
        }
        if apply:
            case.awaiting_reply_since = awaiting
            case.awaiting_reply_notified_at = None
            case.awaiting_reply_escalated_at = None
            if writer is not None and case.bitrix_item_id is not None:
                writer.update_item_fields(
                    entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
                    item_id=int(case.bitrix_item_id),
                    fields={field_map["awaiting_reply_since"]: awaiting},
                )
            session.commit()
            row["applied"] = True
        results.append(row)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Проставить отметку.")
    args = parser.parse_args()

    settings = get_settings()
    writer = (
        SiteServiceRequestBitrixWriter(
            BitrixRestClient(
                str(settings.site_service_requests_bitrix_webhook_url),
                retry_transient_html_403=True,
            )
        )
        if args.apply
        else None
    )

    with session_scope(read_only=not args.apply) as session:
        rows = backfill(session, settings=settings, writer=writer, apply=args.apply)

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "waiting": len(rows),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
