"""Scan-led protocol exercises real API transactions; no 1C or CRM writes."""

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_engine
from app.core.config import get_settings
from app.main import app
from app.models import (
    LogisticsManualReview,
    LogisticsOrderPlan,
    LogisticsOrderPlanUnit,
    LogisticsTransfer,
)
from app.models.logistics_accounting import LogisticsAccountingEvent, LogisticsReceiptCheck
from app.models.site_order_fulfillment import SiteOrderExecutionEvent
from app.services import logistics_accounting as accounting
from tests.test_logistics_api import (
    _configure_logistics_auth,
    _id_maps,
    _seed_reference_data,
    override_db,
    setup_db,
)


@pytest.fixture
def flow(monkeypatch):
    engine, path = setup_db()
    headers = _configure_logistics_auth(monkeypatch)
    monkeypatch.setenv("LOGISTICS_SCAN_ACCOUNTING_ENABLED", "true")
    get_settings.cache_clear()
    app.dependency_overrides = {get_db: override_db(engine)}
    with TestClient(app) as client:
        _seed_reference_data(client, headers)
        ids = _id_maps(engine)
        plan = {
            "origin_order_external_id": "scan-test-order",
            "site_order_number": "test-1",
            "flow_mode": "ORDER_TRANSFER_V1",
            "plan_key": "scan-test-plan",
            "plan_version": 1,
            "final_warehouse_external_id": "central",
            "expected_unit_count": 1,
            "payload": {"accounting_protocol": accounting.PROTOCOL},
            "units": [
                {
                    "unit_key": "sealed-package",
                    "source_warehouse_external_id": "store-1",
                    "target_warehouse_external_id": "central",
                    "transfer_external_id": "transfer-1",
                    "ready_for_handoff": True,
                    "readiness": "ready",
                    "payload": {
                        "lines": [
                            {
                                "line_key": "row-1",
                                "product_external_id": "goods-1",
                                "name": "Тестовый товар",
                                "barcode": "GOODS-1",
                                "quantity": "2",
                            }
                        ]
                    },
                }
            ],
        }
        response = client.post("/api/logistics/sync/order-plans", json=[plan], headers=headers)
        assert response.status_code == 200, response.text
        yield client, headers, engine, ids, plan
    app.dependency_overrides = {}
    get_settings.cache_clear()
    get_engine.cache_clear()
    engine.dispose()
    os.remove(path)


