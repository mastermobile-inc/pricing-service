from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.api.dependencies import get_db
from app.core.config import Settings, get_settings
from app.main import app
from app.models.site_service_requests import SiteServiceRequestCase
from app.services.site_service_requests_ui_auth import (
    SiteServiceRequestUiSession,
    require_site_service_request_ui_session,
)


@contextmanager
def _ui_dependencies(
    db_session,
    *,
    item_id: int = 391,
    replies_enabled: bool = True,
    attachments_enabled: bool = True,
    user_id: int = 131016,
    write_allowed_user_ids: list[int] | None = None,
    stage_map: dict[str, str] | None = None,
):
    settings = Settings(
        site_service_requests_ui_enabled=True,
        site_service_requests_ui_replies_enabled=replies_enabled,
        site_service_requests_outbound_replies_enabled=True,
        site_service_requests_command_attachments_enabled=attachments_enabled,
        site_service_requests_ui_write_allowed_user_ids=(
            [user_id] if write_allowed_user_ids is None else write_allowed_user_ids
        ),
        site_service_requests_event_encryption_key=base64.urlsafe_b64encode(b"u" * 32).decode(
            "ascii"
        ),
        site_service_requests_bitrix_stage_map=stage_map or {},
    )

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_site_service_request_ui_session] = lambda: (
        SiteServiceRequestUiSession(
            domain="portal.example",
            member_id="member-3223",
            user_id=user_id,
            user_name="Тимур Тибилов",
            is_admin=False,
            item_id=item_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def test_ui_reply_note_conversation_and_item_scope(client, db_session):
    case = SiteServiceRequestCase(
        source_ticket_id=760,
        source_kind="site_ticket",
        source_key="site-support-ticket:760",
        bitrix_item_id=391,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    with _ui_dependencies(db_session):
        reply = client.post(
            "/api/site-service-requests/ui/items/391/replies",
            data={"clientRequestId": "request-3223", "text": "Ответ клиенту"},
            files={"files": ("answer.txt", b"answer", "text/plain")},
        )
        duplicate = client.post(
            "/api/site-service-requests/ui/items/391/replies",
            data={"clientRequestId": "request-3223", "text": "Ответ клиенту"},
            files={"files": ("answer.txt", b"answer", "text/plain")},
        )
        conflict = client.post(
            "/api/site-service-requests/ui/items/391/replies",
            data={"clientRequestId": "request-3223", "text": "Другой ответ"},
            files={"files": ("answer.txt", b"answer", "text/plain")},
        )
        note = client.post(
            "/api/site-service-requests/ui/items/391/notes",
            json={"clientRequestId": "note-3223", "text": "Заметка для коллег"},
        )
        conversation = client.get("/api/site-service-requests/ui/items/391/conversation")
        forbidden = client.get("/api/site-service-requests/ui/items/392/conversation")

    assert reply.status_code == 200
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    assert conflict.status_code == 409
    assert note.status_code == 200
    assert conversation.status_code == 200
    assert conversation.json()["ticketId"] == 760
    assert conversation.json()["canReply"] is True
    assert conversation.json()["canAttachFiles"] is True
    assert [row["direction"] for row in conversation.json()["messages"]] == [
        "outbound",
        "internal",
    ]
    assert forbidden.status_code == 403


def test_ui_read_only_gate_blocks_all_mutations(client, db_session):
    case = SiteServiceRequestCase(
        source_ticket_id=761,
        source_kind="site_ticket",
        source_key="site-support-ticket:761",
        bitrix_item_id=392,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    with _ui_dependencies(db_session, item_id=392, replies_enabled=False):
        conversation = client.get("/api/site-service-requests/ui/items/392/conversation")
        reply = client.post(
            "/api/site-service-requests/ui/items/392/replies",
            data={"clientRequestId": "request-readonly", "text": "Не отправлять"},
        )
        note = client.post(
            "/api/site-service-requests/ui/items/392/notes",
            json={"clientRequestId": "note-readonly", "text": "Не сохранять"},
        )
        retry = client.post("/api/site-service-requests/ui/items/392/replies/1/retry")

    assert conversation.status_code == 200
    assert conversation.json()["canReply"] is False
    assert conversation.json()["canAttachFiles"] is False
    for response in (reply, note, retry):
        assert response.status_code == 503
        assert response.json()["detail"] == "ui_replies_disabled"


def test_ui_write_allowlist_is_fail_closed_for_all_mutations(client, db_session):
    case = SiteServiceRequestCase(
        source_ticket_id=762,
        source_kind="site_ticket",
        source_key="site-support-ticket:762",
        bitrix_item_id=393,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    with _ui_dependencies(
        db_session,
        item_id=393,
        write_allowed_user_ids=[],
    ):
        conversation = client.get("/api/site-service-requests/ui/items/393/conversation")
        reply = client.post(
            "/api/site-service-requests/ui/items/393/replies",
            data={"clientRequestId": "request-forbidden", "text": "Не отправлять"},
        )
        note = client.post(
            "/api/site-service-requests/ui/items/393/notes",
            json={"clientRequestId": "note-forbidden", "text": "Не сохранять"},
        )
        retry = client.post("/api/site-service-requests/ui/items/393/replies/1/retry")

    assert conversation.status_code == 200
    assert conversation.json()["canReply"] is False
    assert conversation.json()["canAttachFiles"] is False
    for response in (reply, note, retry):
        assert response.status_code == 403
        assert response.json()["detail"] == "ui_write_not_allowed"


def test_ui_admin_and_timur_can_write_but_files_have_separate_capability(client, db_session):
    case = SiteServiceRequestCase(
        source_ticket_id=763,
        source_kind="site_ticket",
        source_key="site-support-ticket:763",
        bitrix_item_id=394,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    with _ui_dependencies(
        db_session,
        item_id=394,
        attachments_enabled=False,
        user_id=115204,
        write_allowed_user_ids=[115204, 131016],
    ):
        conversation = client.get("/api/site-service-requests/ui/items/394/conversation")
        reply = client.post(
            "/api/site-service-requests/ui/items/394/replies",
            data={"clientRequestId": "request-text-only", "text": "Текстовый ответ"},
        )
        reply_with_file = client.post(
            "/api/site-service-requests/ui/items/394/replies",
            data={"clientRequestId": "request-with-file", "text": "Ответ с файлом"},
            files={"files": ("answer.txt", b"answer", "text/plain")},
        )
        note = client.post(
            "/api/site-service-requests/ui/items/394/notes",
            json={"clientRequestId": "note-by-admin", "text": "Заметка администратора"},
        )

    assert conversation.status_code == 200
    assert conversation.json()["canReply"] is True
    assert conversation.json()["canAttachFiles"] is False
    assert reply.status_code == 200
    assert reply_with_file.status_code == 422
    assert reply_with_file.json()["detail"] == "command_attachments_disabled"
    assert note.status_code == 200


def test_ui_email_conversation_stays_on_timeline_channel(client, db_session):
    case = SiteServiceRequestCase(
        source_ticket_id=-764,
        source_kind="bitrix_mail",
        source_key="bitrix-mail:shop:764",
        bitrix_item_id=395,
        first_seen_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    with _ui_dependencies(db_session, item_id=395):
        conversation = client.get("/api/site-service-requests/ui/items/395/conversation")

    assert conversation.status_code == 200
    assert conversation.json()["sourceKind"] == "bitrix_mail"
    assert conversation.json()["ticketId"] is None
    assert conversation.json()["canReply"] is False
    assert conversation.json()["canAttachFiles"] is False


def _service_request_link(item_id: int = 391):
    from app.services.customer_returns import CustomerReturnServiceRequestLink

    return CustomerReturnServiceRequestLink(
        item_id=item_id,
        title=f"Тикет сайта #{item_id}",
        stage_id="DT1134_55:PREPARATION",
        stage_name="В работе / уточнение",
        closed=False,
        deal_id=None,
        order_ref="236342",
        site_ticket_id="746",
        responsible_user_id=131016,
        responsible_name="Тимур Тибилов",
    )


def test_returns_tab_registers_and_links_return_to_the_card(client, db_session, monkeypatch):
    """Возврат из карточки попадает в реестр уже привязанным к обращению."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )

    with _ui_dependencies(db_session):
        empty = client.get("/api/site-service-requests/ui/items/391/returns")
        created = client.post(
            "/api/site-service-requests/ui/items/391/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-UI-3223"},
        )
        listed = client.get("/api/site-service-requests/ui/items/391/returns")

    assert empty.status_code == 200
    assert empty.json() == {"canRegister": True, "returns": []}
    assert created.status_code == 201
    shipment = created.json()["returns"][0]
    assert shipment["tracking_number"] == "CDEK-UI-3223"
    assert shipment["status"] == "registered"
    assert shipment["bitrix_case_id"] == "391"
    assert shipment["site_ticket_id"] == "746"
    assert shipment["service_request_item_id"] == 391
    assert listed.json()["returns"][0]["id"] == shipment["id"]


def test_returns_tab_does_not_duplicate_the_same_parcel(client, db_session, monkeypatch):
    """Повторная регистрация того же трека не заводит вторую запись."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )

    with _ui_dependencies(db_session):
        first = client.post(
            "/api/site-service-requests/ui/items/391/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-UI-DOUBLE"},
        )
        second = client.post(
            "/api/site-service-requests/ui/items/391/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-UI-DOUBLE"},
        )

    assert first.status_code == 201
    assert second.status_code == 200
    assert len(second.json()["returns"]) == 1


def test_returns_tab_is_read_only_without_write_rights(client, db_session, monkeypatch):
    """Без права записи вкладка показывает возвраты, но не даёт их заводить."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )

    with _ui_dependencies(db_session, write_allowed_user_ids=[]):
        listed = client.get("/api/site-service-requests/ui/items/391/returns")
        denied = client.post(
            "/api/site-service-requests/ui/items/391/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-UI-READONLY"},
        )

    assert listed.status_code == 200
    assert listed.json()["canRegister"] is False
    assert denied.status_code == 403


def test_returns_tab_keeps_other_cards_out_of_scope(client, db_session, monkeypatch):
    """Вкладка одной карточки не показывает и не заводит возвраты другой."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )

    with _ui_dependencies(db_session):
        foreign_list = client.get("/api/site-service-requests/ui/items/392/returns")
        foreign_create = client.post(
            "/api/site-service-requests/ui/items/392/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-UI-FOREIGN"},
        )

    assert foreign_list.status_code == 403
    assert foreign_create.status_code == 403


def _order_status(**overrides):
    from app.services.site_service_request_order_status import SiteServiceRequestOrderStatus

    values = {
        "deal_id": 33485,
        "order_ref": "240315",
        "tracking": "10311127882",
        "status_text": "Вручен 26.08.2026 14:58",
        "tracking_link": "https://www.cdek.ru/ru/tracking/?order_id=10311127882",
        "planned_delivery_date": None,
        "storage_date": None,
        "multiple_shipments": False,
        "customer_message": "Здравствуйте! По вашему заказу №240315: Вручен 26.08.2026 14:58.",
    }
    values.update(overrides)
    return SiteServiceRequestOrderStatus(**values)


def test_order_status_is_read_from_the_linked_deal(client, db_session, monkeypatch):
    """Кнопка «Где заказ» берёт сделку из карточки и отдаёт готовый текст."""

    from app.api import site_service_requests_ui as ui_module

    db_session.add(
        SiteServiceRequestCase(
            source_ticket_id=746,
            first_seen_at=datetime.now(UTC),
            bitrix_item_id=391,
            crm_deal_id=33485,
            assignment_state="waiting",
            round_robin_seq=0,
            sync_status="synced",
        )
    )
    db_session.commit()
    captured: dict[str, int] = {}

    def _fake(*, settings, deal_id):
        captured["dealId"] = deal_id
        return _order_status()

    monkeypatch.setattr(
        ui_module.order_status_service,
        "get_site_service_request_order_status",
        _fake,
    )

    with _ui_dependencies(db_session):
        response = client.get("/api/site-service-requests/ui/items/391/order-status")

    assert response.status_code == 200
    assert captured["dealId"] == 33485
    body = response.json()
    assert body["dealId"] == 33485
    assert body["multipleShipments"] is False
    assert body["customerMessage"].startswith("Здравствуйте! По вашему заказу №240315")


def test_order_status_without_a_known_deal_is_not_found(client, db_session, monkeypatch):
    """Заказ не привязан ни в базе, ни в карточке — подставлять нечего."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )

    with _ui_dependencies(db_session):
        response = client.get("/api/site-service-requests/ui/items/391/order-status")

    assert response.status_code == 404
    assert response.json()["detail"] == "order_status_deal_unknown"


def test_order_status_survives_an_unavailable_bitrix(client, db_session, monkeypatch):
    """Недоступный портал — это «попробуйте позже», а не ошибка карточки."""

    from app.api import site_service_requests_ui as ui_module

    db_session.add(
        SiteServiceRequestCase(
            source_ticket_id=747,
            first_seen_at=datetime.now(UTC),
            bitrix_item_id=391,
            crm_deal_id=33485,
            assignment_state="waiting",
            round_robin_seq=0,
            sync_status="synced",
        )
    )
    db_session.commit()

    def _unavailable(**_kwargs):
        raise ui_module.order_status_service.SiteServiceRequestOrderStatusUnavailable("нет связи")

    monkeypatch.setattr(
        ui_module.order_status_service,
        "get_site_service_request_order_status",
        _unavailable,
    )

    with _ui_dependencies(db_session):
        response = client.get("/api/site-service-requests/ui/items/391/order-status")

    assert response.status_code == 503
    assert response.json()["detail"] == "order_status_unavailable"


def test_order_status_is_refused_for_another_card(client, db_session):
    """Сессия открыта на одну карточку — чужой заказ через неё не посмотреть."""

    with _ui_dependencies(db_session, item_id=391):
        response = client.get("/api/site-service-requests/ui/items/999/order-status")

    assert response.status_code == 403


def _goods_stage_settings_override(monkeypatch, *, moved: list):
    """Подменяет перевод стадии, чтобы не ходить в Битрикс из теста."""

    from app.api import site_service_requests_ui as ui_module

    def _move(*, settings, item_id, stage_id):
        moved.append((item_id, stage_id))

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "move_service_request_stage",
        _move,
    )


def test_registering_a_return_moves_the_card_to_waiting_for_goods(client, db_session, monkeypatch):
    """Возврат заведён — карточка уходит ждать посылку, а не ответ клиента."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )
    moved: list = []
    _goods_stage_settings_override(monkeypatch, moved=moved)
    stage_map = {
        "new": "DT1134_55:NEW",
        "work": "DT1134_55:PREPARATION",
        "client": "DT1134_55:CLIENT",
        "goods": "DT1134_55:CLIENT_2",
    }

    with _ui_dependencies(db_session, stage_map=stage_map):
        created = client.post(
            "/api/site-service-requests/ui/items/391/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-GOODS-1"},
        )

    assert created.status_code == 201
    assert moved == [(391, "DT1134_55:CLIENT_2")]


def test_return_registration_survives_a_failed_stage_move(client, db_session, monkeypatch):
    """Битрикс не ответил — возврат всё равно зарегистрирован, без ложной ошибки."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )

    def _boom(**_kwargs):
        raise RuntimeError("bitrix is down")

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "move_service_request_stage",
        _boom,
    )
    stage_map = {"work": "DT1134_55:PREPARATION", "goods": "DT1134_55:CLIENT_2"}

    with _ui_dependencies(db_session, stage_map=stage_map):
        created = client.post(
            "/api/site-service-requests/ui/items/391/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-GOODS-2"},
        )

    assert created.status_code == 201
    assert created.json()["returns"][0]["tracking_number"] == "CDEK-GOODS-2"


def test_return_registration_does_not_move_the_stage_until_it_exists(
    client, db_session, monkeypatch
):
    """Стадии «Ожидаем товар» в портале ещё нет — карточку не трогаем."""

    from app.api import site_service_requests_ui as ui_module

    monkeypatch.setattr(
        ui_module.customer_return_request_service,
        "get_customer_return_service_request",
        lambda **_kwargs: _service_request_link(),
    )
    moved: list = []
    _goods_stage_settings_override(monkeypatch, moved=moved)

    with _ui_dependencies(db_session):
        created = client.post(
            "/api/site-service-requests/ui/items/391/returns",
            json={"carrier": "cdek", "trackingNumber": "CDEK-GOODS-3"},
        )

    assert created.status_code == 201
    assert moved == []
