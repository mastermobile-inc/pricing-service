from __future__ import annotations

from datetime import UTC, datetime

from scripts.backfill_site_service_awaiting_reply import awaiting_since_from_history


class _Message:
    def __init__(self, author_kind: str, at: datetime, *, visible: bool = True):
        self.author_kind = author_kind
        self.created_at = at
        self.is_visible_to_customer = visible


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=UTC)


def test_backfill_marks_the_customer_message_after_our_answer() -> None:
    """Клиент написал последним — берём его первое сообщение после нашего ответа."""

    messages = [
        _Message("customer", _at(11, 10)),
        _Message("support-team", _at(11, 15)),
        _Message("customer", _at(11, 20)),
        _Message("customer", _at(12, 9)),
    ]

    assert awaiting_since_from_history(messages) == _at(11, 20)


def test_backfill_skips_cards_answered_last() -> None:
    """Последним ответили мы — ждать нечего."""

    messages = [
        _Message("customer", _at(11, 10)),
        _Message("support-team", _at(11, 15)),
    ]

    assert awaiting_since_from_history(messages) is None


def test_backfill_leaves_untouched_cards_without_a_first_answer() -> None:
    """Первого ответа не было: этой паузой управляет срок первого ответа."""

    messages = [
        _Message("customer", _at(11, 10)),
        _Message("customer", _at(11, 20)),
    ]

    assert awaiting_since_from_history(messages) is None


def test_backfill_ignores_internal_notes() -> None:
    """Скрытая заметка клиенту не видна и ожидание не снимает."""

    messages = [
        _Message("customer", _at(11, 10)),
        _Message("support-team", _at(11, 15)),
        _Message("customer", _at(11, 20)),
        _Message("support-team", _at(11, 22), visible=False),
    ]

    assert awaiting_since_from_history(messages) == _at(11, 20)
