"""Isolated SQL regression; no CRM, 1C or file-backed database access."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from app.services import order_payment_control as service


@pytest.mark.parametrize(
    "duplicate", [None, "valid", "no_due", "stale", "other_stage", "unrelated"]
)
@pytest.mark.parametrize(
    "row_age,state_age,has_due",
    [
        (0, 0, True),
        (600, 600, True),
        (601, 0, True),
        (0, 601, True),
        (0, None, True),
        (0, 0, False),
    ],
)
def test_ready_sql_rejects_ambiguous_or_stale_snapshot(duplicate, row_age, state_age, has_due):
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("""CREATE TABLE order_assembly_queue_item (
                deal_id INTEGER UNIQUE, order_number TEXT, crm_stage TEXT,
                assembly_due_at TIMESTAMP, synced_at TIMESTAMP)"""))
            connection.execute(text("""CREATE TABLE order_assembly_queue_sync_state (
                source TEXT UNIQUE, last_success_at TIMESTAMP)"""))
            if state_age is not None:
                connection.execute(
                    text("INSERT INTO order_assembly_queue_sync_state VALUES ('bitrix_deal', :at)"),
                    {"at": now - timedelta(seconds=state_age)},
                )
            row = dict(
                deal=1,
                number="T3520-CRM",
                stage="EXECUTING",
                due=now + timedelta(hours=2) if has_due else None,
                synced=now - timedelta(seconds=row_age),
            )
            insert = text(
                "INSERT INTO order_assembly_queue_item VALUES (:deal, :number, :stage, :due, :synced)"
            )
            connection.execute(insert, row)
            if duplicate is not None:
                second = dict(row, deal=2)
                if duplicate == "no_due":
                    second["due"] = None
                elif duplicate == "stale":
                    second["synced"] = now - timedelta(minutes=11)
                elif duplicate == "other_stage":
                    second["stage"] = "NEW"
                elif duplicate == "unrelated":
                    second["number"] = "T3520-OTHER"
                connection.execute(insert, second)
            values = list(
                connection.execute(
                    service.CONFIRMED_READY_AT_SQL,
                    {
                        "site_order_number": "T3520-CRM",
                        "fresh_after": now - timedelta(minutes=10),
                    },
                ).scalars()
            )
            expected = (
                duplicate in (None, "unrelated")
                and row_age <= 600
                and state_age is not None
                and state_age <= 600
                and has_due
            )
            assert (len(values) == 1) is expected
    finally:
        engine.dispose()
