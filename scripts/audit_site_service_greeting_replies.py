#!/usr/bin/env python3
"""Показывает, какие реальные ответы клиенту попадут под фильтр «пустых».

Фильтр отсекает сообщения, состоящие из одного приветствия, чтобы «Здравствуйте!»
не закрывало срок первого ответа. Порог подбирается по фактическим текстам, а не
на глаз, поэтому перед включением правки этот разбор обязателен.

Скрипт ничего не меняет и не пишет: только читает переписку и печатает разбор.

    python scripts/audit_site_service_greeting_replies.py
    python scripts/audit_site_service_greeting_replies.py --show-texts
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

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db.session import session_scope  # noqa: E402
from app.models.site_service_requests import (  # noqa: E402
    SiteServiceRequestCase,
    SiteServiceRequestMessage,
)
from app.services.site_service_request_conversations import (  # noqa: E402
    _decrypt_message_text,
)
from app.services.site_service_requests import (  # noqa: E402
    build_site_service_request_cipher,
)
from app.services.site_service_requests_worker import (  # noqa: E402
    SITE_SERVICE_MIN_REPLY_CHARS,
    _is_substantive_support_reply,
)

_PREVIEW_CHARS = 120


def audit(session: Session, *, cipher, show_texts: bool) -> dict[str, Any]:
    rows = session.execute(
        select(SiteServiceRequestMessage, SiteServiceRequestCase.source_ticket_id)
        .join(
            SiteServiceRequestCase, SiteServiceRequestCase.id == SiteServiceRequestMessage.case_id
        )
        .where(
            SiteServiceRequestMessage.direction == "outbound",
            SiteServiceRequestMessage.is_visible_to_customer.is_(True),
            SiteServiceRequestMessage.text_encrypted.is_not(None),
        )
        .order_by(SiteServiceRequestMessage.case_id, SiteServiceRequestMessage.created_at)
    ).all()

    total = 0
    empty: list[dict[str, Any]] = []
    first_by_case: dict[int, bool] = {}
    for message, ticket_id in rows:
        try:
            text = _decrypt_message_text(message, cipher=cipher)
        except Exception as exc:  # noqa: BLE001 — разбор не должен падать на одной строке
            empty.append({"ticketId": ticket_id, "error": type(exc).__name__})
            continue
        total += 1
        substantive = _is_substantive_support_reply(text)
        first_by_case.setdefault(message.case_id, substantive)
        if substantive:
            continue
        row: dict[str, Any] = {
            "ticketId": ticket_id,
            "createdAt": message.created_at.isoformat(),
            "chars": len(" ".join((text or "").split())),
        }
        if show_texts:
            row["text"] = " ".join((text or "").split())[:_PREVIEW_CHARS]
        empty.append(row)

    affected_cases = sum(1 for substantive in first_by_case.values() if not substantive)
    return {
        "threshold": SITE_SERVICE_MIN_REPLY_CHARS,
        "outboundMessages": total,
        "wouldBeTreatedAsEmpty": len(empty),
        "casesWhoseFirstReplyIsEmpty": affected_cases,
        "rows": empty,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show-texts",
        action="store_true",
        help="Печатать сами тексты (переписка с клиентами — не выводить в общий чат).",
    )
    args = parser.parse_args()

    settings = get_settings()
    cipher = build_site_service_request_cipher(settings)
    with session_scope(read_only=True) as session:
        report = audit(session, cipher=cipher, show_texts=args.show_texts)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
