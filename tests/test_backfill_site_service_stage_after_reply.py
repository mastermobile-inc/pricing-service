from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.models.customer_return import CustomerReturnShipment
from app.models.site_service_requests import (
    SiteServiceRequestCase,
    SiteServiceRequestCommand,
    SiteServiceRequestMessage,
)
from scripts.backfill_site_service_stage_after_reply import plan_stage_backfill

_NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


class _FakeWriter:
    """Минимальный Битрикс: хранит стадию карточки и записанные комментарии."""

    def __init__(self, stages: dict[int, str]) -> None:
        self.stages = dict(stages)
        self.comments: list[str] = []

    def get_item(self, *, entity_type_id: int, item_id: int) -> dict[str, Any]:
        return {"id": str(item_id), "stageId": self.stages[item_id]}

    def update_item_fields(
        self, *, entity_type_id: int, item_id: int, fields: dict[str, Any]
    ) -> dict[str, Any]:
        self.stages[item_id] = fields["stageId"]
        return {"id": str(item_id), "stageId": self.stages[item_id]}

    def add_timeline_comment(self, *, entity_type_id: int, item_id: int, comment: str) -> None:
        self.comments.append(comment)


def _settings() -> Settings:
    return Settings(
        site_service_requests_bitrix_entity_type_id=1134,
        site_service_requests_bitrix_stage_map={
            "new": "DT1134_55:NEW",
            "work": "DT1134_55:PREPARATION",
            "client": "DT1134_55:CLIENT",
            "success": "DT1134_55:SUCCESS",
            "failure": "DT1134_55:FAIL",
        },
    )


def _answered_case(db_session, *, item_id: int, ticket_id: int) -> SiteServiceRequestCase:
    case = SiteServiceRequestCase(
        source_ticket_id=ticket_id,
        first_seen_at=_NOW,
        first_response_at=_NOW,
        bitrix_item_id=item_id,
        assignment_state="waiting",
        round_robin_seq=0,
        sync_status="synced",
    )
    db_session.add(case)
    db_session.commit()
    db_session.add(
        SiteServiceRequestMessage(
            case_id=case.id,
            source_message_id=ticket_id,
            message_kind="site_message",
            direction="outbound",
            author_kind="support",
            is_visible_to_customer=True,
            text_sha256="0" * 64,
            created_at=_NOW,
        )
    )
    db_session.commit()
    return case


