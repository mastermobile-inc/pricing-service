from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.bitrix_logistics import _actor_from_session
from app.api.dependencies import get_db
from app.core.config import get_settings
from app.main import app
from app.models import (
    Base,
    LogisticsDriver,
    LogisticsManualReview,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsTransferState,
    LogisticsUser,
    LogisticsWarehouse,
)
from app.models.logistics import LogisticsDraftAudit, LogisticsSyncStatus
from app.services import logistics, logistics_onec
from app.services import logistics_drivers as drivers
from app.services import logistics_pending as pending


def employee(uid, position="Водитель", active=True, name="Эльвин"):
    return {"ID": str(uid), "WORK_POSITION": position, "ACTIVE": active, "NAME": name}


def snapshot(users, status="OPENED"):
    def call(method, params):
        if method == "timeman.status":
            if status is None:
                raise RuntimeError("unavailable")
            return {"result": {"STATUS": status}}
        start = params["start"]
        return {
            "result": users[start : start + 2],
            "total": len(users),
            **({"next": start + 2} if start + 2 < len(users) else {}),
        }

    return drivers.fetch_snapshot(call)[0]


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    settings = get_settings()
    monkeypatch.setattr(
        settings, "logistics_stage_pilot_warehouse_external_ids", ["w1", "w2", "w3"]
    )
    monkeypatch.setattr(settings, "logistics_stage_automation_enabled", False)
    monkeypatch.setattr(settings, "logistics_pending_documents_enabled", True)
    monkeypatch.setattr(settings, "logistics_rtu_sync_success_file", str(tmp_path / "missing"))
    monkeypatch.setattr(settings, "logistics_transfer_sync_success_file", None)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize(
    "status,expected",
    [("OPENED", "on_shift"), ("PAUSED", "off_shift"), ("CLOSED", "off_shift"), (None, "unknown")],
)
def test_snapshot_exact_position_and_shift(status, expected):
    rows = snapshot(
        [
            employee(1, "  вОдИтЕлЬ\u00a0"),
            employee(2, "Курьер"),
            employee(3, active=False),
            employee(4, "Старший водитель"),
        ],
        status,
    )
    assert [r["bitrix_user_id"] for r in rows] == ["1"]
    assert rows[0]["shift_status"] == expected


@pytest.mark.parametrize(
    "body",
    [
        {"result": [employee(1)], "total": 3},
        {"result": [employee(1)], "total": 3, "next": 0},
        {"result": [employee(1), employee(1)], "total": 2},
        {"result": [], "total": 0},
        {"result": [employee(1)]},
    ],
)
def test_incomplete_snapshot_never_reconciles(db, body):
    drivers.reconcile(db, snapshot([employee(9)]), apply=True)
    stamp = db.scalar(select(LogisticsSyncStatus.last_success_at))
    with pytest.raises(RuntimeError):
        drivers.fetch_snapshot(lambda *_: body)
    assert len(logistics.list_drivers(db)) == 1
    assert db.scalar(select(LogisticsSyncStatus.last_success_at)) == stamp


def test_driver_reconcile_history_no_name_merge_dry_run_and_legacy_guard(db):
    legacy = LogisticsDriver(full_name="Эльвин", external_id="legacy")
    db.add(legacy)
    db.commit()
    old_id = legacy.id
    rows = snapshot([employee(1), employee(2)])
    assert drivers.reconcile(db, rows)["created"] == 2
    assert legacy.is_active and not drivers.managed(db)
    drivers.reconcile(db, rows, apply=True)
    assert legacy.id == old_id and not legacy.is_active
    assert len(logistics.list_drivers(db)) == 2
    assert drivers.reconcile(db, rows, apply=True)["created"] == 0
    with pytest.raises(HTTPException, match="старый импорт"):
        logistics.sync_drivers(db, [{"external_id": "legacy", "full_name": "Эльвин"}])
    drivers.reconcile(db, snapshot([employee(1, "Курьер"), employee(2, active=False)]), apply=True)
    assert logistics.list_drivers(db) == []
    assert db.scalar(select(func.count()).select_from(LogisticsDriver)) == 3


def seed(db, count=3):
    db.add_all(
        [
            LogisticsWarehouse(id=i, external_id=f"w{i}", name=f"Склад {i}", kind="store")
            for i in (1, 2, 3)
        ]
    )
    db.flush()
    db.add_all(
        [
            LogisticsUser(id=1, full_name="Отправитель", role="sender", default_warehouse_id=1),
            LogisticsUser(id=2, full_name="Получатель", role="receiver", default_warehouse_id=2),
            LogisticsUser(
                id=3, full_name="Другой магазин", role="receiver", default_warehouse_id=3
            ),
            LogisticsDriver(id=1, full_name="Водитель 1"),
            LogisticsDriver(id=2, full_name="Водитель 2"),
        ]
    )
    for i in range(1, count + 1):
        ref = logistics.normalize_mm_log_document_ref(str(i))
        db.add(
            LogisticsTransfer(
                id=i,
                source_document_type="rtu",
                external_id=ref,
                barcode=f"B{i}",
                lookup_code=f"MMLOG1|rtu|{ref}|123",
                site_order_number="123",
                document_number=f"РТУ-{i}",
                document_date=datetime(2026, 9, 12),
                source_warehouse_id=1,
                target_warehouse_id=2,
                onec_status="posted",
                payload={"ready_for_handoff": True},
            )
        )
    db.commit()


