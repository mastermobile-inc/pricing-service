#!/usr/bin/env python3
"""Пересобирает связи «клиент и заказ» у карточек сервисных обращений.

Карточка получает клиента и заказ один раз, при обработке события с сайта.
Если тогда связь не сложилась — например номер заказа пришёл как «№ 246936» и не
совпал с голым номером в сделке, — карточка остаётся в статусе
``client_match_required`` навсегда: новых событий по ней может уже не быть.

Скрипт проходит по таким карточкам и повторяет тот же поиск, что делает worker,
уже исправленным кодом. Ничего не создаёт: контакты и сделки только ищутся среди
существующих, и запись идёт лишь туда, где связь определилась однозначно.

    python -m scripts.resync_site_service_request_links            # разбор без записи
    python -m scripts.resync_site_service_request_links --apply    # записать связи
    python -m scripts.resync_site_service_request_links --ticket 784 --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402
from app.infrastructure.db.session import session_scope  # noqa: E402
from app.models.site_service_requests import SiteServiceRequestCase  # noqa: E402
from app.services.expertise_bitrix import BitrixRestClient  # noqa: E402
from app.services.site_service_requests_worker import (  # noqa: E402
    SiteServiceRequestBitrixApi,
    SiteServiceRequestBitrixReader,
    SiteServiceRequestBitrixWriter,
    resolved_site_service_request_field_map,
)

UNRESOLVED_STATUSES = ("client_match_required", "order_match_required", "order_not_found")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{9,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _contact_parts(raw: str) -> tuple[str | None, str | None]:
    phone = _PHONE_RE.search(raw or "")
    email = _EMAIL_RE.search(raw or "")
    return (phone.group(0).strip() if phone else None, email.group(0) if email else None)


def _item(reader_api: SiteServiceRequestBitrixApi, *, settings: Settings, item_id: int) -> dict:
    response = reader_api.call(
        "crm.item.get",
        [
            ("entityTypeId", str(settings.site_service_requests_bitrix_entity_type_id)),
            ("id", str(item_id)),
        ],
    )
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("bitrix_item_readback_invalid")
    item = result.get("item")
    if not isinstance(item, dict):
        raise RuntimeError("bitrix_item_readback_invalid")
    return item


def _field(item: dict, field_map: dict[str, str], key: str) -> Any:
    upper = field_map[key]
    parts = upper.lower().split("_")
    camel = parts[0] + "".join(part.capitalize() for part in parts[1:])
    for name in (upper, camel, upper.lower()):
        if name in item:
            return item[name]
    return None


def resync(
    session: Session,
    *,
    settings: Settings,
    reader: SiteServiceRequestBitrixReader,
    writer: SiteServiceRequestBitrixWriter,
    api: SiteServiceRequestBitrixApi,
    apply: bool,
    ticket_id: int | None,
) -> list[dict[str, Any]]:
    field_map = resolved_site_service_request_field_map(settings)
    query = select(SiteServiceRequestCase).where(
        SiteServiceRequestCase.bitrix_item_id.is_not(None),
        SiteServiceRequestCase.sync_status.in_(UNRESOLVED_STATUSES),
    )
    if ticket_id is not None:
        query = query.where(SiteServiceRequestCase.source_ticket_id == ticket_id)

    results: list[dict[str, Any]] = []
    for case in session.scalars(query.order_by(SiteServiceRequestCase.source_ticket_id)).all():
        item_id = int(case.bitrix_item_id)
        item = _item(api, settings=settings, item_id=item_id)
        raw_contact = str(_field(item, field_map, "customer_contact") or "")
        order_number = str(_field(item, field_map, "order_refs") or "")
        phone, email = _contact_parts(raw_contact)

        row: dict[str, Any] = {
            "ticketId": case.source_ticket_id,
            "itemId": item_id,
            "status": case.sync_status,
            "orderNumber": order_number,
            "resolved": False,
        }
        if not phone and not email:
            row["reason"] = "no_contact_data"
            results.append(row)
            continue

        contact = reader.find_contact(phone=phone or "", email=email)
        contact, order = reader.resolve_contact_and_order(
            contact=contact,
            order_number=order_number,
            order_field=settings.site_service_requests_crm_order_field,
        )
        row["contactStatus"] = contact.status
        row["orderStatus"] = order.status
        row["contactId"] = contact.contact_id
        row["dealId"] = order.deal_id

        if contact.status not in {"matched", "created"} or order.status != "matched":
            row["reason"] = "still_ambiguous"
            results.append(row)
            continue

        row["resolved"] = True
        if not apply:
            results.append(row)
            continue

        writer.update_item_fields(
            entity_type_id=settings.site_service_requests_bitrix_entity_type_id,
            item_id=item_id,
            fields={
                field_map["crm_contact"]: contact.contact_id,
                field_map["crm_deal"]: order.deal_id,
                field_map["site_sync_status"]: (
                    settings.site_service_requests_bitrix_enum_map or {}
                ).get("sync_status_synced"),
                field_map["site_sync_error"]: None,
            },
        )
        case.crm_contact_id = contact.contact_id
        case.crm_company_id = contact.company_id
        case.crm_deal_id = order.deal_id
        case.sync_status = "synced"
        case.base_sync_status = "synced"
        case.last_error_code = None
        session.commit()
        results.append(row)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Записать найденные связи.")
    parser.add_argument("--ticket", type=int, help="Только один тикет сайта.")
    args = parser.parse_args()

    settings = get_settings()
    api = BitrixRestClient(
        str(settings.site_service_requests_bitrix_webhook_url),
        retry_transient_html_403=True,
    )
    reader = SiteServiceRequestBitrixReader(api)
    writer = SiteServiceRequestBitrixWriter(api)

    with session_scope(read_only=not args.apply) as session:
        rows = resync(
            session,
            settings=settings,
            reader=reader,
            writer=writer,
            api=api,
            apply=args.apply,
            ticket_id=args.ticket,
        )

    resolved = [row for row in rows if row["resolved"]]
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "checked": len(rows),
                "resolved": len(resolved),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
