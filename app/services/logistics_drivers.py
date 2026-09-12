"""Bitrix is read only here; scan/confirm use the last local snapshot."""

from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.logistics import LogisticsDriver, LogisticsSyncStatus


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalized_position(value):
    return " ".join(str(value or "").split()).casefold()


def active_driver(user):
    return (
        user.get("ACTIVE") in (True, "Y", "true", "1", 1)
        and normalized_position(user.get("WORK_POSITION")) == "водитель"
    )


def managed(session):
    return (
        session.scalar(
            select(LogisticsSyncStatus.id).where(LogisticsSyncStatus.source == "drivers")
        )
        is not None
    )


def lock_authority(session):
    # Serializes first source switch with the legacy importer, including an empty table.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(225, 3927)"))


def require_driver(session, driver_id):
    row = session.scalar(
        select(LogisticsDriver)
        .where(LogisticsDriver.id == driver_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or not row.is_active
        or (
            managed(session)
            and (not row.bitrix_user_id or normalized_position(row.work_position) != "водитель")
        )
    ):
        raise HTTPException(409, "Водитель больше недоступен. Выберите действующего водителя")
    return row


def freshness(session):
    marker = session.scalar(
        select(LogisticsSyncStatus).where(LogisticsSyncStatus.source == "drivers")
    )
    stamp = marker.last_success_at if marker else None
    return {
        "last_success_at": stamp,
        "stale": stamp is None
        or (now() - stamp.replace(tzinfo=None)).total_seconds()
        > get_settings().logistics_sync_freshness_seconds,
    }


def serialize(row):
    fresh = (
        row.shift_checked_at is not None
        and (now() - row.shift_checked_at.replace(tzinfo=None)).total_seconds()
        <= get_settings().logistics_sync_freshness_seconds
    )
    return dict(
        id=row.id,
        external_id=row.external_id,
        bitrix_user_id=row.bitrix_user_id,
        full_name=row.full_name,
        phone=row.phone,
        is_active=row.is_active,
        shift_status=(row.shift_status or "unknown") if fresh else "unknown",
        shift_checked_at=row.shift_checked_at,
        synced_at=row.synced_at,
    )


class BitrixDriverReader:
    def __init__(self, base, client):
        self.base = base.rstrip("/")
        self.client = client

    def __call__(self, method, params):
        try:
            response = self.client.post(f"{self.base}/{method}.json", data=params)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            # Never log an exception containing the webhook URL.
            raise RuntimeError("Bitrix driver request failed") from None
        if not isinstance(body, dict) or body.get("error") or "result" not in body:
            raise RuntimeError(f"Bitrix method unavailable: {method}")
        return body


def fetch_snapshot(call):
    """Require all pages and stable totals before reconciling missing employees."""
    users, seen, start, expected_total = [], set(), 0, None
    for _ in range(1000):
        body = call(
            "user.get",
            {
                "start": start,
                "SORT": "ID",
                "ORDER": "ASC",
                "FILTER[ACTIVE]": "Y",
                "FILTER[USER_TYPE]": "employee",
            },
        )
        page = body.get("result")
        total = body.get("total")
        if not isinstance(page, list) or not isinstance(total, int) or total <= 0:
            raise RuntimeError("Incomplete Bitrix employee snapshot")
        if expected_total is None:
            expected_total = total
        if total != expected_total:
            raise RuntimeError("Employee list changed during pagination; retry next minute")
        for user in page:
            uid = str(user.get("ID", "")) if isinstance(user, dict) else ""
            if not uid.isdecimal() or int(uid) <= 0 or uid in seen or "ACTIVE" not in user:
                raise RuntimeError("Invalid or repeated employee in snapshot")
            seen.add(uid)
            users.append(user)
        next_page = body.get("next")
        if next_page is None:
            if len(users) != total:
                raise RuntimeError("Truncated employee snapshot")
            break
        if not page or not isinstance(next_page, int) or next_page <= start:
            raise RuntimeError("Invalid Bitrix pagination cursor")
        start = next_page
    else:
        raise RuntimeError("Employee pagination limit exceeded")
    drivers = []
    for user in users:
        if not active_driver(user):
            continue
        uid = str(user["ID"])
        shift, checked = "unknown", None
        try:
            status = call("timeman.status", {"USER_ID": uid})["result"]
            raw = status.get("STATUS") if isinstance(status, dict) else None
            if raw in ("OPENED", "PAUSED", "CLOSED", "EXPIRED"):
                shift = "on_shift" if raw == "OPENED" else "off_shift"
                checked = now()
        except (RuntimeError, KeyError):
            pass
        name = " ".join(
            str(user.get(key) or "").strip() for key in ("LAST_NAME", "NAME", "SECOND_NAME")
        ).strip()
        if not name:
            raise RuntimeError("Driver name is missing")
        drivers.append(
            dict(
                bitrix_user_id=uid,
                full_name=name[:255],
                phone=str(user.get("PERSONAL_MOBILE") or user.get("WORK_PHONE") or "")[:32] or None,
                work_position=str(user.get("WORK_POSITION") or "")[:255],
                shift_status=shift,
                shift_checked_at=checked,
            )
        )
    return drivers, len(users)


def reconcile(session: Session, snapshot, *, apply=False):
    if apply:
        lock_authority(session)
    ids = [row["bitrix_user_id"] for row in snapshot]
    if len(set(ids)) != len(ids) or any(
        normalized_position(row["work_position"]) != "водитель" for row in snapshot
    ):
        raise ValueError("Invalid driver snapshot")
    # Row locks serialize with driver replacement/confirmation.
    existing = list(
        session.scalars(select(LogisticsDriver).order_by(LogisticsDriver.id).with_for_update())
    )
    by_id = {row.bitrix_user_id: row for row in existing if row.bitrix_user_id}
    report = dict(created=0, updated=0, deactivated=0, active=len(snapshot))
    stamp = now()
    for data in snapshot:
        row = by_id.get(data["bitrix_user_id"])
        report["updated" if row else "created"] += 1
        if apply:
            if row is None:
                row = LogisticsDriver(
                    full_name=data["full_name"], bitrix_user_id=data["bitrix_user_id"]
                )
                session.add(row)
            for key, value in data.items():
                setattr(row, key, value)
            row.is_active, row.synced_at = True, stamp
    for row in existing:
        if row.bitrix_user_id not in ids and row.is_active:
            report["deactivated"] += 1
            if apply:
                row.is_active, row.synced_at = False, stamp
    if apply:
        marker = session.scalar(
            select(LogisticsSyncStatus).where(LogisticsSyncStatus.source == "drivers")
        )
        if marker is None:
            marker = LogisticsSyncStatus(source="drivers", last_success_at=stamp)
            session.add(marker)
        marker.last_success_at = stamp
        session.commit()
    return report
