from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import require_order_fulfillment_internal_token
from app.core.config import get_settings
from app.main import app
from app.schemas.order_fulfillment_quote import FulfillmentQuoteRequest
from app.services.order_fulfillment_quote import (
    InventorySnapshot,
    QuoteUnavailable,
    TimingProfile,
    calculate_quote,
    next_departure,
)
from scripts.run_order_fulfillment_demo import DEFAULT_PROFILE, create_demo_app

NOW = datetime.fromisoformat("2026-09-13T10:00:00+03:00")
FIXTURE = Path(__file__).parent / "fixtures/order_fulfillment_inventory.json"


@pytest.fixture
def profile():
    return TimingProfile.model_validate_json(DEFAULT_PROFILE.read_text())


@pytest.fixture
def inventory():
    data = json.loads(FIXTURE.read_text())
    data["observed_at"] = NOW.isoformat()
    return InventorySnapshot.model_validate(data)


def request(*, method="cdek", pickup=None, sources=None, quantity="2", lines=None):
    return FulfillmentQuoteRequest.model_validate(
        {
            "delivery_method": method,
            "pickup_point_id": pickup,
            "lines": lines
            or [
                {
                    "line_id": "l1",
                    "product_id": "demo-display",
                    "quantity": quantity,
                    "sources": sources,
                }
            ],
        }
    )


def source(warehouse, quantity):
    return {"warehouse_id": "demo-" + warehouse, "quantity": quantity}


def run(req, profile, inventory, now=NOW):
    inventory = inventory.model_copy(update={"observed_at": now})
    return calculate_quote(req, profile, inventory, now=now)


def test_local_stock_no_transfer(profile, inventory):
    result = run(
        request(method="pickup", pickup="demo-pickup-sadovaya", quantity="1"), profile, inventory
    )
    allocation = result.lines[0].sources[0]
    assert allocation.warehouse_id == "demo-sadovaya"
    assert allocation.legs == []
    assert result.ready_at.isoformat() == "2026-09-13T11:05:00+03:00"
    assert not result.payment_allowed and not result.reservation_created
    assert result.test_only and not result.production_eligible
    assert result.carrier_handoff_at is None


@pytest.mark.parametrize("carrier", ["cdek", "russian_post", "yandex_delivery"])
def test_split_one_sku_to_central_all_carriers(carrier, profile, inventory):
    result = run(request(method=carrier), profile, inventory)
    assert result.consolidation_warehouse_id == "demo-central"
    assert [(a.warehouse_id, a.quantity) for a in result.lines[0].sources] == [
        ("demo-central", Decimal(1)),
        ("demo-mitino", Decimal(1)),
    ]
    assert result.ready_at.isoformat() == "2026-09-13T16:00:00+03:00"
    assert result.carrier_handoff_at is None and result.carrier_delivery_at is None
    assert any("дата отправки неизвестна" in warning for warning in result.warnings)


def test_recommend_spb_before_moscow_but_allow_deliberate_moscow(profile, inventory):
    req = request(method="pickup", pickup="demo-pickup-sadovaya")
    local = run(req, profile, inventory)
    assert {a.warehouse_id for a in local.lines[0].sources} == {"demo-sadovaya", "demo-grand"}
    req.lines[0].sources = request(sources=[source("mitino", "2")]).lines[0].sources
    remote = run(req, profile, inventory)
    assert remote.ready_at > local.ready_at
    legs = remote.lines[0].sources[0].legs
    assert [(leg.source_warehouse_id, leg.target_warehouse_id) for leg in legs] == [
        ("demo-mitino", "demo-central"),
        ("demo-central", "demo-sadovaya"),
    ]
    # Missed Central's same-day 13:00 cutoff after arrival at 15:30.
    assert legs[1].departure_at.isoformat() == "2026-09-14T14:00:00+03:00"
    assert any("межгород" in warning for warning in remote.warnings)


@pytest.mark.parametrize("point", ["central", "mitino", "sadovaya", "grand", "pyatigorsk"])
def test_all_fixture_pickup_points(point, profile, inventory):
    result = run(request(method="pickup", pickup="demo-pickup-" + point), profile, inventory)
    assert result.consolidation_warehouse_id == "demo-" + point


def test_moscow_to_pyatigorsk(profile, inventory):
    result = run(
        request(
            method="pickup",
            pickup="demo-pickup-pyatigorsk",
            quantity="1",
            sources=[source("central", "1")],
        ),
        profile,
        inventory,
    )
    assert result.ready_at.isoformat() == "2026-09-14T15:00:00+03:00"


@pytest.mark.parametrize(("clock", "expected_day"), [("12:25:00", 13), ("12:25:01", 14)])
def test_cutoff_uses_assembled_time_and_payment_condition(clock, expected_day, profile, inventory):
    now = datetime.fromisoformat(f"2026-09-13T{clock}+03:00")
    result = run(request(sources=[source("mitino", "2")]), profile, inventory, now)
    assert result.lines[0].sources[0].legs[0].departure_at.day == expected_day