def test_answered_card_in_work_moves_to_waiting_for_the_customer(db_session) -> None:
    """Ответ отправлен, стадия осталась «В работе» — переводим в «Ожидаем клиента»."""

    _answered_case(db_session, item_id=700, ticket_id=700)
    writer = _FakeWriter({700: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=True)

    assert [row["ticketId"] for row in plan["moved"]] == [700]
    assert plan["review"] == []
    assert writer.stages[700] == "DT1134_55:CLIENT"
    assert any("site-service-stage-backfill" in text for text in writer.comments)


def test_dry_run_changes_nothing(db_session) -> None:
    """Без --apply скрипт только показывает список."""

    _answered_case(db_session, item_id=701, ticket_id=701)
    writer = _FakeWriter({701: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=False)

    assert [row["applied"] for row in plan["moved"]] == [False]
    assert writer.stages[701] == "DT1134_55:PREPARATION"
    assert writer.comments == []


def test_card_already_waiting_for_the_customer_is_skipped(db_session) -> None:
    """Стадия уже правильная — в списке её нет."""

    _answered_case(db_session, item_id=702, ticket_id=702)
    writer = _FakeWriter({702: "DT1134_55:CLIENT"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=True)

    assert plan == {"moved": [], "review": []}


def test_internal_note_sends_the_card_to_manual_review(db_session) -> None:
    """Коллеги что-то обсуждали внутри — стадию мог поставить человек осознанно."""

    case = _answered_case(db_session, item_id=703, ticket_id=703)
    db_session.add(
        SiteServiceRequestMessage(
            case_id=case.id,
            source_message_id=9703,
            message_kind="internal_note",
            direction="internal",
            author_kind="support",
            is_visible_to_customer=False,
            text_sha256="0" * 64,
            created_at=_NOW,
        )
    )
    db_session.commit()
    writer = _FakeWriter({703: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=True)

    assert plan["moved"] == []
    assert [row["reason"] for row in plan["review"]] == ["internal_note"]
    assert writer.stages[703] == "DT1134_55:PREPARATION"


def test_open_return_sends_the_card_to_manual_review(db_session) -> None:
    """По карточке едет возврат — ждём товар, а не ответ клиента."""

    _answered_case(db_session, item_id=704, ticket_id=704)
    db_session.add(
        CustomerReturnShipment(
            carrier="cdek",
            tracking_number="CDEK-704",
            status="in_transit",
            status_changed_at=_NOW,
            source="service_request_card",
            service_request_item_id=704,
            updated_at=_NOW,
        )
    )
    db_session.commit()
    writer = _FakeWriter({704: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=True)

    assert [row["reason"] for row in plan["review"]] == ["open_return"]
    assert writer.stages[704] == "DT1134_55:PREPARATION"


def test_finished_return_does_not_block_the_card(db_session) -> None:
    """Товар уже забрали — возврат закрыт и стадию держать незачем."""

    _answered_case(db_session, item_id=705, ticket_id=705)
    db_session.add(
        CustomerReturnShipment(
            carrier="cdek",
            tracking_number="CDEK-705",
            status="picked_up",
            status_changed_at=_NOW,
            source="service_request_card",
            service_request_item_id=705,
            updated_at=_NOW,
        )
    )
    db_session.commit()
    writer = _FakeWriter({705: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=True)

    assert [row["ticketId"] for row in plan["moved"]] == [705]
    assert writer.stages[705] == "DT1134_55:CLIENT"


def test_pending_outbound_command_sends_the_card_to_manual_review(db_session) -> None:
    """Ответ ещё не ушёл клиенту — рано объявлять, что мы ждём его."""

    case = _answered_case(db_session, item_id=706, ticket_id=706)
    db_session.add(
        SiteServiceRequestCommand(
            case_id=case.id,
            command_key="cmd-706",
            reply_encrypted=b"x",
            reply_sha256="0" * 64,
            status="pending",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    db_session.commit()
    writer = _FakeWriter({706: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=True)

    assert [row["reason"] for row in plan["review"]] == ["outbound_command_pending"]


def test_card_without_a_visible_reply_is_never_touched(db_session) -> None:
    """Ответа клиенту не было — это работа гейта первого ответа, не наша."""

    case = SiteServiceRequestCase(
        source_ticket_id=707,
        first_seen_at=_NOW,
        first_response_at=_NOW,
        bitrix_item_id=707,
        assignment_state="waiting",
        round_robin_seq=0,
        sync_status="synced",
    )
    db_session.add(case)
    db_session.commit()
    writer = _FakeWriter({707: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(db_session, settings=_settings(), writer=writer, apply=True)

    assert plan == {"moved": [], "review": []}


def test_ticket_filter_limits_the_run(db_session) -> None:
    """`--ticket` разбирает одно обращение, остальные не трогает."""

    _answered_case(db_session, item_id=708, ticket_id=708)
    _answered_case(db_session, item_id=709, ticket_id=709)
    writer = _FakeWriter({708: "DT1134_55:PREPARATION", 709: "DT1134_55:PREPARATION"})

    plan = plan_stage_backfill(
        db_session, settings=_settings(), writer=writer, apply=True, ticket_id=709
    )

    assert [row["ticketId"] for row in plan["moved"]] == [709]
    assert writer.stages[708] == "DT1134_55:PREPARATION"


def test_missing_stage_map_stops_the_script(db_session) -> None:
    """Без настроенных стадий скрипт не угадывает, а останавливается."""

    with pytest.raises(SystemExit):
        plan_stage_backfill(
            db_session,
            settings=Settings(site_service_requests_bitrix_stage_map={}),
            writer=_FakeWriter({}),
            apply=False,
        )
