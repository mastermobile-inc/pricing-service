from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.models import (
    Base,
    LogisticsDriver,
    LogisticsTransfer,
    LogisticsTransferEvent,
    LogisticsUser,
    LogisticsWarehouse,
)
from app.services import logistics
from app.services import logistics_routing as routing


@pytest.fixture
def db(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "logistics_transit_routing_enabled", True)
    monkeypatch.setattr(settings, "logistics_stage_automation_enabled", False)
    monkeypatch.setattr(
        settings, "logistics_stage_pilot_warehouse_external_ids", ["a", "b", "cs", "region"]
    )
    monkeypatch.setattr(settings, "logistics_transit_source_external_ids", ["a", "b"])
    monkeypatch.setattr(settings, "logistics_central_transit_external_id", "cs")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for i, ref in enumerate(["a", "b", "cs", "region"], 1):
            session.add(
                LogisticsWarehouse(
                    id=i, external_id=ref, name=ref, kind="transit" if ref == "cs" else "store"
                )
            )
        session.add_all(
            [
                LogisticsUser(id=1, full_name="Admin", role="admin"),
                LogisticsUser(id=2, full_name="Sender", role="sender", default_warehouse_id=1),
                LogisticsDriver(id=1, full_name="Driver"),
            ]
        )
        session.flush()
        for i in range(1, 4):
            session.add(
                LogisticsTransfer(
                    id=i,
                    external_id=f"unit{i}",
                    barcode=f"qr{i}",
                    lookup_code=f"qr{i}",
                    source_document_type="rtu",
                    document_number=str(i),
                    document_date=datetime(2026, 9, 14),
                    source_warehouse_id=1,
                    target_warehouse_id=2,
                    document_target_warehouse_id=2,
                    site_order_number="123",
                    onec_status="posted",
                    payload={"ready_for_handoff": True},
                )
            )
        session.commit()
        yield session
    engine.dispose()


def draft(db, warehouse=1, operation="handoff", actor=1):
    return logistics.create_draft(
        db,
        draft_type=operation,
        actor_user_id=actor,
        warehouse_id=warehouse,
        driver_id=1 if operation == "handoff" else None,
    )


def scan(db, d, number=1, mode=None, actor=1):
    return logistics.add_scan_to_draft(
        db, draft_id=d["id"], actor_user_id=actor, lookup_code=f"qr{number}", route_mode=mode
    )


def confirm(db, d, actor=1):
    return logistics.confirm_draft(
        db,
        draft_id=d["id"],
        actor_user_id=actor,
        comment=None,
        idempotency_key=f"confirm-{d['id']}",
        photos=[],
        source_channel="bitrix",
    )


def test_mixed_transit_final_only_crm_and_next_leg(db, monkeypatch):
    events = []
    monkeypatch.setattr(
        logistics.site_order_fulfillment,
        "upsert_execution_event",
        lambda *a, **kw: events.append(kw),
    )
    d = draft(db)
    d = scan(db, d, mode="direct")
    routing.change_draft_route(
        db, draft_id=d["id"], item_id=d["items"][0]["id"], actor_user_id=1, mode="via_transit"
    )
    d = scan(db, d, number=2, mode="direct")
    confirm(db, d)
    assert db.get(LogisticsTransfer, 1).state.dropoff_warehouse_id == 3
    assert db.get(LogisticsTransfer, 2).state.dropoff_warehouse_id == 2
    assert len(events) == 2
    receipt = draft(db, 3, "receipt")
    scan(db, receipt)
    confirm(db, receipt)
    assert len(events) == 2  # No pickup waiting at intermediate point.
    unit = db.get(LogisticsTransfer, 1)
    assert unit.state.current_warehouse_id == 3
    d = draft(db, 3)
    with pytest.raises(HTTPException):
        scan(db, d, mode="via_transit")
    scan(db, d, mode="direct")
    confirm(db, d)
    assert len(events) == 2  # Second handoff does not replay moving.
    d = draft(db, 2, "receipt")
    scan(db, d)
    confirm(db, d)
    confirm(db, d)
    assert len(events) == 3
    assert events[-1]["event_type"] == logistics.site_order_fulfillment.EVENT_PICKUP_STORED
    d = draft(db, 2)
    with pytest.raises(HTTPException):
        scan(db, d, mode="direct")


