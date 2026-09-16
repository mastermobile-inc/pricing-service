"""Статус доставки заказа для ответа клиенту из карточки обращения.

Сорок процентов обращений — вопрос «где мой заказ». Ответ на него целиком лежит
в сделке: его заполняет приложение «Отправки», а сотрудник каждый раз открывает
сделку, выбирает нужные строчки и перепечатывает их руками. Здесь тот же текст
собирается один раз и одинаково для всех.

Текст готовится на бэкенде намеренно: формат один, он покрыт тестами, и в него
не может случайно попасть внутренний код поля или идентификатор сделки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.core.config import Settings
from app.services.site_order_fulfillment import BitrixChatClient, BitrixChatError

# Поля приложения «Отправки» в сделке (otpravkee.me). Проверены на боевом портале:
# все строковые, даты приходят уже в виде «дд.мм.гггг».
DEAL_FIELDS = {
    "tracking": "UF_CRM_OT_TRACKING",
    "status": "UF_CRM_OT_STATUS",
    "tracking_link": "UF_CRM_OT_TRACKING_LINK",
    "planned_delivery_date": "UF_CRM_OT_PLANNED_DELIVERY_DATE",
    "storage_date": "UF_CRM_OT_STORAGE_DATE",
}
ORDER_REF_FIELD = "UF_CRM_1772784329053"

_TRACKING_SEPARATORS = re.compile(r"[\s,;/]+")


class SiteServiceRequestOrderStatusUnavailable(RuntimeError):
    """Битрикс временно недоступен — подсказку показать не из чего."""


class SiteServiceRequestOrderStatusNotFound(RuntimeError):
    """Сделки с таким идентификатором нет."""


@dataclass(frozen=True)
class SiteServiceRequestOrderStatus:
    deal_id: int
    order_ref: str | None
    tracking: str | None
    status_text: str | None
    tracking_link: str | None
    planned_delivery_date: str | None
    storage_date: str | None
    multiple_shipments: bool
    customer_message: str


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("value") or value.get("VALUE")
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _tracking_tokens(value: Any) -> list[str]:
    """Трек-номера одной строкой или списком приводятся к списку."""

    raw: list[str] = []
    if isinstance(value, list):
        raw = [str(item) for item in value if item is not None]
    elif value is not None:
        raw = [str(value)]
    tokens: list[str] = []
    for chunk in raw:
        tokens.extend(token for token in _TRACKING_SEPARATORS.split(chunk.strip()) if token)
    return tokens


def _future_date(value: str | None, *, today: date) -> str | None:
    """Дата «дд.мм.гггг», если она ещё не прошла, иначе ``None``.

    Прошедшая дата доставки в тексте клиенту только путает: «ожидаемая доставка
    26.08» под статусом «Вручен» выглядит как ошибка.
    """

    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        # Формат неизвестен — отдаём как есть, врать в текст нечем.
        return value
    return value if parsed >= today else None


def build_customer_message(
    *,
    order_ref: str | None,
    status_text: str | None,
    tracking: str | None,
    tracking_link: str | None,
    planned_delivery_date: str | None,
    storage_date: str | None,
) -> str:
    """Текст для клиента. Пустые данные строку не добавляют."""

    if not status_text and not tracking:
        # Ни статуса, ни трека: «Здравствуйте! По вашему заказу...» без
        # продолжения — бессодержательная отписка, лучше не предлагать её вовсе.
        return ""
    order_part = f" {order_ref}" if order_ref else ""
    head = f"Здравствуйте! По вашему заказу{order_part}"
    lines = [f"{head}: {status_text}." if status_text else f"{head} есть отправление."]
    if tracking:
        tracking_line = f"Отслеживание: {tracking}"
        if tracking_link:
            tracking_line += f" — {tracking_link}"
        lines.append(tracking_line)
    if planned_delivery_date:
        lines.append(f"Ожидаемая дата доставки: {planned_delivery_date}.")
    if storage_date:
        lines.append(f"Заказ хранится в пункте выдачи до {storage_date}.")
    return "\n".join(lines)


def _client(settings: Settings, client: BitrixChatClient | None) -> BitrixChatClient:
    if client is not None:
        return client
    webhook = _clean(settings.site_service_requests_bitrix_webhook_url)
    if not webhook:
        raise SiteServiceRequestOrderStatusUnavailable(
            "Bitrix24 order status lookup is not configured"
        )
    return BitrixChatClient(str(webhook), timeout=15.0)


def get_site_service_request_order_status(
    *,
    settings: Settings,
    deal_id: int,
    client: BitrixChatClient | None = None,
    today: date | None = None,
) -> SiteServiceRequestOrderStatus:
    crm = _client(settings, client)
    try:
        response = crm.call("crm.deal.get", {"id": deal_id})
    except BitrixChatError as exc:
        raise SiteServiceRequestOrderStatusUnavailable(
            "Bitrix24 is temporarily unavailable"
        ) from exc
    deal = response.get("result")
    if not isinstance(deal, dict) or not deal:
        raise SiteServiceRequestOrderStatusNotFound(f"Bitrix24 deal {deal_id} was not found")

    current_day = today or date.today()
    tokens = _tracking_tokens(deal.get(DEAL_FIELDS["tracking"]))
    tracking = tokens[0] if tokens else None
    multiple_shipments = len(tokens) > 1
    status_text = _clean(deal.get(DEAL_FIELDS["status"]))
    tracking_link = _clean(deal.get(DEAL_FIELDS["tracking_link"]))
    planned = _future_date(
        _clean(deal.get(DEAL_FIELDS["planned_delivery_date"])), today=current_day
    )
    storage = _future_date(_clean(deal.get(DEAL_FIELDS["storage_date"])), today=current_day)
    order_ref = _clean(deal.get(ORDER_REF_FIELD))

    # При нескольких отправлениях общий трек клиенту отправлять нельзя: он
    # относится только к одной посылке. Текст не собираем, UI покажет предупреждение.
    message = (
        ""
        if multiple_shipments
        else build_customer_message(
            order_ref=f"№{order_ref}" if order_ref else None,
            status_text=status_text,
            tracking=tracking,
            tracking_link=tracking_link,
            planned_delivery_date=planned,
            storage_date=storage,
        )
    )
    return SiteServiceRequestOrderStatus(
        deal_id=deal_id,
        order_ref=order_ref,
        tracking=tracking,
        status_text=status_text,
        tracking_link=tracking_link,
        planned_delivery_date=planned,
        storage_date=storage,
        multiple_shipments=multiple_shipments,
        customer_message=message,
    )
