from __future__ import annotations

from datetime import date

import pytest

from app.core.config import Settings
from app.services.site_order_fulfillment import BitrixChatError
from app.services.site_service_request_order_status import (
    SiteServiceRequestOrderStatusNotFound,
    SiteServiceRequestOrderStatusUnavailable,
    get_site_service_request_order_status,
)

_TODAY = date(2026, 9, 16)


class _FakeCrm:
    """Битрикс, отвечающий заранее заданной сделкой."""

    def __init__(self, deal: dict | None, *, error: bool = False) -> None:
        self.deal = deal
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self.error:
            raise BitrixChatError("crm.deal.get: timeout")
        return {"result": self.deal}


def _settings() -> Settings:
    return Settings(site_service_requests_bitrix_webhook_url="https://portal.example/rest/1/t/")


def _deal(**overrides) -> dict:
    values = {
        "ID": "33485",
        "UF_CRM_OT_TRACKING": "10311127882",
        "UF_CRM_OT_STATUS": "Вручен 26.08.2026 14:58",
        "UF_CRM_OT_TRACKING_LINK": "https://www.cdek.ru/ru/tracking/?order_id=10311127882",
        "UF_CRM_OT_PLANNED_DELIVERY_DATE": "26.08.2026",
        "UF_CRM_OT_STORAGE_DATE": "02.09.2026",
        "UF_CRM_1772784329053": "240315",
    }
    values.update(overrides)
    return values


def _status(deal: dict | None, **kwargs):
    return get_site_service_request_order_status(
        settings=_settings(),
        deal_id=33485,
        client=_FakeCrm(deal, **kwargs),
        today=_TODAY,
    )


def test_message_uses_the_order_number_status_and_tracking() -> None:
    """Готовый текст собирает всё, что клиент спрашивает в «где мой заказ»."""

    status = _status(_deal())

    assert status.customer_message == (
        "Здравствуйте! По вашему заказу №240315: Вручен 26.08.2026 14:58.\n"
        "Отслеживание: 10311127882 — https://www.cdek.ru/ru/tracking/?order_id=10311127882"
    )
    assert status.multiple_shipments is False


def test_past_delivery_dates_are_left_out() -> None:
    """«Ожидаемая доставка 26.08» под статусом «Вручен» только путает клиента."""

    status = _status(_deal())

    assert status.planned_delivery_date is None
    assert status.storage_date is None
    assert "Ожидаемая дата доставки" not in status.customer_message


def test_future_dates_are_shown() -> None:
    """Пока посылка едет, даты доставки и хранения клиенту нужны."""

    status = _status(
        _deal(
            UF_CRM_OT_STATUS="Обработка. Покинуло сортировочный центр",
            UF_CRM_OT_PLANNED_DELIVERY_DATE="20.09.2026",
            UF_CRM_OT_STORAGE_DATE="27.09.2026",
        )
    )

    assert "Ожидаемая дата доставки: 20.09.2026." in status.customer_message
    assert "Заказ хранится в пункте выдачи до 27.09.2026." in status.customer_message


def test_status_without_tracking_still_makes_a_message() -> None:
    status = _status(_deal(UF_CRM_OT_TRACKING="", UF_CRM_OT_TRACKING_LINK=""))

    assert status.customer_message.startswith("Здравствуйте! По вашему заказу №240315:")
    assert "Отслеживание" not in status.customer_message


def test_tracking_without_status_still_makes_a_message() -> None:
    status = _status(_deal(UF_CRM_OT_STATUS=""))

    assert "есть отправление." in status.customer_message
    assert "Отслеживание: 10311127882" in status.customer_message


def test_empty_shipment_fields_give_no_message() -> None:
    """Без статуса и трека подсказка была бы бессодержательной отпиской."""

    status = _status(_deal(UF_CRM_OT_STATUS="", UF_CRM_OT_TRACKING="", UF_CRM_OT_TRACKING_LINK=""))

    assert status.customer_message == ""


def test_several_trackings_block_the_message() -> None:
    """Общий трек на несколько отправлений клиенту отправлять нельзя."""

    status = _status(_deal(UF_CRM_OT_TRACKING="10311127882, 80223624331954"))

    assert status.multiple_shipments is True
    assert status.customer_message == ""


def test_tracking_as_a_list_is_supported() -> None:
    status = _status(_deal(UF_CRM_OT_TRACKING=["10311127882"]))

    assert status.multiple_shipments is False
    assert status.tracking == "10311127882"


def test_message_never_leaks_internal_identifiers() -> None:
    """В тексте клиенту нет ни кодов полей, ни идентификатора сделки."""

    status = _status(_deal())

    assert "UF_CRM" not in status.customer_message
    assert "33485" not in status.customer_message


def test_unknown_date_format_is_passed_through() -> None:
    """Непонятный формат не выдумываем, отдаём как есть."""

    status = _status(_deal(UF_CRM_OT_PLANNED_DELIVERY_DATE="на следующей неделе"))

    assert status.planned_delivery_date == "на следующей неделе"


def test_missing_deal_is_not_found() -> None:
    with pytest.raises(SiteServiceRequestOrderStatusNotFound):
        _status(None)


def test_unavailable_bitrix_is_reported_separately() -> None:
    """Недоступный портал — это «попробуйте позже», а не «сделки нет»."""

    with pytest.raises(SiteServiceRequestOrderStatusUnavailable):
        _status(_deal(), error=True)


def test_unconfigured_webhook_is_unavailable() -> None:
    with pytest.raises(SiteServiceRequestOrderStatusUnavailable):
        get_site_service_request_order_status(settings=Settings(), deal_id=33485)