def test_schedule_weekend_and_timezone():
    monday = next_departure(NOW, [1], time(14), 60)
    assert monday.isoformat() == "2026-09-14T14:00:00+03:00"
    assert next_departure(NOW, list(range(1, 8)), time(14)).day == 13


def test_latest_required_part_includes_local_stock(profile, inventory):
    profile.assembly_minutes = 1440
    result = run(request(), profile, inventory)
    assert result.ready_at == max(a.ready_at for a in result.lines[0].sources) + timedelta(
        minutes=30
    )


@pytest.mark.parametrize(
    ("sources", "code"),
    [
        ([source("central", "2")], "selected_source_insufficient_stock"),
        ([source("sadovaya", "2")], "source_route_unavailable"),
        ([source("transit", "2")], "source_route_unavailable"),
        ([source("missing", "2")], "source_route_unavailable"),
        ([source("mitino", "1")], "allocation_quantity_mismatch"),
        ([source("mitino", "1"), source("mitino", "1")], "duplicate_source"),
        ([source("mitino", "0.5"), source("central", "1.5")], "allocation_step_invalid"),
    ],
)
def test_no_silent_replacement_or_quantity_loss(sources, code, profile, inventory):
    with pytest.raises(QuoteUnavailable, match=code):
        run(request(sources=sources), profile, inventory)


def test_unknown_and_inactive_pickup_fail_closed(profile, inventory):
    inventory.warehouses[2].active = False
    for point in ["demo-pickup-sadovaya", "unknown"]:
        with pytest.raises(QuoteUnavailable, match="pickup_point_unavailable"):
            run(request(method="pickup", pickup=point), profile, inventory)


def test_technical_and_inactive_never_in_options(profile, inventory):
    inventory.warehouses[1].active = False
    result = run(request(quantity="1"), profile, inventory)
    assert {o.warehouse_id for o in result.lines[0].options} == {"demo-central"}


def test_missing_route_does_not_turn_into_zero_duration(profile, inventory):
    profile.internal_routes = []
    with pytest.raises(QuoteUnavailable, match="source_route_unavailable"):
        run(request(sources=[source("mitino", "2")]), profile, inventory)


@pytest.mark.parametrize("age", [181, -1])
def test_stale_or_future_inventory(age, profile, inventory):
    inventory.observed_at = NOW - timedelta(seconds=age)
    with pytest.raises(QuoteUnavailable, match="inventory_not_fresh"):
        calculate_quote(request(), profile, inventory, now=NOW)


def test_duplicate_sku_lines_share_stock_and_stable_ids(profile, inventory):
    lines = [
        {"line_id": f"line-{index}", "product_id": "demo-display", "quantity": "3"}
        for index in range(2)
    ]
    with pytest.raises(QuoteUnavailable, match="insufficient_stock"):
        run(request(lines=lines), profile, inventory)
    lines[1]["quantity"] = "1"
    result = run(request(lines=lines), profile, inventory)
    assert [line.line_id for line in result.lines] == ["line-0", "line-1"]
    assert sum(a.quantity for line in result.lines for a in line.sources) == 4


def test_explicit_selection_has_priority_over_recommendation(profile, inventory):
    lines = [
        {"line_id": "a", "product_id": "demo-display", "quantity": "3"},
        {
            "line_id": "b",
            "product_id": "demo-display",
            "quantity": "1",
            "sources": [source("central", "1")],
        },
    ]
    result = run(request(lines=lines), profile, inventory)
    assert result.lines[0].sources[0].warehouse_id == "demo-mitino"


@pytest.mark.parametrize("field", ["characteristic_id", "series_id", "unit_id"])
def test_characteristic_series_unit_not_collapsed(field, profile, inventory):
    req = request(quantity="1")
    setattr(req.lines[0], field, "other")
    with pytest.raises(QuoteUnavailable, match="stock_identity_or_quantity_invalid"):
        run(req, profile, inventory)


def test_quote_does_not_mutate_stock_or_request(profile, inventory):
    req = request()
    before = inventory.model_dump_json(), req.model_dump_json()
    first = run(req, profile, inventory)
    second = run(req, profile, inventory)
    assert first == second
    assert before == (inventory.model_dump_json(), req.model_dump_json())
    changed = run(req, profile, inventory, NOW + timedelta(minutes=10))
    assert changed.quote_id != first.quote_id


def test_split_preserves_characteristic_series_and_fractional_unit(profile, inventory):
    identity = {
        "product_id": "demo-display",
        "characteristic_id": "revision-A",
        "series_id": "series-1",
        "unit_id": "metre",
    }
    data = inventory.model_dump(mode="json")
    data["stock"] = [
        {
            **identity,
            "warehouse_id": "demo-central",
            "free_quantity": "0.5",
            "quantity_step": "0.25",
        },
        {**identity, "warehouse_id": "demo-mitino", "free_quantity": "1", "quantity_step": "0.25"},
    ]
    inv = InventorySnapshot.model_validate(data)
    req = request(lines=[{**identity, "line_id": "stable-fractional-line", "quantity": "0.75"}])
    result = run(req, profile, inv)
    line = result.lines[0]
    assert line.line_id == "stable-fractional-line"
    assert line.key() == tuple(identity.values())
    assert [a.quantity for a in line.sources] == [Decimal("0.5"), Decimal("0.25")]