def test_reroute_idempotency_version_roles_stale_receipt(db):
    d = draft(db, actor=2)
    scan(db, d, mode="direct", actor=2)
    confirm(db, d, actor=2)
    unit = db.get(LogisticsTransfer, 1)
    receipt = draft(db, 2, "receipt")
    scan(db, receipt)
    args = dict(
        transfer_id=1,
        mode="via_transit",
        reason="Next leg tomorrow",
        expected_version=unit.state.version,
        idempotency_key="r1",
    )
    with pytest.raises(HTTPException) as exc:
        routing.reroute(db, actor_user_id=2, **args)
    assert exc.value.status_code == 403
    result = routing.reroute(db, actor_user_id=1, **args)
    assert routing.reroute(db, actor_user_id=1, **args) == result
    with pytest.raises(HTTPException):
        routing.reroute(db, actor_user_id=1, **{**args, "idempotency_key": "r2"})
    with pytest.raises(HTTPException):
        confirm(db, receipt)
    assert logistics._get_draft(db, receipt["id"]).status == "open"
    routing.reroute(
        db,
        actor_user_id=1,
        **{
            **args,
            "mode": "direct",
            "expected_version": result["version"],
            "idempotency_key": "r3",
        },
    )
    with pytest.raises(HTTPException):
        confirm(db, receipt)  # Returning the route does not revive stale receipt scans.
    assert unit.target_warehouse_id == unit.document_target_warehouse_id == 2
    assert (
        len(
            db.scalars(
                select(LogisticsTransferEvent).where(
                    LogisticsTransferEvent.event_type == "handed_to_driver"
                )
            ).all()
        )
        == 1
    )


def test_region_and_feature_off(db, monkeypatch):
    unit = db.get(LogisticsTransfer, 1)
    assert [r["mode"] for r in routing.choices(db, unit, 4)] == ["direct"]
    assert routing.choices(db, unit, 2) == []
    monkeypatch.setattr(get_settings(), "logistics_transit_routing_enabled", False)
    assert routing.choices(db, unit, 1) == []
    with pytest.raises(HTTPException):
        scan(db, draft(db), mode="via_transit")


def test_bff_route_and_monitor_contract(db):
    from fastapi.testclient import TestClient

    from app.api.bitrix_logistics import _actor_from_session
    from app.api.dependencies import get_db
    from app.api.logistics_web import require_logistics_web_actor
    from app.main import app

    actor = db.get(LogisticsUser, 1)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[_actor_from_session] = lambda: actor
    app.dependency_overrides[require_logistics_web_actor] = lambda: actor
    try:
        with TestClient(app) as client:
            d = draft(db)
            response = client.post(
                f"/api/bitrix/logistics/handoffs/draft/{d['id']}/scan",
                json={"lookup_code": "qr1", "route_mode": "direct"},
            )
            assert response.status_code == 200, response.text
            item = response.json()["items"][0]
            assert item["final_warehouse_id"] == 2
            assert len(item["route_options"]) == 2
            response = client.patch(
                f"/api/bitrix/logistics/handoffs/draft/{d['id']}/items/{item['id']}/route",
                json={"mode": "via_transit"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["items"][0]["dropoff_warehouse_id"] == 3
            confirm(db, d)
            monitor = client.get("/api/bitrix/logistics/monitor?status=in_transit").json()[0]
            payload = dict(
                mode="direct",
                reason="Direct delivery available",
                expected_version=monitor["version"],
                idempotency_key="api-route",
            )
            response = client.post("/api/logistics/web/transfers/1/reroute", json=payload)
            assert response.status_code == 200, response.text
            assert db.get(LogisticsTransfer, 1).state.dropoff_warehouse_id == 2
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(_actor_from_session, None)
        app.dependency_overrides.pop(require_logistics_web_actor, None)
