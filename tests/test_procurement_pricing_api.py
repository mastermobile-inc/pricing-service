from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.api.procurement_pricing import buyer, router
from app.models.procurement_pricing import (
    ProcurementPriceBatch,
    ProcurementPriceEvent,
    ProcurementPriceLine,
    ProcurementPricePreset,
)
from app.schemas.procurement_pricing import PricingRow, PricingTable
from app.services.bitrix_procurement_order_formation_auth import ProcurementOrderFormationSession
from app.services.procurement_pricing_source import _last_year


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (
        ProcurementPriceBatch,
        ProcurementPriceLine,
        ProcurementPriceEvent,
        ProcurementPricePreset,
    ):
        model.__table__.create(engine)
    app = FastAPI()
    app.include_router(router)

    def session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[buyer] = lambda: ProcurementOrderFormationSession(
        "buyer", "test", "member", "1", datetime.now(UTC)
    )
    with TestClient(app) as c:
        yield c, app
    engine.dispose()


ROOT = "/procurement-order-formation/pricing"


def test_batch_serialization_and_user_presets_are_private(client):
    c, app = client
    response = c.post(
        ROOT + "/batches",
        json={
            "request_key": str(uuid4()),
            "lines": [{"code": "ABC", "price_type": "platinum", "old_price": 80, "new_price": 90}],
        },
    )
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["status"] == "draft"
    with patch(
        "app.api.procurement_pricing.current_prices",
        return_value={("ABC", "platinum"): {"price": Decimal(80), "currency": "RUB"}},
    ):
        assert (
            c.post(ROOT + f"/batches/{batch['id']}/approve", json={"version": 1}).json()["status"]
            == "approved"
        )
    preset = c.post(
        ROOT + "/presets",
        json={
            "name": "Низкий брак",
            "filters": {"start": "2026-09-01", "end": "2026-09-30", "defect_max": 2},
        },
    )
    assert preset.status_code == 200, preset.text
    app.dependency_overrides[buyer] = lambda: ProcurementOrderFormationSession(
        "other", "test", "member", "2", datetime.now(UTC)
    )
    assert c.get(ROOT + "/presets").json() == []
    assert c.get(ROOT + f"/batches/{batch['id']}").status_code == 404
    assert c.post(ROOT + f"/batches/{batch['id']}/approve", json={"version": 1}).status_code == 404


def test_no_session_cannot_read_prices():
    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get(ROOT + "/table?start=2026-09-01&end=2026-09-30").status_code in (
        401,
        403,
    )


def test_export_is_full_filtered_dataset_and_escapes_spreadsheet_formula(client):
    c, _ = client
    table = PricingTable(
        items=[PricingRow(code="ABC", name='=WEBSERVICE("https://bad")', bronze=100)],
        total=1,
        start=date(2026, 9, 1),
        end=date(2026, 9, 30),
        observed_at=datetime.now(UTC),
        facts_through=date(2026, 9, 19),
    )
    with patch("app.api.procurement_pricing.build_table", return_value=table) as build:
        response = c.get(ROOT + "/export?start=2026-09-01&end=2026-09-30&format=xlsx")
        assert response.status_code == 200, response.text
        assert build.call_args.kwargs["paginate"] is False
        book = load_workbook(BytesIO(response.content))
        assert book.active["B4"].data_type != "f"
        assert book.active["B4"].value.startswith("'=")
        assert "Прогноз продаж, шт." in [cell.value for cell in book.active[3]]
        assert book.active["F4"].value == 100


def test_leap_year_comparison():
    assert _last_year(date(2024, 2, 29)) == date(2023, 2, 28)


@pytest.mark.parametrize("path", ["table", "export"])
def test_invalid_combined_filters_return_validation_error(client, path):
    c, _ = client
    assert c.get(ROOT + f"/{path}?start=2026-09-30&end=2026-09-01").status_code == 422
    assert (
        c.get(
            ROOT + f"/{path}?start=2026-09-01&end=2026-09-30&defect_min=10&defect_max=1"
        ).status_code
        == 422
    )