def test_carrier_schedule_separate_from_internal_routes(profile, inventory):
    profile.carrier_handoff["cdek"].weekdays = [1]
    profile.carrier_handoff["cdek"].local_time = time(17)
    result = run(request(), profile, inventory)
    assert result.ready_at.day == 13
    assert result.carrier_handoff_at.isoformat() == "2026-09-14T17:00:00+03:00"
    assert result.carrier_delivery_at is None


@pytest.mark.parametrize(
    "field", ["payment_available_at", "observed_at", "free_quantity", "reserve_confirmed"]
)
def test_caller_cannot_supply_server_facts(field):
    data = request().model_dump(mode="json")
    data[field] = "2026-09-13T00:00:00Z"
    with pytest.raises(ValidationError):
        FulfillmentQuoteRequest.model_validate(data)


def test_api_default_disabled_and_requires_auth(monkeypatch):
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", "test-quote-token")
    monkeypatch.setenv("ORDER_FULFILLMENT_QUOTE_MODE", "disabled")
    get_settings.cache_clear()
    try:
        client = TestClient(app)
        assert (
            client.post(
                "/api/order-fulfillment/quote", json=request().model_dump(mode="json")
            ).status_code
            == 401
        )
        response = client.post(
            "/api/order-fulfillment/quote",
            json=request().model_dump(mode="json"),
            headers={"Authorization": "Bearer test-quote-token"},
        )
        assert (
            response.status_code == 503
            and response.json()["detail"] == "fulfillment_quote_disabled"
        )
    finally:
        get_settings.cache_clear()


def test_production_environment_cannot_enable_fixture_quote(monkeypatch):
    monkeypatch.setenv("ORDER_FULFILLMENT_INTERNAL_API_TOKEN", "test-quote-token")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ORDER_FULFILLMENT_QUOTE_MODE", "test_fixture")
    get_settings.cache_clear()
    try:
        response = TestClient(app).post(
            "/api/order-fulfillment/quote",
            json=request().model_dump(mode="json"),
            headers={"Authorization": "Bearer test-quote-token"},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "fulfillment_quote_disabled"
    finally:
        get_settings.cache_clear()


def test_api_fixture_contract_and_hot_reload(monkeypatch, tmp_path, profile, inventory):
    profile_file = tmp_path / "profile.json"
    inventory_file = tmp_path / "inventory.json"
    inventory.observed_at = datetime.now(NOW.tzinfo)
    profile_file.write_text(profile.model_dump_json())
    inventory_file.write_text(inventory.model_dump_json())
    monkeypatch.setenv("ORDER_FULFILLMENT_QUOTE_MODE", "test_fixture")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ORDER_FULFILLMENT_QUOTE_PROFILE_PATH", str(profile_file))
    monkeypatch.setenv("ORDER_FULFILLMENT_QUOTE_INVENTORY_PATH", str(inventory_file))
    get_settings.cache_clear()
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[require_order_fulfillment_internal_token] = lambda: "test"
    try:
        client = TestClient(app)
        payload = request(quantity="1").model_dump(mode="json")
        first = client.post("/api/order-fulfillment/quote", json=payload)
        assert first.status_code == 200
        profile.final_packing_minutes += 60
        profile_file.write_text(profile.model_dump_json())
        second = client.post("/api/order-fulfillment/quote", json=payload)
        assert second.status_code == 200
        delta = datetime.fromisoformat(second.json()["ready_at"]) - datetime.fromisoformat(
            first.json()["ready_at"]
        )
        assert timedelta(minutes=60) <= delta < timedelta(minutes=61)
        profile_file.write_text("{}")
        assert client.post("/api/order-fulfillment/quote", json=payload).status_code == 503
    finally:
        app.dependency_overrides = previous
        get_settings.cache_clear()


def test_demo_has_no_real_order_payment_or_file_access():
    client = TestClient(create_demo_app())
    assert client.get("/").status_code == 200
    assert client.get("/demo/bootstrap").json()["test_only"] is True
    assert (
        client.post(
            "/api/order-fulfillment/quote", json=request().model_dump(mode="json")
        ).status_code
        == 200
    )
    for path in [
        "/.env",
        "/order/",
        "/api/order-payment-control/check",
        "/api/order-closures/prepay72/tick",
        "/api/logistics/sync/order-plans",
    ]:
        assert client.post(path).status_code == 404
    assert client.get("/", headers={"Host": "attacker.invalid"}).status_code == 400
    assert (
        client.post(
            "/api/order-fulfillment/quote",
            json=request().model_dump(mode="json"),
            headers={"Origin": "https://evil.invalid"},
        ).status_code
        == 403
    )