def draft(db, operation="handoff", user=1, warehouse=1):
    return logistics.create_draft(
        db,
        draft_type=operation,
        actor_user_id=user,
        warehouse_id=warehouse,
        driver_id=1 if operation == "handoff" else None,
    )


def scan(db, draft_id, number=1, user=1, code=None):
    return logistics.add_scan_to_draft(
        db, draft_id=draft_id, actor_user_id=user, lookup_code=code or f"B{number}"
    )


def confirm(db, draft_id, user=1):
    return logistics.confirm_draft(
        db,
        draft_id=draft_id,
        actor_user_id=user,
        comment=None,
        idempotency_key=None,
        photos=[],
        source_channel="bitrix",
    )


def test_driver_change_preserves_scans_rejects_inactive_and_receives_history(db):
    seed(db)
    handoff = draft(db)
    scan(db, handoff["id"])
    db.get(LogisticsDriver, 1).is_active = False
    db.commit()
    with pytest.raises(HTTPException, match="Водитель больше недоступен"):
        confirm(db, handoff["id"])
    db.rollback()
    changed = logistics.change_draft_driver(
        db, draft_id=handoff["id"], actor_user_id=1, driver_id=2
    )
    assert changed["item_count"] == 1 and changed["driver_id"] == 2
    assert db.scalar(select(LogisticsDraftAudit)).details["previous_driver_id"] == 1
    confirm(db, handoff["id"])
    db.get(LogisticsDriver, 2).is_active = False
    db.commit()
    # Retry confirmed handoff remains a no-op despite driver deactivation.
    assert confirm(db, handoff["id"])["processed_count"] == 1
    expected = pending.pending_documents(
        db, actor_user_id=2, operation="receipt", warehouse_id=2, driver_id=2
    )
    assert expected["total"] == 1 and expected["drivers"][0]["id"] == 2
    receipt = draft(db, "receipt", 2, 2)
    scan(db, receipt["id"], user=2)
    confirm(db, receipt["id"], 2)
    assert db.get(LogisticsTransferState, 1).current_warehouse_id == 2
    assert db.scalar(select(func.count()).select_from(LogisticsTransferEvent)) == 2


def test_duplicate_qr_pending_partial_receipt_and_wrong_store(db):
    seed(db)
    handoff = draft(db)
    a = scan(db, handoff["id"], code="MMLOG1|rtu|1")
    b = scan(db, handoff["id"], code=db.get(LogisticsTransfer, 1).lookup_code)
    assert (a["scan_result"], b["scan_result"], b["item_count"]) == ("added", "already_scanned", 1)
    scan(db, handoff["id"], 2)
    before = pending.pending_documents(
        db, actor_user_id=1, operation="handoff", warehouse_id=1, draft_id=handoff["id"], limit=1
    )
    assert (before["total"], before["scanned_count"], before["remaining_count"]) == (3, 2, 1)
    assert len(before["items"]) == 1 and before["items"][0]["in_draft"]
    assert all(f["stale"] for f in before["freshness"].values())
    confirm(db, handoff["id"])
    after = pending.pending_documents(
        db, actor_user_id=1, operation="handoff", warehouse_id=1, offset=99
    )
    assert after["total"] == 1 and after["items"] == []
    wrong = draft(db, "receipt", 3, 3)
    with pytest.raises(HTTPException, match="другой точке: Склад 2"):
        scan(db, wrong["id"], user=3)
    db.rollback()
    receipt = draft(db, "receipt", 2, 2)
    scan(db, receipt["id"], user=2)
    p = pending.pending_documents(
        db, actor_user_id=2, operation="receipt", warehouse_id=2, draft_id=receipt["id"]
    )
    assert (p["total"], p["scanned_count"], p["remaining_count"]) == (2, 1, 1)
    confirm(db, receipt["id"], 2)
    assert (
        pending.pending_documents(db, actor_user_id=2, operation="receipt", warehouse_id=2)["total"]
        == 1
    )
    # Read-only views did not add reviews or events.
    assert db.scalar(select(func.count()).select_from(LogisticsManualReview)) == 0
    assert db.scalar(select(func.count()).select_from(LogisticsTransferEvent)) == 3