def draft(flow, kind, actor, warehouse, dropoff=None):
    client, headers, _, ids, _ = flow
    body = {"actor_user_id": ids["users"][actor], "warehouse_id": ids["warehouses"][warehouse]}
    if dropoff:
        body.update(
            driver_id=ids["drivers"]["Иван Водитель"],
            default_dropoff_warehouse_id=ids["warehouses"][dropoff],
        )
    response = client.post(f"/api/logistics/{kind}/draft", json=body, headers=headers)
    assert response.status_code == 200, response.text
    url = f"/api/logistics/{kind}/draft/{response.json()['id']}"
    response = client.post(
        url + "/scan",
        json={"actor_user_id": body["actor_user_id"], "lookup_code": "BC-0001"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return url, body["actor_user_id"], response.json()


def confirm(flow, value, **extra):
    client, headers, _, _, _ = flow
    return client.post(
        value[0] + "/confirm", json={"actor_user_id": value[1], **extra}, headers=headers
    )


def test_scan_receipt_requires_count_and_accounting_ack(flow):
    client, headers, engine, ids, plan = flow
    handoff = draft(flow, "handoffs", "Отправитель", "store-1", "central")
    assert confirm(flow, handoff).status_code == 200
    assert confirm(flow, handoff).status_code == 200
    assert client.get("/api/logistics/accounting/events").status_code == 401
    queued = client.get("/api/logistics/accounting/events", headers=headers).json()
    assert len(queued) == 1
    assert queued[0]["operation"] == "dispatch"
    receipt = draft(flow, "receipts", "Получатель", "central")
    assert receipt[2]["items"][0]["requires_goods_count"] is True
    assert confirm(flow, receipt).status_code == 409
    counted = [
        {"transfer_id": ids["transfer_id"], "lines": [{"line_key": "row-1", "quantity": "2"}]}
    ]
    result = confirm(flow, receipt, receipts=counted)
    assert result.status_code == 200, result.text
    assert result.json()["accounting"][0]["accounting_status"] == "pending"
    monitor = client.get("/api/logistics/monitor", headers=headers)
    assert monitor.status_code == 200
    assert monitor.json()[0]["accounting"]["receipt_status"] == "matched"
    assert monitor.json()[0]["accounting"]["accounting_status"] == "pending"
    with Session(engine) as session:
        unit = session.scalar(select(LogisticsOrderPlanUnit))
        assert not accounting.ready_for_pickup(session, unit)
        assert len(session.scalars(select(LogisticsReceiptCheck)).all()) == 1
    ack_url = f"/api/logistics/accounting/events/{queued[0]['event_id']}/ack"
    applied = {"status": "applied", "documents": ["onec:dispatch:test"]}
    assert client.post(ack_url, json={"status": "applied"}, headers=headers).status_code == 422
    assert (
        client.post(
            ack_url, json={"status": "error", "error": "1C unavailable"}, headers=headers
        ).status_code
        == 200
    )
    assert client.post(ack_url, json=applied, headers=headers).status_code == 200
    assert client.post(ack_url, json=applied, headers=headers).status_code == 200
    assert (
        client.post(
            ack_url, json={"status": "applied", "documents": ["different"]}, headers=headers
        ).status_code
        == 409
    )
    final = client.get("/api/logistics/accounting/events", headers=headers).json()
    assert len(final) == 1 and final[0]["operation"] == "final_receipt"
    assert (
        client.post(
            f"/api/logistics/accounting/events/{final[0]['event_id']}/ack",
            json={"status": "applied", "documents": ["onec:receipt:test"]},
            headers=headers,
        ).status_code
        == 200
    )
    with Session(engine) as session:
        assert accounting.ready_for_pickup(session, session.scalar(select(LogisticsOrderPlanUnit)))
        assert len(session.scalars(select(LogisticsAccountingEvent)).all()) == 2
    plan["units"][0]["payload"]["lines"][0]["quantity"] = "3"
    assert (
        client.post("/api/logistics/sync/order-plans", json=[plan], headers=headers).status_code
        == 409
    )


def test_delayed_ack_reconciles_on_fresh_plan_without_duplicate_events(flow):
    client, headers, engine, ids, plan = flow
    with Session(engine) as session:
        session.get(LogisticsTransfer, ids["transfer_id"]).site_order_number = "test-1"
        session.commit()
    assert (
        confirm(flow, draft(flow, "handoffs", "Отправитель", "store-1", "central")).status_code
        == 200
    )
    receipt = draft(flow, "receipts", "Получатель", "central")
    assert (
        confirm(
            flow,
            receipt,
            receipts=[
                {
                    "transfer_id": ids["transfer_id"],
                    "lines": [{"line_key": "row-1", "quantity": "2"}],
                }
            ],
        ).status_code
        == 200
    )
    with Session(engine) as session:
        stored_plan = session.scalar(select(LogisticsOrderPlan))
        stored_plan.synced_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        plan_id = stored_plan.id
        session.commit()
    for operation in ("dispatch", "final_receipt"):
        event = client.get("/api/logistics/accounting/events", headers=headers).json()[0]
        assert event["operation"] == operation
        response = client.post(
            f"/api/logistics/accounting/events/{event['event_id']}/ack",
            json={"status": "applied", "documents": ["onec:" + operation]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    selector = select(SiteOrderExecutionEvent).where(
        SiteOrderExecutionEvent.source_ref
        == f"logistics_order_plan:{plan_id}:all_accepted_at_final"
    )
    with Session(engine) as session:
        assert session.scalars(selector).all() == []
    for _ in range(2):
        response = client.post("/api/logistics/sync/order-plans", json=[plan], headers=headers)
        assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert len(session.scalars(selector).all()) == 1
        assert len(session.scalars(select(LogisticsAccountingEvent)).all()) == 2


@pytest.mark.parametrize("quantity,damaged", [("1", False), ("3", False), ("2", True)])
def test_discrepancy_persists_counts_and_blocks_issue(flow, quantity, damaged):
    client, headers, engine, ids, _ = flow
    assert (
        confirm(flow, draft(flow, "handoffs", "Отправитель", "store-1", "central")).status_code
        == 200
    )
    receipt = draft(flow, "receipts", "Получатель", "central")
    response = confirm(
        flow,
        receipt,
        receipts=[
            {
                "transfer_id": ids["transfer_id"],
                "damaged": damaged,
                "lines": [{"line_key": "row-1", "quantity": quantity}],
            }
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json()["accounting"][0]["receipt_status"] == "discrepancy"
    with Session(engine) as session:
        unit = session.scalar(select(LogisticsOrderPlanUnit))
        assert not accounting.ready_for_pickup(session, unit)
        check = session.scalar(select(LogisticsReceiptCheck))
        assert check.lines[0]["quantity"] == quantity
        assert (
            session.scalar(select(LogisticsManualReview)).review_type
            == "package_receipt_discrepancy"
        )
        assert len(session.scalars(select(LogisticsAccountingEvent)).all()) == 1


def test_intermediate_leg_does_not_dispatch_twice(flow):
    client, headers, engine, ids, _ = flow
    sent = draft(flow, "handoffs", "Отправитель", "store-1", "store-2")
    assert confirm(flow, sent).status_code == 200
    received = draft(flow, "receipts", "Неверная Точка", "store-2")
    assert received[2]["items"][0]["requires_goods_count"] is False
    assert confirm(flow, received).status_code == 200
    assert (
        client.post(
            "/api/logistics/sync/users",
            headers=headers,
            json=[
                {
                    "external_id": "source-2-sender",
                    "full_name": "Перевалка",
                    "role": "sender",
                    "default_warehouse_external_id": "store-2",
                }
            ],
        ).status_code
        == 200
    )
    ids["users"].update(_id_maps(engine)["users"])
    sent_again = draft(flow, "handoffs", "Перевалка", "store-2", "central")
    assert confirm(flow, sent_again).status_code == 200
    with Session(engine) as session:
        assert len(session.scalars(select(LogisticsAccountingEvent)).all()) == 1
        assert session.scalar(select(LogisticsReceiptCheck)) is None


def test_rollback_disables_commands_but_retains_pending_event(flow, monkeypatch):
    client, headers, engine, _, _ = flow
    sent = draft(flow, "handoffs", "Отправитель", "store-1", "central")
    assert confirm(flow, sent).status_code == 200
    monkeypatch.setenv("LOGISTICS_SCAN_ACCOUNTING_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get("/api/logistics/accounting/events", headers=headers).status_code == 409
    assert confirm(flow, sent).status_code == 200
    received = draft(flow, "receipts", "Получатель", "central")
    assert confirm(flow, received).status_code == 409
    with Session(engine) as session:
        assert len(session.scalars(select(LogisticsAccountingEvent)).all()) == 1


def test_legacy_manual_state_changes_cannot_bypass_new_protocol(flow):
    client, headers, _, ids, _ = flow
    for action in ("return", "handoff-cancel"):
        response = client.post(
            f"/api/logistics/transfers/{ids['transfer_id']}/{action}",
            headers=headers,
            json={
                "actor_user_id": ids["users"]["Логист"],
                "warehouse_id": ids["warehouses"]["central"],
            },
        )
        assert response.status_code == 409, response.text
