"""Производственный календарь РФ: один источник рабочих дней на весь сервис.

Календарь лежит в `config/procurement-work-calendar.json` и описывает каждый год
двумя списками: `holidays` — нерабочие дни, попадающие на будни (включая переносы),
`working_weekends` — суббота или воскресенье, объявленные рабочими.

Год, которого нет в файле, не достраивается по умолчанию: правила переносов
устанавливает постановление Правительства, вычислить их нельзя. Вместо тихой
ошибки в сроках вызывающий код получает `ValueError` и решает сам, что делать.

Загрузка кешируется на процесс: правка JSON подхватится только после
перезапуска. Для ежеминутного cron-воркера это незаметно, для API-процесса —
после релиза.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_CALENDAR_PATH = (
    Path(__file__).resolve().parents[2] / "config/procurement-work-calendar.json"
)


@lru_cache(maxsize=1)
def load_work_calendar() -> dict:
    """Читает календарь с диска. Возвращённый словарь менять нельзя — он общий."""

    return json.loads(DEFAULT_CALENDAR_PATH.read_text())


def _resolve_calendar(calendar: dict | None) -> dict:
    return load_work_calendar() if calendar is None else calendar


def is_working_day(day: date, *, calendar: dict | None = None) -> bool:
    """Рабочий ли день. Год без настроек — `ValueError`, а не догадка."""

    resolved = _resolve_calendar(calendar)
    year = resolved["years"].get(str(day.year))
    if year is None:
        raise ValueError(f"Производственный календарь на {day.year} год не настроен")
    return day.isoformat() in year.get("working_weekends", []) or (
        day.weekday() < 5 and day.isoformat() not in year["holidays"]
    )


def next_working_day(day: date, *, calendar: dict | None = None) -> date:
    """Сам день, если он рабочий, иначе ближайший следующий рабочий."""

    resolved = _resolve_calendar(calendar)
    while not is_working_day(day, calendar=resolved):
        day += timedelta(days=1)
    return day


def to_moscow(moment: datetime) -> datetime:
    """Момент в московском времени. Naive-значение считается UTC, как в БД."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC).astimezone(MOSCOW)
    return moment.astimezone(MOSCOW)


def add_working_days(
    start: datetime,
    days: int,
    *,
    calendar: dict | None = None,
) -> datetime:
    """Момент через `days` рабочих дней от `start`, время суток сохраняется.

    Возвращает UTC без таймзоны — тот же формат, в котором сервис хранит время.
    Выходные и праздники не считаются: пять рабочих дней от пятницы — это
    следующая пятница, а не среда.
    """

    resolved = _resolve_calendar(calendar)
    local = to_moscow(start)
    if days <= 0:
        return local.astimezone(UTC).replace(tzinfo=None)
    day = local.date()
    remaining = days
    while remaining > 0:
        day += timedelta(days=1)
        if is_working_day(day, calendar=resolved):
            remaining -= 1
    return datetime.combine(day, local.time(), MOSCOW).astimezone(UTC).replace(tzinfo=None)
