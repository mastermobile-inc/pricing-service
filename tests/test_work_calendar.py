from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.work_calendar import (
    add_working_days,
    is_working_day,
    load_work_calendar,
    next_working_day,
)


def test_working_days_skip_weekends() -> None:
    """Пять рабочих дней от пятницы — это следующая пятница, а не среда."""

    assert add_working_days(datetime(2026, 9, 4, 9, 0, tzinfo=UTC), 5) == datetime(
        2026, 9, 11, 9, 0
    )


def test_working_days_skip_holidays() -> None:
    """12 июня 2026 — праздник, срок съезжает на понедельник 15-го."""

    assert add_working_days(datetime(2026, 6, 5, 9, 0, tzinfo=UTC), 5) == datetime(
        2026, 6, 15, 9, 0
    )


def test_naive_input_is_treated_as_utc() -> None:
    """В базе время лежит без таймзоны и в UTC — считаем так же."""

    assert add_working_days(datetime(2026, 9, 4, 9, 0), 1) == datetime(2026, 9, 7, 9, 0)


def test_time_of_day_is_preserved() -> None:
    """Сдвигаем дату, а не время: вечернее сообщение остаётся вечерним."""

    assert add_working_days(datetime(2026, 9, 4, 20, 30, tzinfo=UTC), 1) == datetime(
        2026, 9, 7, 20, 30
    )


def test_zero_days_returns_the_same_moment() -> None:
    assert add_working_days(datetime(2026, 9, 5, 9, 0, tzinfo=UTC), 0) == datetime(2026, 9, 5, 9, 0)


def test_working_weekend_counts_as_a_working_day() -> None:
    """Объявленная рабочей суббота считается наравне с буднями."""

    calendar = {"years": {"2026": {"holidays": [], "working_weekends": ["2026-09-05"]}}}

    assert is_working_day(date(2026, 9, 5), calendar=calendar) is True
    assert add_working_days(
        datetime(2026, 9, 4, 9, 0, tzinfo=UTC), 1, calendar=calendar
    ) == datetime(2026, 9, 5, 9, 0)


def test_next_working_day_returns_the_day_itself_when_it_works() -> None:
    assert next_working_day(date(2026, 9, 4)) == date(2026, 9, 4)
    assert next_working_day(date(2026, 9, 5)) == date(2026, 9, 7)


def test_unknown_year_raises_instead_of_guessing() -> None:
    """Переносы устанавливает постановление — вычислить их нельзя."""

    with pytest.raises(ValueError, match="2028"):
        add_working_days(datetime(2027, 12, 30, 9, 0, tzinfo=UTC), 5)


def test_calendar_covers_the_year_ahead() -> None:
    """Календарь должен быть настроен минимум на год вперёд от 2026-го."""

    years = load_work_calendar()["years"]

    assert {"2026", "2027"} <= set(years)
    assert is_working_day(date(2027, 2, 20)) is True  # рабочая суббота
    assert is_working_day(date(2027, 2, 22)) is False  # перенос к 23 февраля
    assert is_working_day(date(2027, 5, 3)) is False  # перенос с 1 мая
