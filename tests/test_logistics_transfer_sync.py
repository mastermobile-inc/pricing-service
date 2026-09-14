from datetime import date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    LogisticsManualReview,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsWarehouse,
)
from app.services import logistics
from app.services.logistics_transfer_sync import reconcile_page, sync_all
from tasks.sync_logistics_transfers_from_onec import parse_args


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                LogisticsWarehouse(id=i, name=str(i), kind="store", external_id=f"w{i}")
                for i in (1, 2, 3)
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def row(number=1, **changes):
    return dict(
        external_id=f"0x{number:032x}",
        document_number=str(number),
        document_date=datetime(2026, 9, 14),
        source_warehouse_external_id="w1",
        target_warehouse_external_id="w2",
        document_target_warehouse_external_id="w3",
        accounting_warehouse_code="РБ0000027",
        posted=True,
        deleted=False,
        printed=True,
        scanned=True,
        **changes,
    )


def test_all_pages_dry_apply_idempotency_no_events(db):
    rows = [row(i) for i in range(1, 502)]
    calls = []

    def fetch(engine, **kw):
        calls.append(kw["after"])
        return [r for r in rows if kw["after"] is None or r["external_id"] > kw["after"]][
            : kw["page_size"]
        ]

    result = sync_all(db, None, date_from=date(2026, 9, 1), fetch=fetch)
    assert result["read"] == result["created"] == 501
    assert db.scalar(select(func.count()).select_from(LogisticsTransfer)) == 0
    result = sync_all(db, None, apply=True, fetch=fetch)
    assert result["created"] == 501 and result["pages"] == 2
    result = sync_all(db, None, apply=True, fetch=fetch)
    assert result["created"] == result["updated"] == 0
    assert db.scalar(select(func.count()).select_from(LogisticsTransferEvent)) == 0
    unit = db.scalar(select(LogisticsTransfer).limit(1))
    assert unit.state.current_warehouse_id == 1
    assert unit.site_order_number is None


def test_readiness_loss_before_and_after_movement_unknown_resolution(db):
    qr = f"MMLOG1|transfer|{row()['external_id']}"
    with pytest.raises(HTTPException):
        logistics.lookup_unit(db, qr)
    assert reconcile_page(db, [row()], apply=True)["resolved"] == 1
    assert db.scalar(select(LogisticsManualReview)).status == "resolved"
    unit = db.scalar(select(LogisticsTransfer))
    reconcile_page(db, [{**row(), "printed": False}], apply=True)
    assert unit.payload["ready_for_handoff"] is False
    reconcile_page(db, [row()], apply=True)
    assert unit.payload["ready_for_handoff"] is True
    unit.state.status = "in_transit"
    unit.state.current_warehouse_id = None
    unit.state.dropoff_warehouse_id = 3
    unit.state.last_event_type = "handed_to_driver"
    db.commit()
    before = unit.state.version
    changed = {**row(), "target_warehouse_external_id": "w1", "posted": False}
    assert reconcile_page(db, [changed], apply=True)["conflicts"] == 1
    assert reconcile_page(db, [changed], apply=True)["conflicts"] == 1
    assert unit.target_warehouse_id == 2 and unit.state.dropoff_warehouse_id == 3
    assert unit.state.version == before
    assert (
        db.scalar(
            select(func.count())
            .select_from(LogisticsManualReview)
            .where(LogisticsManualReview.status == "open")
        )
        == 1
    )


@pytest.mark.parametrize(
    "change",
    [
        {"printed": False},
        {"scanned": False},
        {"posted": False},
        {"deleted": True},
        {"accounting_warehouse_code": "other"},
        {"target_warehouse_external_id": None},
    ],
)
def test_new_unready_skipped(db, change):
    assert reconcile_page(db, [{**row(), **change}], apply=True)["skipped"] == 1
    assert db.scalar(select(func.count()).select_from(LogisticsTransfer)) == 0


def test_unstable_pagination_rejected_and_cli():
    engine = create_engine("sqlite://")
    with Session(engine) as db, pytest.raises(RuntimeError):
        sync_all(db, None, fetch=lambda *a, **k: [row(2), row(1)])
    assert not parse_args([]).apply
    assert (
        parse_args(["--transfer-external-id", "1"]).transfer_external_id
        == "0x00000000000000010000000000000000"
    )
    with pytest.raises(SystemExit):
        parse_args(["--limit", "0"])


def test_missing_source_reference_import_does_not_grant_access(db):
    source_ref = "0x00000000000000000000000000000004"
    incoming = {
        **row(),
        "source_warehouse_external_id": source_ref,
        "source_warehouse_name": "Справочный склад",
    }
    assert reconcile_page(db, [incoming])["created"] == 1
    assert (
        db.scalar(select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == source_ref))
        is None
    )
    report = reconcile_page(db, [incoming], apply=True)
    assert report["created"] == report["warehouses_created"] == 1
    warehouse = db.scalar(
        select(LogisticsWarehouse).where(LogisticsWarehouse.external_id == source_ref)
    )
    assert not warehouse.is_active
    unit = db.scalar(select(LogisticsTransfer))
    assert unit.state.current_warehouse_id == warehouse.id
    assert reconcile_page(db, [incoming], apply=True)["created"] == 0
    assert db.scalar(select(func.count()).select_from(LogisticsTransferEvent)) == 0


def test_query_escapes_filters_and_uses_unambiguous_date():
    from sqlalchemy.dialects import mssql

    from app.services.logistics_transfer_sync import fetch_page

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, params):
            sql = str(
                statement.params(**params).compile(
                    dialect=mssql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            assert "CONVERT(datetime, '20260831', 112)" in sql
            assert "d._Number = 'test''quote'" in sql
            assert "d._IDRRef > CONVERT(binary(16), '0x00000000000000000000000000000001', 1)" in sql
            assert "ORDER BY d._IDRRef" in sql
            return self

        def mappings(self):
            return []

    class Engine:
        def connect(self):
            return Connection()

    assert (
        fetch_page(
            Engine(),
            date_from=date(2026, 8, 31),
            document_number="test'quote",
            after=row()["external_id"],
        )
        == []
    )
