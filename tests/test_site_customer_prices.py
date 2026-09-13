import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import site_customer_prices as api
from app.services import site_customer_prices as service


def settings():
    return SimpleNamespace(
        site_customer_prices_enabled=True,
        site_customer_prices_internal_token="test-price-token",
        site_customer_prices_resolver_argv=json.dumps([sys.executable, "-c", "print('{}')"]),
    )


def client(monkeypatch):
    monkeypatch.setattr(api, "get_settings", settings)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_token_and_strict_identity(monkeypatch):
    http = client(monkeypatch)
    assert http.post("/resolve", json={"user_id": 23978}).status_code == 401
    headers = {"X-Site-Price-Token": "test-price-token"}
    assert http.post("/resolve", headers=headers, json={"user_id": 0}).status_code == 422
    assert (
        http.post("/resolve", headers=headers, json={"user_id": 23978, "phone": "bad"}).status_code
        == 422
    )


def test_api_delegates_only_user_id(monkeypatch):
    http = client(monkeypatch)

    def resolve(uid):
        assert uid == 23978
        return dict(
            user_id=uid,
            status="conflict",
            reason="link_sync_pending",
            checked_at=datetime.now(timezone.utc),
            check_id="check",
        )

    monkeypatch.setattr(api, "resolve_site_customer_price", resolve)
    response = http.post(
        "/resolve", headers={"X-Site-Price-Token": "test-price-token"}, json={"user_id": 23978}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "conflict"


def test_bad_worker_output_never_retail(monkeypatch):
    monkeypatch.setattr(service, "get_settings", settings)
    assert service.resolve_site_customer_price(23978).status == "unavailable"


def test_non_object_worker_output_fails_closed(monkeypatch):
    conf = settings()
    conf.site_customer_prices_resolver_argv = json.dumps([sys.executable, "-c", "print('[]')"])
    monkeypatch.setattr(service, "get_settings", lambda: conf)
    assert service.resolve_site_customer_price(23978).status == "unavailable"


def test_non_string_worker_status_fails_closed(monkeypatch):
    raw = dict(
        user_id=23978,
        status=123,
        reason="invalid",
        checked_at=datetime.now(timezone.utc).isoformat(),
        check_id="c",
    )
    conf = settings()
    conf.site_customer_prices_resolver_argv = json.dumps(
        [sys.executable, "-c", "print(" + repr(json.dumps(raw)) + ")"]
    )
    monkeypatch.setattr(service, "get_settings", lambda: conf)
    assert service.resolve_site_customer_price(23978).status == "unavailable"


def test_fixed_argv_handles_confirmed_result(monkeypatch):
    raw = dict(
        user_id=23978,
        status="confirmed",
        reason="linked",
        checked_at=datetime.now(timezone.utc).isoformat(),
        check_id="c",
        price_type="2.Бронзовый",
        expected_group="10",
        catalog_group_id=10,
    )
    conf = settings()
    conf.site_customer_prices_resolver_argv = json.dumps(
        [sys.executable, "-c", "print(" + repr(json.dumps(raw)) + ")"]
    )
    monkeypatch.setattr(service, "get_settings", lambda: conf)
    assert service.resolve_site_customer_price(23978).catalog_group_id == 10
    assert service.resolve_site_customer_price(123978).status == "unavailable"


def test_disabled_worker_does_not_execute(monkeypatch):
    conf = settings()
    conf.site_customer_prices_enabled = False
    monkeypatch.setattr(service, "get_settings", lambda: conf)
    monkeypatch.setattr(
        service.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    assert service.resolve_site_customer_price(23978).status == "unavailable"