def test_pending_shared_readiness_and_scope(db):
    seed(db, 8)
    db.get(LogisticsTransfer, 2).payload = {"external_carrier_flow": True}
    db.get(LogisticsTransfer, 3).payload = {"ready_for_handoff": False}
    db.get(LogisticsTransfer, 4).onec_deleted = True
    db.get(LogisticsTransfer, 5).onec_status = "draft"
    db.get(LogisticsTransfer, 6).target_warehouse_id = 1
    db.add(LogisticsManualReview(transfer_id=7, review_type="ambiguous", reason="test"))
    # Intermediate warehouse is the document target; final leg must stay visible.
    db.get(LogisticsTransfer, 8).document_target_warehouse_id = 1
    db.commit()
    p = pending.pending_documents(db, actor_user_id=1, operation="handoff", warehouse_id=1)
    assert [row["transfer_id"] for row in p["items"]] == [1, 8]
    handoff = draft(db)
    with pytest.raises(HTTPException):
        scan(db, handoff["id"], 3)
    db.rollback()
    with pytest.raises(HTTPException) as exc:
        pending.pending_documents(db, actor_user_id=1, operation="handoff", warehouse_id=2)
    assert exc.value.status_code == 403


def test_shift_freshness_does_not_block(db):
    drivers.reconcile(db, snapshot([employee(1)], "PAUSED"), apply=True)
    driver = db.scalar(select(LogisticsDriver))
    assert drivers.serialize(driver)["shift_status"] == "off_shift"
    driver.shift_checked_at = drivers.now() - timedelta(minutes=4)
    assert drivers.serialize(driver)["shift_status"] == "unknown"
    assert drivers.require_driver(db, driver.id) is driver


def test_rtu_readiness_loss_is_persisted_without_events_and_can_recover(db):
    seed(db, 1)
    row = {
        "rtu_external_id": db.get(LogisticsTransfer, 1).external_id,
        "rtu_number": "РТУ-1",
        "rtu_date": datetime(2026, 9, 12),
        "site_order_number": "123",
        "site_delivery_method": "Самовывоз",
        "site_delivery_addition": "Склад 2",
        "source_warehouse_external_id": "w1",
        "source_warehouse_name": "Склад 1",
        "is_posted": 1,
        "is_marked": 0,
        "has_printed": 1,
        "has_assembled": 0,
    }
    logistics_onec.sync_ready_rtu_units(db, onec_engine=None, source_rows=[row], dry_run=True)
    assert db.get(LogisticsTransfer, 1).payload["ready_for_handoff"] is True
    applied = logistics_onec.sync_ready_rtu_units(
        db, onec_engine=None, source_rows=[row], dry_run=False
    )
    assert applied["synced_updated"] == 1
    assert db.get(LogisticsTransfer, 1).payload["ready_for_handoff"] is False
    assert (
        pending.pending_documents(db, actor_user_id=1, operation="handoff", warehouse_id=1)["total"]
        == 0
    )
    assert (
        logistics_onec.sync_ready_rtu_units(db, onec_engine=None, source_rows=[row], dry_run=False)[
            "synced_updated"
        ]
        == 0
    )
    row["has_assembled"] = 1
    logistics_onec.sync_ready_rtu_units(db, onec_engine=None, source_rows=[row], dry_run=False)
    assert db.get(LogisticsTransfer, 1).payload["ready_for_handoff"] is True
    assert db.scalar(select(func.count()).select_from(LogisticsTransferEvent)) == 0


def test_unknown_scan_messages_are_distinct_and_preserve_draft(db):
    seed(db)
    d = draft(db)
    scan(db, d["id"])
    for code, message in [
        ("bad-code", "Код не распознан"),
        ("MMLOG1|rtu|99", "QR распознан, но документ ещё не загружен"),
    ]:
        with pytest.raises(HTTPException, match=message):
            scan(db, d["id"], code=code)
        db.rollback()
        assert logistics.get_open_draft_for_actor(db, actor_user_id=1)["item_count"] == 1


def test_bff_pending_driver_patch_and_draft_ownership(db):
    seed(db)
    handoff = draft(db)
    scan(db, handoff["id"])
    actor_id = 1
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[_actor_from_session] = lambda: db.get(LogisticsUser, actor_id)
    try:
        with TestClient(app) as client:
            url = f"/api/bitrix/logistics/handoffs/draft/{handoff['id']}/driver"
            response = client.patch(url, json={"driver_id": 2})
            assert response.status_code == 200, response.text
            assert response.json()["item_count"] == 1
            p = client.get(
                "/api/bitrix/logistics/pending-documents",
                params={"operation": "handoff", "warehouse_id": 1, "draft_id": handoff["id"]},
            )
            assert p.status_code == 200 and p.json()["remaining_count"] == 2
            assert (
                client.get(
                    "/api/bitrix/logistics/pending-documents",
                    params={"operation": "handoff", "warehouse_id": 1, "limit": 0},
                ).status_code
                == 422
            )
            actor_id = 3
            assert client.patch(url, json={"driver_id": 1}).status_code == 403
    finally:
        app.dependency_overrides.clear()
